"""guacd: the Clouds Guacamole background service.

One long-running process per user that:
  * runs one `rclone rcd` hosting every mount, plus a short-lived `rclone rcd` per running sync,
    talking to them over private unix sockets (no rclone process is ever spawned for a status
    check);
  * streams drives with a tuned VFS cache and keeps chosen folders on this device with
    `bisync`, triggered by local changes (inotify), a timer, and resume-from-suspend;
  * pushes state to the bar widget over its own unix socket only when something changed.
"""

import asyncio
import concurrent.futures
import copy
import errno
import fcntl
import fnmatch
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, config, filters, paths, providers, recent, util
from .inotify import (
    IN_CLOSE_WRITE,
    IN_CREATE,
    IN_DELETE_SELF,
    IN_IGNORED,
    IN_ISDIR,
    IN_MOVE_SELF,
    IN_MOVED_TO,
    IN_Q_OVERFLOW,
    Inotify,
)
from .rc import RcClient, RcError, RcUnavailable

log = logging.getLogger("guac")

TICK_ACTIVE = 1.0  # while anything transfers
TICK_IDLE = 3.0  # a client is connected, nothing moving
TICK_BACKGROUND = 10.0  # nobody watching
QUOTA_TTL = 30 * 60
RECENT_TTL = 60
RECENT_LIMIT = 12
MAX_SYNC_JOBS = 3  # folders syncing at once, each in its own rclone process
DEBOUNCE = 4.0  # quiet time after a local change before syncing it
FOLLOWUP_DEBOUNCE = 10.0  # changes seen while a sync ran (mostly its own writes)
MAX_DIRTY_WAIT = 30.0  # never hold local changes back longer than this
SCHEDULER_MAX_SLEEP = 30.0  # the scheduler looks this often even with nothing due
MOUNT_TIMEOUT = 90.0
MAX_WATCH_DIRS = 100000
MAX_BROWSE_ENTRIES = 2000
CLIENT_LINE_LIMIT = 1 << 20
CLIENT_MAX_BUFFER = 8 << 20

# Editor and download temp files: never worth waking the syncer for (they are filtered from sync too)
_IGNORED_EVENT_NAMES = [
    ".~lock.*#",
    "~$*",
    ".~*",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swx",
    "*~",
    "*.part",
    "*.partial",
    "*.crdownload",
    ".goutputstream-*",
    "4913",
    ".DS_Store",
    "*.kate-swp",
]
# One pattern for all of them: matched against every event in a burst of writes
_IGNORED_EVENT_RE = re.compile(
    "|".join(fnmatch.translate(p) for p in _IGNORED_EVENT_NAMES)
)

_SYSTEM_DIRS = [
    "/usr",
    "/etc",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var",
    "/opt",
    "/srv",
    "/root",
]


class UserError(Exception):
    """A failure to show to the user as-is."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class Drive:
    name: str
    mount_state: str = (
        "off"  # off | mounting | mounted | unmounting | error | waiting | foreign
    )
    error: str = ""
    error_kind: str = ""
    retry_at: float = 0.0
    backoff: float = 0.0
    mount_point: str = ""
    kernel_path: str = (
        ""  # mount_point with symlinks in its parents resolved, as /proc lists it
    )
    mounted_fs: str = ""
    meta_path: str = ""
    data_path: str = ""
    uploads: int = 0
    cache_bytes: int = 0
    cache_files: int = 0
    quota_busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Folder:
    id: str
    remote: str
    path: str
    local: str
    paused: bool = False
    label: str = ""
    # Own never-synced patterns, used instead of filters.txt; empty: the global list
    excludes: list[str] = field(default_factory=list)
    # Digest of the filter rules the last successful sync ran with, and the running one's
    filters_md5: str = ""
    run_filters_md5: str = ""
    job_id: int = 0
    job_started: float = 0.0
    progress: dict[str, Any] = field(default_factory=dict)
    initialized: bool = False
    last_sync: float = 0.0
    last_changes: int = 0
    error: str = ""
    error_kind: str = ""
    attention: str = ""
    attention_code: str = ""
    next_due: float = 0.0
    retry_at: float = 0.0
    backoff: float = 0.0
    dirty_since: float = 0.0
    last_event: float = 0.0
    dirty_during_run: bool = False
    followup_only: bool = (
        False  # dirty only from events seen during the last run (mostly its own writes)
    )
    resync_reason: str = ""
    run_resync: bool = (
        False  # the running job's mode, independent of later filter edits
    )
    recovery_attempted: bool = False  # cleared only after an ordinary sync succeeds
    force_next: bool = False
    conflicts: list[str] = field(default_factory=list)
    watching: bool = False
    watch_error: str = ""
    waiting_network: bool = False
    moving: list[str] = field(
        default_factory=list
    )  # files the running sync is transferring
    rerun: bool = False  # "sync now" was asked while a sync was already running


@dataclass
class SyncWorker:
    """The rclone process running one folder's bisync.

    bisync captures its log by redirecting rclone's global logger, so two bisyncs in one process
    mix their output (conflicts and errors land on the wrong folder) and can leave the log file
    pointing at a finished job's buffer. One process per running sync keeps each output its own.
    """

    proc: asyncio.subprocess.Process
    rcc: RcClient


def _prepare_mount_point(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    with os.scandir(path) as it:
        for _ in it:
            raise UserError(
                f"The mount folder {path} is not empty. Move its files away or pick another location."
            )


def _remove_empty_dir(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _kernel_path(path: str) -> str:
    """How /proc/self/mountinfo will name a mount at path: resolve the parent, never the mount itself."""
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _probe(path: str) -> bool:
    """True if path answers stat; False if it's a dead mount (ENOTCONN and friends)."""
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _top_dirs(path: str, limit: int) -> list[str]:
    names = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
            except OSError:
                continue
            if len(names) >= limit:
                break
    return names


def _list_dirs(path: str, limit: int) -> list[str]:
    names = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
            except OSError:
                continue
            if len(names) >= limit:
                break
    return sorted(names, key=str.lower)


def _collect_dirs(
    top: str, limit: int, skip: Callable[[str], bool] | None = None
) -> list[str]:
    found = []
    stack = [top]
    while stack and len(found) < limit:
        d = stack.pop()
        found.append(d)
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if (
                            entry.is_dir(follow_symlinks=False)
                            and not entry.name.startswith(".Trash-")
                            and (skip is None or not skip(entry.path))
                        ):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _relative(path: str, top: str) -> str:
    if path.startswith(top + "/"):
        return path[len(top) + 1 :]
    return os.path.relpath(path, top)


def _write_filters(path: Path, text: str) -> None:
    """Write a filter file in place of the old one, only when its rules changed."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    config.write_text_atomic(path, text)


def _ensure_link(fid: str, target: str) -> str:
    link_dir = paths.ensure_private_dir(paths.link_dir())
    link = link_dir / fid
    try:
        if os.readlink(link) == target:
            return str(link)
        os.unlink(link)
    except FileNotFoundError:
        pass
    except OSError:
        os.unlink(link)
    os.symlink(target, link)
    return str(link)


def _session_name_length(remote_spec: str, link: str) -> int:
    """Length of the file names bisync derives from both sides (see rclone's bilib.SessionName)."""
    canon = lambda s: re.sub(r"[\\/:?*<>|]", "_", s.strip("\\/"))
    return len(canon(remote_spec)) + 2 + len(canon(link)) + len(".path1.lst-dry-new")


def _dir_nonempty(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        pass
    return False


def _prune_backups(root: str, days: int) -> None:
    cutoff = time.time() - days * 86400
    try:
        folders = list(os.scandir(root))
    except OSError:
        return
    for folder in folders:
        try:
            dated = list(os.scandir(folder.path))
        except OSError:
            continue
        for d in dated:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name):
                try:
                    if time.mktime(time.strptime(d.name, "%Y-%m-%d")) < cutoff:
                        shutil.rmtree(d.path, ignore_errors=True)
                except ValueError:
                    pass


def _count_files(root: str, limit: int = 10000) -> int:
    count = 0
    for _, _, files in os.walk(root):
        count += len(files)
        if count >= limit:
            break
    return count


def _resolve_onedrive(token_json: str) -> tuple[str | None, str | None]:
    try:
        token = json.loads(token_json).get("access_token")
        if not token:
            return None, None
        req = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/me/drive",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read(1 << 20).decode())
        return data.get("id"), data.get("driveType", "personal")
    except Exception:  # noqa: BLE001 - any failure means the drive can't be looked up
        return None, None


def _resolve_pcloud_host(token_json: str) -> str | None:
    """pCloud keeps US and EU accounts on different API hosts; find the one that knows the token."""
    try:
        token = json.loads(token_json).get("access_token")
    except (ValueError, AttributeError):
        return None
    if not token:
        return None
    for host in ("api.pcloud.com", "eapi.pcloud.com"):
        try:
            req = urllib.request.Request(
                f"https://{host}/userinfo", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if json.loads(resp.read(1 << 20).decode()).get("result") == 0:
                    return host
        except Exception:  # noqa: BLE001, S112 - any failure means "not this host"
            continue
    return None


def clean_remote_path(path: Any) -> str:
    parts = [
        p for p in str(path or "").replace("\\", "/").split("/") if p not in ("", ".")
    ]
    if any(p == ".." for p in parts):
        raise UserError("Paths cannot contain '..'")
    return "/".join(parts)


def clean_remote_name(name: Any) -> str:
    clean = "".join(c for c in str(name or "") if c.isalnum() or c in "-_").strip()
    if not clean:
        raise UserError("Give the drive a name (letters, digits, '-' and '_')")
    if clean.startswith("-"):
        raise UserError("Names cannot start with '-'")
    return clean


def _parse_conflicts(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if "Renaming Path1 copy" in line or "Renaming Path2 copy" in line:
            target = line.rsplit(" - ", 1)[-1].strip()
            name = os.path.basename(target)
            if name and name not in out:
                out.append(util.neutralize_controls(name))
    return out


def _transient_failure(text: str) -> str:
    """ "rate" or "network" when bisync's critical error came from throttling or the network.

    bisync ends every critical error with "Must run --resync to recover", so that phrase alone
    doesn't mean the sync state is broken: a throttled or dropped listing only needs a retry.
    """
    critical = "\n".join(l for l in text.splitlines() if "critical error" in l.lower())
    kind = util.classify_error(critical) if critical else "other"
    return kind if kind in ("rate", "network") else ""


def _stats_speed(stats: dict[str, Any]) -> int:
    """Bytes per second of a core/stats answer.

    rclone leaves the per-file speeds empty for transfers driven by sync and bisync, so fall
    back to the group's average while something is moving.
    """
    moving = stats.get("transferring") or []
    per_file = sum(int(t.get("speedAvg") or 0) for t in moving)
    return int(per_file or (float(stats.get("speed") or 0) if moving else 0))


def _batch_result(r: Any) -> dict[str, Any] | RcError:
    """One entry of job/batch's results, as RcClient.call would have returned or raised it."""
    if not isinstance(r, dict):
        return {"result": r}
    if "error" in r and "status" in r:
        return RcError(str(r["error"]), int(r.get("status") or 0))
    return r


class Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer

    def send(self, obj: Any) -> None:
        self.send_line(json.dumps(obj, separators=(",", ":")))

    def send_line(self, text: str) -> None:
        if self.writer.is_closing():
            return
        if self.writer.transport.get_write_buffer_size() > CLIENT_MAX_BUFFER:
            # A client that stopped reading must not grow our memory without bound
            self.writer.close()
            return
        self.writer.write(text.encode() + b"\n")


class Daemon:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.st = config.load_state()
        self.rclone_bin = shutil.which("rclone")
        self.rcc = RcClient(str(paths.rclone_socket()))
        self.rc_pool = concurrent.futures.ThreadPoolExecutor(
            16, thread_name_prefix="rc"
        )
        # Filesystem probes get their own pool: a hung FUSE stat must never starve rc calls
        self.fs_pool = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="fs")
        self.engine: asyncio.subprocess.Process | None = None
        self.workers: dict[str, SyncWorker] = {}  # folder id -> rclone running its sync
        self.engine_state = "starting"
        self.engine_error = ""
        self.rclone_version = ""
        self.rclone_config_path = ""
        self._rclone_conf_sig: Any = None
        self.remotes: dict[str, dict[str, Any]] = {}
        self.drives: dict[str, Drive] = {}
        self.folders: dict[str, Folder] = {}
        self.clients: set[Client] = set()
        self.online = util.has_default_route()
        self.transfers: dict[str, Any] = {"count": 0, "speed": 0, "names": []}
        self.recent: list[dict[str, Any]] = []
        self._recent_at = 0.0
        self._recent_due = 0.0
        self.auth: dict[str, Any] = {
            "busy": False,
            "waiting": False,
            "url": "",
            "error": "",
            "success": "",
            "remote": "",
        }
        self._auth_proc: asyncio.subprocess.Process | None = None
        self._auth_task: asyncio.Task | None = None
        self.inotify: Inotify | None = None
        self._wd: dict[int, tuple[str, str]] = {}
        self._folder_wds: dict[str, set[int]] = {}
        self._last_state_line = ""
        self._boot_offset = self._boottime_offset()
        self._notified: dict[str, float] = {}
        self._backups_pruned = 0.0
        self.backup_files = 0  # local files syncs replaced or deleted, kept in backups/
        self._filters_sig: Any = None
        self._global_filters = ""  # filters.txt as last read
        self._global_md5 = ""
        # filters.txt's digest saved by versions that tracked one for every folder
        self._legacy_filters_md5 = ""
        self._matchers: dict[str, tuple[str, filters.Matcher]] = {}
        self._rc_batch = True  # until an rclone without job/batch says otherwise
        self._tasks: list[asyncio.Task] = []
        # Bound to the event loop on first use
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tick_now = asyncio.Event()

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _boottime_offset() -> float:
        # CLOCK_MONOTONIC stops during suspend, CLOCK_BOOTTIME doesn't: their gap grows on resume
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    def mark(self) -> None:
        self._dirty.set()
        # Whatever changed may have made a folder due: the scheduler sleeps until told so
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def rc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        client: RcClient | None = None,
    ) -> dict[str, Any]:
        """Call the engine, or with client, the rclone process running a sync."""
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            self.rc_pool, (client or self.rcc).call, method, params or {}, timeout
        )
        try:
            return await asyncio.wait_for(fut, timeout + 5)
        except asyncio.TimeoutError:
            raise RcError(f"rclone {method} timed out")

    async def rc_batch(
        self, calls: list[tuple[str, dict[str, Any]]], timeout: float = 5.0
    ) -> list[dict[str, Any] | RcError]:
        """Quick engine calls in one request, so rclone wakes once instead of once per call.
        Each result is the call's reply or its error; rclone without job/batch gets one each."""
        if self._rc_batch:
            try:
                res = await self.rc(
                    "job/batch",
                    {
                        "concurrency": 1,
                        "inputs": [{"_path": method, **p} for method, p in calls],
                    },
                    timeout=timeout,
                )
            except RcError as e:
                if e.status != 404:
                    raise
                self._rc_batch = False
            else:
                results = res.get("results")
                if isinstance(results, list) and len(results) == len(calls):
                    return [_batch_result(r) for r in results]
        out: list[dict[str, Any] | RcError] = []
        for method, p in calls:
            try:
                out.append(await self.rc(method, p, timeout=timeout))
            except RcError as e:
                out.append(e)
        return out

    async def fs(self, fn: Callable, *args: Any, timeout: float = 5.0) -> Any:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(self.fs_pool, fn, *args), timeout
        )

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            pass
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    def notify(
        self, key: str, summary: str, body: str = "", urgency: str = "normal"
    ) -> None:
        if not self.cfg.get("notifications", True) or not shutil.which("notify-send"):
            return
        now = time.time()
        if now - self._notified.get(key, 0) < 600:
            return
        self._notified[key] = now

        async def send() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "notify-send",
                    "-a",
                    "Clouds Guacamole",
                    "-u",
                    urgency,
                    "-i",
                    "folder-cloud",
                    summary,
                    body,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), 10)
            except (OSError, asyncio.TimeoutError):
                pass

        self._spawn(send())

    def save_cfg(self) -> None:
        config.save(self.cfg)

    def mount_root(self) -> Path:
        return paths.expand(self.cfg["mount_root"])

    def local_root(self) -> Path:
        return paths.expand(self.cfg["local_root"])

    def cloud_root(self) -> Path:
        """What "open the cloud folder" opens: the shared parent of the stream and sync roots
        (~/Cloud for ~/Cloud/Stream and ~/Cloud/Sync), else the stream root."""
        mount_root, local_root = self.mount_root(), self.local_root()
        parent = mount_root.parent
        if parent == local_root.parent and parent not in (Path.home(), Path("/")):
            return parent
        return mount_root

    def desired_mount_point(self, name: str) -> Path:
        r = self.cfg["remotes"].get(name) or {}
        if r.get("mount_path"):
            return paths.expand(r["mount_path"])
        return self.mount_root() / name

    def _shared_client_note(self, name: str) -> str:
        """For a rate limit: the advice when the drive signs in with rclone's shared OAuth client."""
        r = self.remotes.get(name) or {}
        rtype = r.get("type")
        if rtype not in providers.CLIENT_GUIDES or r.get("custom_client"):
            return ""
        if rtype == "drive":
            return (
                "Google is rejecting rclone's shared client ID (its quota, shared by every rclone user, "
                "is used up). Set up your own client ID to fix this for good."
            )
        return (
            f"rclone's built-in {r['provider']['name']} app is shared by every rclone user and is "
            "being rate-limited. Set up your own to fix this for good."
        )

    def remote_cfg(self, name: str) -> dict[str, Any]:
        if name not in self.cfg["remotes"]:
            self.cfg["remotes"][name] = config.normalize_remote({})
        return self.cfg["remotes"][name]

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: self._spawn(self.reload()))

        paths.ensure_private_dir(paths.state_dir())
        paths.ensure_private_dir(paths.backup_root())
        config.ensure_filters()
        self._spawn(self._ensure_local_root())
        self._filters_sig = self._filters_signature()
        self._read_global_filters()
        self._legacy_filters_md5 = str(self.st.get("filters_md5") or "")
        self.st["filters_md5"] = self._global_md5
        self._init_inotify()
        self._rebuild_folders()

        server = await self._start_server()
        log.info("guacd %s listening on %s", __version__, paths.daemon_socket())
        for coro in (
            self._supervise_engine(),
            self._ticker(),
            self._scheduler(),
            self._broadcaster(),
            self._slow_loop(),
        ):
            self._spawn(coro)

        await self._stop.wait()
        log.info("shutting down")
        server.close()
        await self._shutdown()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            paths.daemon_socket().unlink()
        except OSError:
            pass
        return 0

    async def _ensure_local_root(self) -> None:
        """The sync root exists from the start, so the cloud folder shows both halves."""
        try:
            await self.fs(os.makedirs, str(self.local_root()), 0o755, True, timeout=5)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("could not create %s: %s", self.local_root(), e)

    async def reload(self) -> None:
        self.cfg = config.load()
        await self.load_remotes()
        self._rebuild_folders()
        for name in list(self.drives):
            self._spawn(self.reconcile_drive(name))
        self.mark()

    async def _start_server(self) -> asyncio.AbstractServer:
        paths.ensure_runtime_dir()
        sock_path = paths.daemon_socket()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        # Private through its 0700 folder, then by its own mode. Never via os.umask: that is
        # process-wide, and folders created meanwhile by other threads (the sync root) would
        # lose their execute bit and refuse every file
        server = await asyncio.start_unix_server(
            self._handle_client, path=str(sock_path), limit=CLIENT_LINE_LIMIT
        )
        os.chmod(sock_path, 0o600)
        return server

    async def _shutdown(self) -> None:
        await asyncio.gather(
            *(self._cancel_sync(f) for f in list(self.folders.values()) if f.job_id)
        )
        await asyncio.gather(*(self._stop_worker(fid) for fid in list(self.workers)))
        if self.engine_state == "running":
            for d in list(self.drives.values()):
                if d.mount_state == "mounted":
                    try:
                        await self.rc(
                            "mount/unmount", {"mountPoint": d.mount_point}, timeout=15
                        )
                    except RcError:
                        await self._lazy_unmount(d.mount_point)
            try:
                await self.rc("core/quit", {}, timeout=5)
            except RcError:
                pass
        if self.engine and self.engine.returncode is None:
            try:
                await asyncio.wait_for(self.engine.wait(), 10)
            except asyncio.TimeoutError:
                self.engine.kill()
        self._persist_all_folders()

    # ------------------------------------------------------------------ rclone engine

    async def _start_worker(self, f: Folder) -> SyncWorker:
        sock = paths.sync_socket(f.id)
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
        rcc = RcClient(str(sock))
        w = SyncWorker(await self._launch_rcd(sock, rcc, []), rcc)
        self.workers[f.id] = w
        return w

    async def _stop_worker(self, fid: str) -> None:
        w = self.workers.pop(fid, None)
        if w is None:
            return
        if w.proc.returncode is None:
            # SIGTERM runs rclone's normal exit path at once; core/quit waits 1.5s first
            w.proc.terminate()
            try:
                await asyncio.wait_for(w.proc.wait(), 5)
            except asyncio.TimeoutError:
                w.proc.kill()
                await w.proc.wait()
        try:
            paths.sync_socket(fid).unlink()
        except OSError:
            pass

    async def _cancel_sync(self, f: Folder, wait: float = 10.0) -> None:
        """Stop a running sync, give bisync a moment to wind down, then end its rclone."""
        w = self.workers.get(f.id)
        if f.job_id and w is not None:
            try:
                await self.rc("job/stop", {"jobid": f.job_id}, timeout=5, client=w.rcc)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    js = await self.rc(
                        "job/status", {"jobid": f.job_id}, timeout=3, client=w.rcc
                    )
                    if js.get("finished"):
                        break
                    await asyncio.sleep(0.2)
            except RcError:
                pass
        f.job_id, f.progress, f.moving = 0, {}, []
        await self._stop_worker(f.id)

    async def _supervise_engine(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            self.rclone_bin = self.rclone_bin or shutil.which("rclone")
            if not self.rclone_bin:
                self.engine_state, self.engine_error = (
                    "missing",
                    "rclone is not installed (sudo pacman -S rclone)",
                )
                self.mark()
                await self._sleep(15)
                continue
            try:
                engine = await self._start_engine()
            except Exception as e:  # noqa: BLE001 - surface any startup failure
                self.engine_state, self.engine_error = "error", util.clean_error(str(e))
                log.error("rclone failed to start: %s", e)
                self.mark()
                await self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            delay = 1.0
            self.engine_state, self.engine_error = "running", ""
            self.mark()
            try:
                await self._after_engine_start()
            except Exception:
                log.exception("post-start setup failed")
            code = await engine.wait()
            if self._stop.is_set():
                return
            log.warning("rclone exited unexpectedly (%s); restarting", code)
            self.engine_state = "restarting"
            self._on_engine_lost()
            self.mark()
            await self._sleep(1)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _start_engine(self) -> asyncio.subprocess.Process:
        sock = paths.rclone_socket()
        paths.ensure_private_dir(sock.parent)
        if sock.exists():
            # A previous daemon's rclone survived it: stop that one so we own a clean engine
            if await asyncio.get_running_loop().run_in_executor(
                self.rc_pool, self.rcc.ping, 1.0
            ):
                try:
                    await self.rc("core/quit", {}, timeout=3)
                except RcError:
                    pass
                for _ in range(50):
                    if not sock.exists():
                        break
                    await asyncio.sleep(0.1)
            try:
                sock.unlink()
            except OSError:
                pass
        self.engine = engine = await self._launch_rcd(
            sock,
            self.rcc,
            [
                "--rc-job-expire-duration=10m",
                "--rc-job-expire-interval=1m",
                # Only the engine rotates the shared log; sync processes just append to it
                "--log-file-max-size=5M",
                "--log-file-max-backups=1",
            ],
        )
        try:
            ver = await self.rc("core/version", timeout=5)
            self.rclone_version = str(ver.get("version", ""))
            cp = await self.rc("config/paths", timeout=5)
            self.rclone_config_path = str(cp.get("config", ""))
        except RcError:
            pass
        log.info("rclone %s ready", self.rclone_version)
        return engine

    async def _launch_rcd(
        self, sock: Path, rcc: RcClient, extra: list[str]
    ) -> asyncio.subprocess.Process:
        """Start an `rclone rcd` on sock and wait until it answers."""
        if not self.rclone_bin:
            raise RcUnavailable("rclone is not installed")
        log_path = paths.rclone_log()
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        args = [
            self.rclone_bin,
            "rcd",
            f"--rc-addr=unix://{sock}",
            "--rc-no-auth",
            f"--log-file={log_path}",
            "--log-level=INFO",
            "--ask-password=false",
            "--stats=0",
            "--fast-list",
            "--transfers=4",
            "--checkers=8",
            # Fail fast when the network drops instead of hanging for rclone's default 5 minutes
            "--timeout=60s",
            "--contimeout=15s",
            "--low-level-retries=5",
            "--use-mmap",
            *extra,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=fd,
                preexec_fn=util.die_with_parent(),
            )
        finally:
            os.close(fd)
        deadline = time.monotonic() + 20
        loop = asyncio.get_running_loop()
        while True:
            if proc.returncode is not None:
                tail = " ".join(util.tail_lines(log_path, 3))
                raise RuntimeError(f"rclone exited during startup: {tail}")
            if sock.exists() and await loop.run_in_executor(
                self.rc_pool, rcc.ping, 1.0
            ):
                return proc
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError("rclone did not start in time")
            await asyncio.sleep(0.05)

    async def _after_engine_start(self) -> None:
        await self.load_remotes()
        for name in list(self.drives):
            self._spawn(self.reconcile_drive(name, force=True))

    def _on_engine_lost(self) -> None:
        # Syncs run in their own rclone processes and carry on
        for d in self.drives.values():
            if d.mount_state in ("mounted", "mounting", "unmounting"):
                d.mount_state = "off"
                d.retry_at = 0

    def _rclone_conf_signature(self) -> Any:
        try:
            st = os.stat(self.rclone_config_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    async def load_remotes(self) -> None:
        try:
            dump = await self.rc("config/dump", timeout=15)
        except RcError as e:
            log.warning("could not read rclone config: %s", e)
            return
        self._rclone_conf_sig = self._rclone_conf_signature()
        remotes = {}
        for name, conf in dump.items():
            if not isinstance(conf, dict):
                continue
            # Only derived, non-secret facts are kept; tokens and passwords stay inside rclone
            remotes[name] = {
                "type": str(conf.get("type", "")),
                "provider": providers.detect(conf),
                "warnings": providers.remote_warnings(conf),
                "custom_client": bool(str(conf.get("client_id", "")).strip()),
            }
        self.remotes = remotes
        changed = False
        for name in remotes:
            if name not in self.cfg["remotes"]:
                # Remotes found in rclone.conf show up as drives, not streamed until asked
                self.cfg["remotes"][name] = config.normalize_remote({})
                changed = True
            if name not in self.drives:
                self.drives[name] = Drive(name)
        for name in list(self.drives):
            if name not in remotes and self.drives[name].mount_state in (
                "off",
                "error",
                "waiting",
            ):
                del self.drives[name]
        if changed:
            self.save_cfg()
        self._rebuild_folders()
        self.mark()

    # ------------------------------------------------------------------ mounts

    async def reconcile_drive(
        self, name: str, force: bool = False, wait: bool = False
    ) -> None:
        d = self.drives.get(name)
        if d is None or self.engine_state != "running":
            return
        if d.lock.locked() and not wait:
            return
        async with d.lock:
            want = bool(self.remote_cfg(name).get("stream")) and name in self.remotes
            if want:
                target = str(self.desired_mount_point(name))
                if d.mount_state == "mounted" and d.mount_point != target:
                    try:
                        await self._unmount(d, force=False)
                    except UserError as e:
                        # Moving it has to wait for uploads to finish or files to be closed
                        d.error = f"Not moved to {target} yet: {e}"
                        self.mark()
                        return
                if d.mount_state == "mounted":
                    return
                if d.mount_state == "foreign" and not force:
                    return
                if not force and d.retry_at > time.time():
                    return
                await self._mount(d)
            else:
                if d.mount_state == "mounted":
                    if not force and d.retry_at > time.time():
                        return
                    try:
                        await self._unmount(d, force=False)
                    except UserError as e:
                        # Pending uploads or a busy mount: stay mounted and try again shortly
                        d.error, d.retry_at = str(e), time.time() + 30
                        self.mark()
                elif d.mount_state in ("error", "waiting", "foreign"):
                    d.mount_state, d.error, d.error_kind = "off", "", ""
                    self.mark()

    async def _mount(self, d: Drive) -> None:
        mp = str(self.desired_mount_point(d.name))
        d.mount_state, d.error, d.error_kind = "mounting", "", ""
        self.mark()
        # No offline gate here: some VPN and policy-routing setups have no default route in the
        # main table, and a wrong guess would stop drives from ever mounting. rclone fails fast.
        try:
            kpath = await self.fs(_kernel_path, mp, timeout=5)
            existing = util.read_mountinfo().get(kpath)
            if existing is not None:
                try:
                    alive = await self.fs(_probe, mp, timeout=3)
                except asyncio.TimeoutError:
                    alive = False
                if alive:
                    d.mount_state, d.error_kind = "foreign", "foreign"
                    d.error = f"Already mounted by another program ({existing['source'] or existing['fstype']})"
                    self.mark()
                    return
                log.info("clearing stale mount at %s", mp)
                await self._lazy_unmount(kpath)
            await self.fs(_prepare_mount_point, mp, timeout=10)
            fs_name = f"{d.name}:"
            info = await self.rc(
                "operations/fsinfo", {"fs": fs_name}, timeout=MOUNT_TIMEOUT
            )
            polling = bool((info.get("Features") or {}).get("ChangeNotify"))
            label = self.remote_cfg(d.name).get("label") or d.name
            cfg = self.cfg
            vfs_opt = {
                "CacheMode": "full",
                "CacheMaxSize": f"{cfg['cache_max_size_gb']}G",
                "CacheMaxAge": cfg["cache_max_age"],
                "CacheMinFreeSpace": f"{cfg['cache_min_free_gb']}G",
                # Backends that push change notifications keep listings fresh on their own, so
                # listings can stay cached for days: browsing never waits on the network.
                "DirCacheTime": "168h" if polling else "10m",
                "PollInterval": "30s",
                "FastFingerprint": True,
                "ReadAhead": "16M",
                "WriteBack": "5s",
            }
            mount_opt = {"AttrTimeout": "5s", "VolumeName": label, "AsyncRead": True}
            await self.rc(
                "mount/mount",
                {
                    "fs": fs_name,
                    "mountPoint": mp,
                    "vfsOpt": vfs_opt,
                    "mountOpt": mount_opt,
                },
                timeout=MOUNT_TIMEOUT,
            )
        except UserError as e:
            self._mount_failed(d, str(e), "other", retry=False)
            return
        except asyncio.TimeoutError:
            self._mount_failed(d, "The mount folder did not respond", "other")
            return
        except RcError as e:
            msg = util.clean_error(str(e))
            self._mount_failed(d, msg, util.classify_error(msg))
            return
        except OSError as e:
            self._mount_failed(
                d,
                f"Can't use the mount folder {mp}: {e.strerror or e}",
                "other",
                retry=False,
            )
            return
        except Exception as e:
            log.exception("mounting %s failed", d.name)
            self._mount_failed(d, f"Unexpected error: {e}", "other")
            return
        d.mount_state, d.mount_point, d.kernel_path, d.mounted_fs = (
            "mounted",
            mp,
            kpath,
            f"{d.name}:",
        )
        d.backoff, d.retry_at, d.error, d.error_kind = 0, 0, "", ""
        log.info("mounted %s at %s", d.name, mp)
        self.mark()
        self._recent_due = time.time() + 3
        self._spawn(self._warm(d))

    def _mount_failed(self, d: Drive, msg: str, kind: str, retry: bool = True) -> None:
        log.warning("mount %s failed (%s): %s", d.name, kind, msg)
        if kind == "auth":
            msg = (
                "Sign-in expired or was revoked. Reconnect this account. (" + msg + ")"
            )
            retry = False
        elif kind == "rate":
            note = self._shared_client_note(d.name)
            if note:
                msg = note + " Retrying meanwhile."
            else:
                msg = "Rate-limited by the provider; retrying. (" + msg + ")"
        d.error, d.error_kind = msg, kind
        d.mount_state = "waiting" if kind in ("network", "rate") else "error"
        if retry:
            d.backoff = min(max(d.backoff * 2, 15.0), 300.0)
            d.retry_at = time.time() + d.backoff
        else:
            d.retry_at = float("inf")
        self.mark()

    async def _unmount(self, d: Drive, force: bool) -> None:
        if not force:
            try:
                st = await self.rc("vfs/stats", {"fs": d.mounted_fs}, timeout=5)
                dc = st.get("diskCache") or {}
                pending = int(dc.get("uploadsInProgress", 0)) + int(
                    dc.get("uploadsQueued", 0)
                )
            except RcError:
                pending = 0
            if pending:
                raise UserError(
                    f"{pending} file(s) are still uploading from {d.name}. Wait, or unmount anyway.",
                    code="uploads_pending",
                )
        d.mount_state = "unmounting"
        self.mark()
        try:
            await self.rc("mount/unmount", {"mountPoint": d.mount_point}, timeout=30)
        except RcError as e:
            msg = util.clean_error(str(e))
            if not force:
                d.mount_state = "mounted"
                self.mark()
                if "busy" in msg.lower():
                    raise UserError(
                        f"{d.name} is in use (an open file or terminal). Close it, or unmount anyway.",
                        code="busy",
                    )
                raise UserError(f"Could not unmount {d.name}: {msg}")
            await self._lazy_unmount(d.mount_point)
            try:
                await self.rc(
                    "mount/unmount", {"mountPoint": d.mount_point}, timeout=10
                )
            except RcError:
                pass
        d.mount_state, d.error, d.error_kind = "off", "", ""
        d.uploads = d.cache_bytes = d.cache_files = 0
        log.info("unmounted %s", d.name)
        self.mark()
        # The mount folder is created on mount; leave no empty folder behind (rmdir never
        # removes anything else, and fails harmlessly on a mount that is still detaching)
        try:
            await self.fs(_remove_empty_dir, d.mount_point, timeout=5)
        except asyncio.TimeoutError:
            pass

    async def _lazy_unmount(self, mp: str) -> None:
        fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
        if not fusermount:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                fusermount,
                "-u",
                "-z",
                mp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), 10)
        except (OSError, asyncio.TimeoutError):
            pass

    async def _warm(self, d: Drive) -> None:
        """Prefetch the first two folder levels so the file manager opens instantly."""
        try:
            st = await self.rc("vfs/stats", {"fs": d.mounted_fs}, timeout=10)
            dc = st.get("diskCache") or {}
            d.meta_path, d.data_path = (
                str(dc.get("pathMeta", "")),
                str(dc.get("path", "")),
            )
            await self.rc("vfs/refresh", {"fs": d.mounted_fs}, timeout=120)
            names = await self.fs(_top_dirs, d.mount_point, 60, timeout=20)
            if names and d.mount_state == "mounted":
                params: dict[str, Any] = {"fs": d.mounted_fs}
                for i, n in enumerate(names):
                    params[f"dir{i}"] = n
                await self.rc("vfs/refresh", params, timeout=300)
        except (RcError, OSError, asyncio.TimeoutError) as e:
            log.debug("warm-up of %s stopped: %s", d.name, e)

    # ------------------------------------------------------------------ folders kept local

    def _rebuild_folders(self) -> None:
        seen = set()
        now = time.time()
        for rname, r in self.cfg["remotes"].items():
            for spec in r.get("folders", []):
                fid = spec["id"]
                seen.add(fid)
                f = self.folders.get(fid)
                if f is None:
                    saved = self.st["folders"].get(fid, {})
                    f = Folder(
                        id=fid,
                        remote=rname,
                        path=spec["path"],
                        local=str(paths.expand(spec["local"])),
                        paused=spec.get("paused", False),
                        label=spec.get("label") or "",
                        excludes=list(spec.get("excludes") or []),
                    )
                    f.initialized = bool(saved.get("initialized"))
                    f.last_sync = float(saved.get("last_sync", 0))
                    f.last_changes = int(saved.get("last_changes", 0))
                    f.conflicts = list(saved.get("conflicts", []))[:20]
                    f.attention = str(saved.get("attention", ""))
                    f.attention_code = str(saved.get("attention_code", ""))
                    f.error = str(saved.get("error", ""))
                    f.resync_reason = str(saved.get("resync_reason", ""))
                    f.recovery_attempted = bool(saved.get("recovery_attempted"))
                    # Saved by older versions: one digest of filters.txt for every folder
                    f.filters_md5 = str(
                        saved.get("filters_md5") or self._legacy_filters_md5
                    )
                    if self.st.get("filters_changed"):
                        f.filters_md5 = "changed"
                    self._check_filters(f)
                    # Catch up shortly after start, staggered so they don't all list at once
                    f.next_due = now + 5 + len(self.folders) * 2
                    self.folders[fid] = f
                    self._watch_folder(f)
                else:
                    f.paused = spec.get("paused", False)
                    f.label = spec.get("label") or ""
                    excludes = list(spec.get("excludes") or [])
                    if excludes != f.excludes:
                        f.excludes = excludes
                        self._filters_changed(f)
        self.st.pop("filters_changed", None)
        for fid in list(self.folders):
            if fid not in seen:
                self._unwatch_folder(fid)
                self._matchers.pop(fid, None)
                del self.folders[fid]
                self.st["folders"].pop(fid, None)

    def _persist_folder(self, f: Folder) -> None:
        if f.id not in self.folders:
            return
        self.st["folders"][f.id] = {
            "initialized": f.initialized,
            "last_sync": f.last_sync,
            "last_changes": f.last_changes,
            "conflicts": f.conflicts[:20],
            "attention": f.attention,
            "attention_code": f.attention_code,
            "error": f.error,
            "filters_md5": f.filters_md5,
            "resync_reason": f.resync_reason,
            "recovery_attempted": f.recovery_attempted,
        }
        config.save_state(self.st)

    def _persist_all_folders(self) -> None:
        for f in self.folders.values():
            self.st["folders"][f.id] = {
                "initialized": f.initialized,
                "last_sync": f.last_sync,
                "last_changes": f.last_changes,
                "conflicts": f.conflicts[:20],
                "attention": f.attention,
                "attention_code": f.attention_code,
                "error": f.error,
                "filters_md5": f.filters_md5,
                "resync_reason": f.resync_reason,
                "recovery_attempted": f.recovery_attempted,
            }
        config.save_state(self.st)

    def _folder_due(self, f: Folder, now: float) -> bool:
        if f.paused or f.job_id or f.attention:
            return False
        if f.retry_at > now:
            return False
        if not f.initialized or f.resync_reason or f.force_next:
            return True
        if f.dirty_since and (
            now - f.last_event >= DEBOUNCE or now - f.dirty_since >= MAX_DIRTY_WAIT
        ):
            return True
        return now >= f.next_due

    def _folder_due_at(self, f: Folder, now: float) -> float:
        """When _folder_due turns true for f if nothing else changes (inf: not by itself)."""
        if f.paused or f.job_id or f.attention:
            return float("inf")
        if not f.initialized or f.resync_reason or f.force_next:
            at = now
        else:
            at = f.next_due
            if f.dirty_since:
                at = min(at, f.last_event + DEBOUNCE, f.dirty_since + MAX_DIRTY_WAIT)
        return max(at, f.retry_at)

    def _scheduler_sleep(self, now: float) -> float:
        """How long the scheduler can sleep before a folder is due. Folders due but held back
        (all sync slots busy, offline, drive not loaded) are looked at again every second."""
        due_at = min(
            (self._folder_due_at(f, now) for f in self.folders.values()),
            default=float("inf"),
        )
        if due_at <= now:
            return 1.0
        return min(max(due_at - now, 0.05), SCHEDULER_MAX_SLEEP)

    async def _scheduler(self) -> None:
        sleep = SCHEDULER_MAX_SLEEP
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), sleep)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            sleep = SCHEDULER_MAX_SLEEP
            if self.engine_state != "running":
                continue
            now = time.time()
            # Kept folders never overlap (see _validate_local) and each sync has its own rclone
            # process, so folders of the same drive sync side by side
            running = [f for f in self.folders.values() if f.job_id]
            # Local edits first, then never-synced folders, then routine checks
            order = sorted(
                self.folders.values(),
                key=lambda f: (not f.dirty_since, f.initialized, f.next_due),
            )
            for f in order:
                if len(running) >= MAX_SYNC_JOBS:
                    break
                if not self._folder_due(f, now):
                    continue
                # Only hold back after a real network failure: the route check alone can be wrong
                if not self.online and f.error_kind == "network":
                    if not f.waiting_network:
                        f.waiting_network = True
                        self.mark()
                    continue
                f.waiting_network = False
                if f.remote not in self.remotes:
                    continue
                try:
                    started = await self._start_sync(f)
                except Exception as e:
                    log.exception("could not start sync of %s", f.id)
                    # Shown on the folder, and retried soon: most causes are passing
                    if isinstance(e, OSError):
                        f.error = (
                            f"Can't use {e.filename or f.local}: {e.strerror or e}"
                        )
                    elif isinstance(e, asyncio.TimeoutError):
                        f.error = f"{f.local} did not respond"
                    else:
                        f.error = f"Could not start the sync: {util.clean_error(str(e)) or type(e).__name__}"
                    f.error_kind = "other"
                    f.retry_at = now + 15
                    self.mark()
                    started = False
                if started:
                    running.append(f)
            sleep = self._scheduler_sleep(time.time())

    async def _start_sync(self, f: Folder) -> bool:
        now = time.time()
        try:
            exists = await self.fs(os.path.isdir, f.local, timeout=5)
        except asyncio.TimeoutError:
            exists = False
        if not exists:
            if f.initialized:
                self._folder_attention(
                    f,
                    "missing_local",
                    f"The local folder {f.local} is missing. Restore it, or re-download with Resync.",
                )
                return False
            await self.fs(lambda p: os.makedirs(p, exist_ok=True), f.local, timeout=10)
        rtype = self.remotes.get(f.remote, {}).get("type", "")
        link = await self.fs(_ensure_link, f.id, f.local, timeout=5)
        filters_path, filters_text = self._filters_of(f)
        if f.excludes:
            await self.fs(_write_filters, filters_path, filters_text, timeout=5)
        f.run_filters_md5 = filters.digest(filters_text)
        params: dict[str, Any] = {
            "path1": providers.bisync_remote_spec(f.remote, rtype, f.path),
            "path2": link,
            "workdir": str(paths.ensure_private_dir(paths.bisync_dir(f.id))),
            "filtersFile": str(filters_path),
            "createEmptySrcDirs": True,
            "compare": "size,modtime",
            # Conflicts never lose data: the newer file wins, the other is kept as name.conflictN
            "conflictResolve": "newer",
            "conflictLoser": "num",
            "conflictSuffix": "conflict",
            "resilient": True,
            "recover": True,
            "maxLock": "5m",
            # bisync's command-line default; through rc it would otherwise abort on any deletion
            "maxDelete": 50,
            # Local files a sync overwrites or deletes are moved here instead of being lost
            "backupDir2": str(paths.backup_dir(f.id) / time.strftime("%Y-%m-%d")),
            "_async": True,
            "_group": f"guac/{f.id}",
            # notes.conflict1.md rather than notes.md.conflict1, so it still opens in the right app
            "_config": {"SuffixKeepExtension": True},
        }
        if not f.initialized or f.resync_reason:
            params["resync"] = True
            params["resyncMode"] = "newer"
        if f.force_next:
            params["force"] = True
        f.run_resync = bool(params.get("resync"))
        if f.run_resync and f.resync_reason == "recover":
            # Survives an interruption or restart between the repair and the next sync.
            f.recovery_attempted = True
            self._persist_folder(f)
        worker = await self._start_worker(f)
        try:
            res = await self.rc("sync/bisync", params, timeout=30, client=worker.rcc)
        except Exception:
            await self._stop_worker(f.id)
            raise
        f.job_id = int(res.get("jobid", 0))
        f.job_started = now
        f.progress = {}
        f.dirty_since = 0
        f.dirty_during_run = False
        f.followup_only = False
        f.force_next = False
        log.info(
            "sync %s (%s:%s) started%s",
            f.id,
            f.remote,
            f.path,
            " [resync]" if params.get("resync") else "",
        )
        # Most syncs finish within milliseconds: look again soon rather than on the next tick
        loop = asyncio.get_running_loop()
        for delay in (0.0, 0.3, 1.0):
            loop.call_later(delay, self._tick_now.set)
        self.mark()
        return bool(f.job_id)

    async def _poll_job(self, f: Folder) -> None:
        group = f"guac/{f.id}"
        job = f.job_id
        w = self.workers.get(f.id)
        lost = w is None or w.proc.returncode is not None
        if w is not None and not lost:
            try:
                js = await self.rc(
                    "job/status", {"jobid": f.job_id}, timeout=5, client=w.rcc
                )
            except RcError as e:
                lost = "not found" in str(e).lower() or isinstance(e, RcUnavailable)
                if not lost:
                    return
        if f.job_id != job:
            return  # cancelled meanwhile (removed, or shutting down)
        if lost or w is None:
            log.warning("sync %s lost its rclone process; retrying", f.id)
            await self._stop_worker(f.id)
            f.job_id, f.progress, f.moving, f.retry_at = 0, {}, [], time.time() + 5
            self.mark()
            return
        if not js.get("finished"):
            try:
                stats = await self.rc(
                    "core/stats", {"group": group}, timeout=5, client=w.rcc
                )
            except RcError:
                return
            f.moving = [str(t.get("name", "")) for t in stats.get("transferring") or []]
            current = (stats.get("transferring") or [{}])[0]
            progress = {
                "bytes": int(stats.get("bytes", 0)),
                "totalBytes": int(stats.get("totalBytes", 0)),
                "transfers": int(stats.get("transfers", 0)),
                "totalTransfers": int(stats.get("totalTransfers", 0)),
                "checks": int(stats.get("checks", 0)),
                "current": util.neutralize_controls(
                    os.path.basename(str(current.get("name", "")))
                ),
                "speed": _stats_speed(stats),
            }
            if progress != f.progress:
                f.progress = progress
                self.mark()
            return
        await self._finish_sync(f, js)

    async def _finish_sync(self, f: Folder, js: dict[str, Any]) -> None:
        group = f"guac/{f.id}"
        changes = 0
        w = self.workers.get(f.id)
        try:
            if w is not None:
                stats = await self.rc(
                    "core/stats",
                    {"group": group, "short": True},
                    timeout=5,
                    client=w.rcc,
                )
                changes = (
                    int(stats.get("transfers", 0))
                    + int(stats.get("deletes", 0))
                    + int(stats.get("renames", 0))
                )
        except RcError:
            pass
        await self._stop_worker(f.id)
        f.moving = []
        out = js.get("output")
        text = str(out.get("output", "")) if isinstance(out, dict) else ""
        err = str(js.get("error") or "")
        now = time.time()
        was_resync = f.run_resync
        rerun, f.rerun = f.rerun, False
        f.job_id, f.progress = 0, {}
        conflicts = _parse_conflicts(text)
        # A later error doesn't undo conflict copies made earlier in this run.
        if conflicts:
            f.conflicts = (conflicts + [c for c in f.conflicts if c not in conflicts])[
                :20
            ]
            self.notify(
                f"conflict:{f.id}",
                f"Sync conflict in {f.label or os.path.basename(f.local)}",
                "Kept both versions: " + ", ".join(conflicts[:3]),
            )
        if js.get("success"):
            first_sync = not f.initialized
            f.initialized, f.resync_reason = True, ""
            if not was_resync:
                f.recovery_attempted = False
            # Rules changed while this sync ran: one more resync, with the new ones
            f.filters_md5 = f.run_filters_md5
            self._check_filters(f)
            f.last_sync, f.last_changes = now, changes
            f.error = f.error_kind = f.attention = f.attention_code = ""
            f.backoff = 0
            f.next_due = now if rerun else now + self.cfg["sync_interval_min"] * 60
            if f.dirty_during_run:
                f.dirty_since = now
                f.last_event = now + FOLLOWUP_DEBOUNCE - DEBOUNCE
                f.followup_only = True
            if first_sync and not f.watching:
                self._watch_folder(f)
            log.info(
                "sync %s done: %d change(s)%s",
                f.id,
                changes,
                f" [{len(conflicts)} conflict(s)]" if conflicts else "",
            )
        else:
            critical = next(
                (
                    line
                    for line in (err + "\n" + text).splitlines()
                    if "critical error:" in line.lower()
                ),
                "",
            )
            detail = util.clean_error(critical or err or text)
            blob = (err + "\n" + critical + "\n" + text[-6000:]).lower()
            transient = _transient_failure(blob)
            log.warning(
                "sync %s failed: %s%s",
                f.id,
                detail,
                f" ({transient})" if transient else "",
            )
            if (
                "run with --force" in blob
                or "too many deletes" in blob
                or "safety abort" in blob
            ):
                self._folder_attention(
                    f,
                    "safety",
                    "Safety stop: most files changed or were deleted on one side. "
                    + (
                        "Check both copies, then choose Resync (restores, never deletes)."
                        if f.resync_reason or not f.initialized
                        else "Check both copies, then choose Sync anyway (applies it) or Resync (restores, never deletes)."
                    ),
                )
            elif "cannot sync to an empty directory" in blob:
                self._folder_attention(
                    f,
                    "empty",
                    "One side is empty, so nothing was synced. "
                    "Resync restores files to the empty side."
                    + (
                        ""
                        if f.resync_reason or not f.initialized
                        else " Sync anyway would empty the other side too."
                    ),
                )
            elif "file name too long" in blob:
                self._folder_attention(
                    f,
                    "too_long",
                    "The paths are too long for bisync's state files. "
                    "Keep a folder higher up, or use a shorter local path.",
                )
            elif "prior lock file found" in blob:
                f.retry_at = now + 90
                f.error, f.error_kind = (
                    "Waiting for a previous sync to release its lock",
                    "lock",
                )
            elif "directory not found" in blob and f.path and f.remote.lower() in blob:
                self._folder_attention(
                    f,
                    "remote_missing",
                    f"The cloud folder '{f.path}' was not found. It may have been moved or deleted.",
                )
            else:
                kind = transient or util.classify_error(blob)
                if kind == "auth":
                    self._folder_attention(
                        f,
                        "auth",
                        "Sign-in expired or was revoked. Reconnect this account.",
                    )
                    if "must run --resync" in blob:
                        f.resync_reason = f.resync_reason or "recover"
                elif kind not in ("network", "rate") and (
                    was_resync
                    or "must run --resync" in blob
                    or "cannot find prior" in blob
                    or "md5 hash not found" in blob
                ):
                    if was_resync or f.recovery_attempted:
                        self._folder_attention(
                            f,
                            "resync_failed",
                            "Could not repair the sync state: "
                            + detail
                            + ". Check both copies, then choose Resync to try again. Nothing will be deleted.",
                        )
                    else:
                        f.resync_reason, f.retry_at = "recover", now + 2
                        f.error = f.error_kind = ""
                else:
                    if f.initialized and "must run --resync" in blob:
                        # The cause is passing, but the next attempt still needs listings.
                        f.resync_reason = f.resync_reason or "recover"
                    if kind == "rate":
                        f.backoff = min(max(f.backoff * 2, 120.0), 1800.0)
                        detail = self._shared_client_note(f.remote) or detail
                    elif kind == "network":
                        f.backoff = min(max(f.backoff * 2, 30.0), 600.0)
                    elif "retryable" in blob or "please try again" in blob:
                        f.backoff = 30.0
                    else:
                        f.backoff = min(max(f.backoff * 2, 60.0), 1800.0)
                    f.retry_at = now + f.backoff
                    f.error, f.error_kind = detail, kind
        self._persist_folder(f)
        self._recent_due = now + 1
        self.mark()
        self.wake()

    def _folder_attention(self, f: Folder, code: str, message: str) -> None:
        f.attention, f.attention_code = message, code
        self._persist_folder(f)
        self.notify(
            f"attention:{f.id}:{code}",
            f"{f.label or os.path.basename(f.local)} needs attention",
            message,
            "critical",
        )
        self.mark()

    # ------------------------------------------------------------------ inotify

    def _init_inotify(self) -> None:
        try:
            self.inotify = Inotify()
        except OSError as e:
            log.warning(
                "inotify unavailable (%s); folders kept local sync on a timer only", e
            )
            self.inotify = None
            return
        asyncio.get_running_loop().add_reader(self.inotify.fd, self._on_inotify)

    def _watch_folder(self, f: Folder) -> None:
        self._spawn(self._add_watches(f.id, f.local))

    async def _add_watches(self, fid: str, top: str) -> None:
        f = self.folders.get(fid)
        if f is None or self.inotify is None:
            if f is not None:
                f.watch_error = "File watching is unavailable; syncing on a timer only"
            return
        try:
            matcher, local = self._matcher(f), f.local
            dirs = await self.fs(
                _collect_dirs,
                top,
                MAX_WATCH_DIRS,
                lambda d: matcher.ignored(_relative(d, local), True),
                timeout=120,
            )
        except (asyncio.TimeoutError, OSError):
            return
        wds = self._folder_wds.setdefault(fid, set())
        for d in dirs:
            if fid not in self.folders:
                return
            try:
                wd = self.inotify.add_watch(d)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    f.watch_error = "Too many folders to watch (raise fs.inotify.max_user_watches); syncing on a timer"
                    self.mark()
                    break
                continue
            self._wd[wd] = (fid, d)
            wds.add(wd)
        was = f.watching
        f.watching = bool(wds)
        if f.watching != was:
            self.mark()

    def _unwatch_folder(self, fid: str) -> None:
        for wd in self._folder_wds.pop(fid, set()):
            self._wd.pop(wd, None)
            if self.inotify is not None:
                try:
                    self.inotify.rm_watch(wd)
                except OSError:
                    pass

    def _on_inotify(self) -> None:
        if self.inotify is None:
            return
        try:
            events = self.inotify.read()
        except OSError:
            return
        now = time.time()
        touched: set[str] = set()
        for ev in events:
            if ev.mask & IN_Q_OVERFLOW:
                for f in self.folders.values():
                    self._mark_folder_dirty(f, now)
                continue
            entry = self._wd.get(ev.wd)
            if entry is None:
                continue
            fid, dirpath = entry
            if ev.mask & IN_IGNORED:
                self._wd.pop(ev.wd, None)
                self._folder_wds.get(fid, set()).discard(ev.wd)
                continue
            f = self.folders.get(fid)
            if f is None:
                continue
            if ev.name and _IGNORED_EVENT_RE.match(ev.name):
                continue
            # Never-synced files and folders never wake the syncer either
            if ev.name and self._matcher(f).ignored(
                _relative(os.path.join(dirpath, ev.name), f.local),
                bool(ev.mask & IN_ISDIR),
            ):
                continue
            if ev.mask & IN_ISDIR and ev.mask & (IN_CREATE | IN_MOVED_TO):
                self._spawn(self._add_watches(fid, os.path.join(dirpath, ev.name)))
            if ev.mask & (IN_DELETE_SELF | IN_MOVE_SELF) and dirpath == f.local:
                f.watching = False
            if ev.mask & (IN_CLOSE_WRITE | IN_MOVED_TO) and not ev.mask & IN_ISDIR:
                self._recent_due = min(self._recent_due or now + 2, now + 2)
            touched.add(fid)
            self._mark_folder_dirty(f, now)
        if touched:
            self.wake()

    def _mark_folder_dirty(self, f: Folder, now: float) -> None:
        if f.job_id:
            f.dirty_during_run = True
            return
        if not f.dirty_since or f.followup_only:
            f.dirty_since = now
            f.followup_only = False
            self.mark()
        f.last_event = now

    # ------------------------------------------------------------------ periodic work

    async def _ticker(self) -> None:
        while not self._stop.is_set():
            active = (
                any(f.job_id for f in self.folders.values())
                or self.transfers.get("count", 0) > 0
                or any(d.uploads for d in self.drives.values())
            )
            interval = (
                TICK_ACTIVE
                if active
                else (TICK_IDLE if self.clients else TICK_BACKGROUND)
            )
            try:
                await asyncio.wait_for(self._tick_now.wait(), interval)
            except asyncio.TimeoutError:
                pass
            self._tick_now.clear()
            if self._stop.is_set():
                return
            try:
                await self._tick()
            except Exception:
                log.exception("tick failed")

    async def _tick(self) -> None:
        now = time.time()
        offset = self._boottime_offset()
        if offset - self._boot_offset > 20:
            log.info("resumed from suspend")
            self._boot_offset = offset
            self._on_resume()
        online = util.has_default_route()
        if online != self.online:
            self.online = online
            log.info("network %s", "up" if online else "down")
            if online:
                for f in self.folders.values():
                    f.waiting_network = False
                self._on_resume()
            self.mark()
        if self.engine_state != "running":
            return
        if self._rclone_conf_signature() != self._rclone_conf_sig:
            await self.load_remotes()

        # Only drives already mounted before the listing can have "disappeared" from it: one that
        # finishes mounting while we look would otherwise be reset by a stale snapshot
        mounted_before = {
            name: d.mount_point
            for name, d in self.drives.items()
            if d.mount_state == "mounted"
        }
        # What the engine knows, asked in one request. With nothing mounted there is nothing
        # to lose and nothing streaming, so no need to ask rclone at all.
        calls: list[tuple[str, dict[str, Any]]] = []
        if mounted_before:
            calls.append(("mount/listmounts", {}))
        vfs_fs = {
            name: self.drives[name].mounted_fs
            for name in mounted_before
            if self.drives[name].mounted_fs
        }
        calls += [("vfs/stats", {"fs": fs}) for fs in vfs_fs.values()]
        if self._engine_streaming():
            calls.append(("core/stats", {}))
        try:
            replies = await self.rc_batch(calls) if calls else []
        except RcError:
            return
        mounts_reply = replies.pop(0) if mounted_before else {}
        if isinstance(mounts_reply, RcError):
            return
        vfs_replies = dict(zip(vfs_fs, replies))
        stats_reply = replies[len(vfs_fs)] if len(replies) > len(vfs_fs) else None
        ours: set[Any] = set()
        kernel: dict[str, dict[str, str]] = {}
        if mounted_before:
            ours = {m.get("MountPoint") for m in mounts_reply.get("mountPoints") or []}
            kernel = util.read_mountinfo()
        for d in list(self.drives.values()):
            if (
                d.mount_state == "mounted"
                and mounted_before.get(d.name) == d.mount_point
                and (d.mount_point not in ours or d.kernel_path not in kernel)
            ):
                log.warning("mount of %s disappeared", d.name)
                if d.mount_point in ours:
                    try:
                        await self.rc(
                            "mount/unmount", {"mountPoint": d.mount_point}, timeout=10
                        )
                    except RcError:
                        pass
                d.mount_state, d.retry_at = "off", 0
                self.mark()
            if d.mount_state == "mounted":
                try:
                    st = vfs_replies.get(d.name)
                    if st is None or vfs_fs.get(d.name) != d.mounted_fs:
                        st = await self.rc("vfs/stats", {"fs": d.mounted_fs}, timeout=5)
                    elif isinstance(st, RcError):
                        raise st
                    dc = st.get("diskCache") or {}
                    uploads = int(dc.get("uploadsInProgress", 0)) + int(
                        dc.get("uploadsQueued", 0)
                    )
                    if (
                        uploads,
                        int(dc.get("bytesUsed", 0)),
                        int(dc.get("files", 0)),
                    ) != (d.uploads, d.cache_bytes, d.cache_files):
                        d.uploads, d.cache_bytes, d.cache_files = (
                            uploads,
                            int(dc.get("bytesUsed", 0)),
                            int(dc.get("files", 0)),
                        )
                        self.mark()
                    if not d.meta_path:
                        d.meta_path, d.data_path = (
                            str(dc.get("pathMeta", "")),
                            str(dc.get("path", "")),
                        )
                except RcError:
                    pass
            want = (
                bool(self.remote_cfg(d.name).get("stream")) and d.name in self.remotes
            )
            if (
                want
                and d.mount_state in ("off", "waiting", "error")
                and d.retry_at <= now
            ) or (
                not want
                and d.mount_state in ("mounted", "error", "waiting")
                and d.retry_at <= now
            ):
                self._spawn(self.reconcile_drive(d.name))

        for f in list(self.folders.values()):
            if f.job_id:
                await self._poll_job(f)

        try:
            # The engine moves streamed files, so only while a drive is mounted; syncs report
            # through their own processes
            moving: list[str] = []
            speed = 0
            if self._engine_streaming():
                stats = stats_reply
                if stats is None:
                    stats = await self.rc("core/stats", {}, timeout=5)
                elif isinstance(stats, RcError):
                    raise stats
                moving = [
                    str(t.get("name", "")) for t in stats.get("transferring") or []
                ]
                speed = _stats_speed(stats)
            for f in self.folders.values():
                if f.job_id:
                    moving += f.moving
                    speed += int(f.progress.get("speed", 0) or 0)
            transfers = {
                "count": len(moving),
                "speed": speed,
                "names": [
                    util.neutralize_controls(os.path.basename(n)) for n in moving[:3]
                ],
            }
            if transfers != self.transfers:
                self.transfers = transfers
                self.mark()
        except RcError:
            pass

    def _engine_streaming(self) -> bool:
        return any(
            d.mount_state in ("mounting", "mounted", "unmounting")
            for d in self.drives.values()
        )

    def _on_resume(self) -> None:
        now = time.time()
        for d in self.drives.values():
            if (
                d.mount_state in ("waiting", "error")
                and d.error_kind != "auth"
                and d.retry_at != float("inf")
            ):
                d.retry_at = now + 3
                d.backoff = 0
        for f in self.folders.values():
            if not f.attention:
                f.retry_at = min(f.retry_at, now + 10) if f.retry_at else 0
                f.next_due = min(f.next_due, now + 10)
                f.backoff = 0
        for name in self.drives:
            q = self.st["quota"].get(name)
            if q:
                q["at"] = 0
        self.wake()

    async def _slow_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(5)
            if self.engine_state != "running":
                continue
            now = time.time()
            try:
                if now >= self._recent_due and (
                    now - self._recent_at >= RECENT_TTL or self._recent_due
                ):
                    await self._refresh_recent()
                if self.online:
                    await self._refresh_quotas(now)
                # Read and hashed again only when the file was touched
                sig = self._filters_signature()
                if sig != self._filters_sig:
                    self._filters_sig = sig
                    self._global_filters_changed()
                if now - self._backups_pruned > 6 * 3600:
                    self._backups_pruned = now
                    await self.fs(
                        _prune_backups,
                        str(paths.backup_root()),
                        int(self.cfg.get("backup_days", 30)),
                        timeout=120,
                    )
                count = await self.fs(
                    _count_files, str(paths.backup_root()), timeout=20
                )
                if count != self.backup_files:
                    self.backup_files = count
                    self.mark()
            except Exception:
                log.exception("maintenance failed")

    def _filters_signature(self) -> Any:
        try:
            st = paths.filters_file().stat()
            return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except OSError:
            return None

    def _read_global_filters(self) -> None:
        try:
            self._global_filters = paths.filters_file().read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            self._global_filters = ""
        self._global_md5 = filters.digest(self._global_filters)

    def _global_filters_changed(self) -> None:
        """filters.txt was edited, here or by hand: folders without their own list resync."""
        old = self._global_md5
        self._read_global_filters()
        if self._global_md5 == old:
            return
        log.info("filters changed: next syncs of folders using them are resyncs")
        self.st["filters_md5"] = self._global_md5
        for f in self.folders.values():
            if not f.excludes:
                self._filters_changed(f)
        self._persist_all_folders()

    def _filters_of(self, f: Folder) -> tuple[Path, str]:
        """The filter file f syncs with and the rules in it: its own list, else filters.txt."""
        if f.excludes:
            return paths.folder_filters_file(f.id), filters.override_text(f.excludes)
        return paths.filters_file(), self._global_filters

    def _matcher(self, f: Folder) -> filters.Matcher:
        # Asked for every file event: rebuilt only when the folder's rules change
        key = "\n".join(f.excludes) if f.excludes else self._global_md5
        cached = self._matchers.get(f.id)
        if cached is None or cached[0] != key:
            cached = (key, filters.Matcher(self._filters_of(f)[1]))
            self._matchers[f.id] = cached
        return cached[1]

    def _check_filters(self, f: Folder) -> None:
        """A folder whose rules differ from its last sync's resyncs once: newer wins, nothing
        is deleted, backups are kept. Files newly excluded stay where they are on both sides;
        files no longer excluded are brought over."""
        md5 = filters.digest(self._filters_of(f)[1])
        if not f.initialized or not f.filters_md5:
            f.filters_md5 = f.filters_md5 or md5
            return
        if md5 != f.filters_md5:
            f.resync_reason = f.resync_reason or "filters"
        elif f.resync_reason == "filters":
            # Changed and changed back before any sync used it
            f.resync_reason = ""

    def _filters_changed(self, f: Folder) -> None:
        self._check_filters(f)
        # Folders newly synced (or no longer) are watched (or not) from now on
        if f.watching or f.id in self._folder_wds:
            self._unwatch_folder(f.id)
            self._watch_folder(f)
        self.mark()

    async def _refresh_quotas(self, now: float, only: str | None = None) -> None:
        for name in list(self.remotes):
            if only and name != only:
                continue
            q = self.st["quota"].get(name) or {}
            if q.get("unsupported") and now - q.get("at", 0) < 24 * 3600:
                continue
            if not only and now - q.get("at", 0) < QUOTA_TTL:
                continue
            d = self.drives.get(name)
            if d is None or d.quota_busy:
                continue
            d.quota_busy = True
            try:
                about = await self.rc(
                    "operations/about", {"fs": f"{name}:"}, timeout=30
                )
                total, used = (
                    int(about.get("total", 0) or 0),
                    int(about.get("used", 0) or 0),
                )
                free = int(about.get("free", 0) or 0)
                if 0 < total < (1 << 50):
                    self.st["quota"][name] = {
                        "total": total,
                        "used": used,
                        "free": free,
                        "at": now,
                    }
                elif used:
                    self.st["quota"][name] = {
                        "total": 0,
                        "used": used,
                        "free": free,
                        "at": now,
                    }
                else:
                    self.st["quota"][name] = {"unsupported": True, "at": now}
                config.save_state(self.st)
                self.mark()
            except RcError as e:
                msg = str(e).lower()
                if "doesn't support about" in msg or "not supported" in msg:
                    self.st["quota"][name] = {"unsupported": True, "at": now}
                else:
                    # Keep what we had; try again in a few minutes
                    q["at"] = now - QUOTA_TTL + 300
                    self.st["quota"][name] = q
            finally:
                d.quota_busy = False

    async def _refresh_recent(self) -> None:
        drives = [
            (d.meta_path, d.data_path, d.mount_point, d.name)
            for d in self.drives.values()
            if d.mount_state == "mounted" and d.meta_path
        ]
        folders = [
            (f.local, f.remote, f.path) for f in self.folders.values() if f.initialized
        ]

        def scan() -> list[dict[str, Any]]:
            groups = [
                recent.from_vfs_cache(m, dp, mp, n, RECENT_LIMIT)
                for m, dp, mp, n in drives
            ]
            groups += [
                recent.from_local_folder(l, r, p, RECENT_LIMIT) for l, r, p in folders
            ]
            return recent.merge(groups, RECENT_LIMIT)

        try:
            items = await self.fs(scan, timeout=30)
        except (asyncio.TimeoutError, OSError):
            return
        self._recent_at, self._recent_due = time.time(), 0.0
        for item in items:
            item["name"] = util.neutralize_controls(item["name"])
            item["folder"] = util.neutralize_controls(item["folder"])
        if items != self.recent:
            self.recent = items
            self.mark()

    # ------------------------------------------------------------------ state for clients

    def _folder_default_name(self, f: Folder) -> str:
        if f.path:
            return os.path.basename(f.path)
        return self.remote_cfg(f.remote).get("label") or f.remote

    def _folder_state(self, f: Folder, now: float) -> dict[str, Any]:
        if f.job_id:
            state = "syncing"  # also while paused: the sync under way still ends
        elif f.paused:
            state = "paused"
        elif f.attention:
            state = "attention"
        elif f.waiting_network:
            state = "waiting"
        elif f.error and f.retry_at > now:
            state = "error"
        elif not f.initialized or f.resync_reason:
            state = "pending"
        elif f.dirty_since and not f.followup_only:
            state = "changes"
        else:
            state = "synced"
        return {
            "id": f.id,
            "remote": f.remote,
            "path": f.path,
            # The alias when set, else the folder's own name
            "name": f.label or self._folder_default_name(f),
            "label": f.label,
            "defaultName": self._folder_default_name(f),
            "local": f.local,
            "state": state,
            "paused": f.paused,
            # Local edits not synced yet: while paused, what resuming will catch up
            "localChanges": bool(f.dirty_since and not f.followup_only),
            "initialized": f.initialized,
            "lastSync": int(f.last_sync),
            "lastChanges": f.last_changes,
            "nextSync": int(max(f.next_due, f.retry_at)) if not f.job_id else 0,
            "error": f.error,
            "errorKind": f.error_kind,
            "attention": f.attention,
            "attentionCode": f.attention_code,
            "resync": f.run_resync
            if f.job_id
            else bool(f.resync_reason) or not f.initialized,
            "progress": f.progress,
            "conflicts": f.conflicts[:5],
            "conflictCount": len(f.conflicts),
            "watching": f.watching,
            "watchError": f.watch_error,
            # Its own never-synced patterns; empty: the global list
            "excludes": f.excludes,
        }

    def build_state(self) -> dict[str, Any]:
        now = time.time()
        drives = []
        folder_count = syncing = attention = uploads = mounted = streaming = 0
        for name in sorted(self.remotes, key=str.lower):
            r = self.remotes[name]
            c = self.remote_cfg(name)
            d = self.drives.get(name) or Drive(name)
            prov = r["provider"]
            q = self.st["quota"].get(name) or {}
            total, used = int(q.get("total", 0)), int(q.get("used", 0))
            folders = [
                self._folder_state(f, now)
                for f in self.folders.values()
                if f.remote == name
            ]
            folders.sort(key=lambda x: x["path"].lower())
            folder_count += len(folders)
            syncing += sum(1 for x in folders if x["state"] == "syncing")
            attention += sum(
                1
                for x in folders
                if x["attention"] or x["state"] == "error" or x["conflictCount"]
            )
            attention += 1 if d.mount_state in ("error", "foreign") else 0
            uploads += d.uploads
            mounted += 1 if d.mount_state == "mounted" else 0
            streaming += 1 if c.get("stream") else 0
            drives.append(
                {
                    "name": name,
                    "label": c.get("label") or name,
                    "type": r["type"],
                    "provider": prov["name"],
                    "providerId": prov["id"],
                    "glyph": prov["glyph"],
                    "color": prov["color"],
                    "stream": bool(c.get("stream")),
                    "mountState": d.mount_state,
                    "mounted": d.mount_state == "mounted",
                    "mountPath": str(self.desired_mount_point(name)),
                    "customPath": bool(c.get("mount_path")),
                    "error": d.error,
                    "errorKind": d.error_kind,
                    "retrying": now < d.retry_at < float("inf"),
                    "quotaKnown": total > 0,
                    "quotaTotal": total,
                    "quotaUsed": used,
                    "quotaFree": int(q.get("free", 0)),
                    "quotaPercent": round(used / total * 100, 1) if total else 0,
                    "uploads": d.uploads,
                    "cacheBytes": d.cache_bytes,
                    "warnings": providers.visible_warnings(
                        r["warnings"], c.get("hidden_hints") or []
                    ),
                    "customClient": r["custom_client"],
                    "canCustomClient": r["type"] in providers.CLIENT_GUIDES,
                    "folders": folders,
                }
            )
        return {
            "version": __version__,
            "engine": {
                "state": self.engine_state,
                "error": self.engine_error,
                "version": self.rclone_version,
            },
            "online": self.online,
            "mountRoot": str(self.mount_root()),
            "localRoot": str(self.local_root()),
            "cloudRoot": str(self.cloud_root()),
            "settings": {k: self.cfg[k] for k in config.SETTABLE},
            "drives": drives,
            "summary": {
                "drives": len(drives),
                "streaming": streaming,
                "mounted": mounted,
                "folders": folder_count,
                "syncing": syncing,
                "attention": attention,
                "uploads": uploads,
            },
            "transfers": self.transfers,
            "recent": self.recent,
            "auth": self.auth,
            "paths": {
                "log": str(paths.daemon_log()),
                "rcloneLog": str(paths.rclone_log()),
                "filters": str(paths.filters_file()),
                "config": str(paths.config_file()),
                "backups": str(paths.backup_root()),
            },
            "backupFiles": self.backup_files,
            # The never-synced patterns in filters.txt, and how many other rules it has
            "filters": {
                "excludes": filters.excludes_of(self._global_filters),
                "otherRules": filters.other_rules(self._global_filters),
            },
        }

    def _state_line(self) -> str:
        return json.dumps(
            {"type": "state", "data": self.build_state()}, separators=(",", ":")
        )

    async def _broadcaster(self) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            await asyncio.sleep(0.08)  # coalesce bursts of changes into one update
            self._dirty.clear()
            if not self.clients:
                continue
            line = self._state_line()
            if line == self._last_state_line:
                continue
            self._last_state_line = line
            for c in list(self.clients):
                c.send_line(line)

    # ------------------------------------------------------------------ client protocol

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("socket")
        try:
            creds = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", creds)
            if uid != os.getuid():
                writer.close()
                return
        except OSError:
            writer.close()
            return
        client = Client(writer)
        self.clients.add(client)
        client.send_line(self._state_line())
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError, ConnectionError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._spawn(self._dispatch(client, msg))
        finally:
            self.clients.discard(client)
            writer.close()

    async def _dispatch(self, client: Client, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        cmd = str(msg.get("cmd", ""))
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        handler = (
            getattr(self, "cmd_" + cmd.replace("-", "_"), None)
            if re.fullmatch(r"[a-z_-]+", cmd)
            else None
        )
        if handler is None:
            client.send(
                {
                    "type": "reply",
                    "id": mid,
                    "ok": False,
                    "error": f"Unknown command: {cmd}",
                }
            )
            return
        try:
            result = await handler(args) or {}
            reply = {
                "type": "reply",
                "id": mid,
                "ok": True,
                "data": result,
                "message": result.get("message", ""),
            }
        except UserError as e:
            reply = {
                "type": "reply",
                "id": mid,
                "ok": False,
                "error": str(e),
                "code": e.code,
            }
        except RcError as e:
            reply = {
                "type": "reply",
                "id": mid,
                "ok": False,
                "error": util.clean_error(str(e)),
            }
        except (KeyError, TypeError, ValueError) as e:
            reply = {
                "type": "reply",
                "id": mid,
                "ok": False,
                "error": f"Bad request: {e}",
            }
        except Exception as e:
            log.exception("command %s failed", cmd)
            reply = {
                "type": "reply",
                "id": mid,
                "ok": False,
                "error": f"Internal error: {e}",
            }
        client.send(reply)
        self.mark()

    def _need_engine(self) -> None:
        if self.engine_state != "running":
            raise UserError(
                self.engine_error or "rclone is starting, try again in a moment"
            )

    def _drive(self, args: dict[str, Any]) -> Drive:
        name = str(args.get("remote", ""))
        if name not in self.remotes or name not in self.drives:
            raise UserError(f"No drive named '{name}'")
        return self.drives[name]

    def _folder(self, args: dict[str, Any]) -> Folder:
        f = self.folders.get(str(args.get("id", "")))
        if f is None:
            raise UserError("That folder is no longer kept local")
        return f

    # --- general

    async def cmd_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

    async def cmd_state(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"state": self.build_state()}

    async def cmd_touch(self, args: dict[str, Any]) -> dict[str, Any]:
        """The panel opened: freshen cheap things now, stale quotas in the background."""
        now = time.time()
        if now - self._recent_at > 10:
            self._recent_due = now
        for name in self.remotes:
            q = self.st["quota"].get(name)
            if q and now - q.get("at", 0) > 10 * 60 and not q.get("unsupported"):
                q["at"] = 0
        return {}

    async def cmd_shutdown(self, args: dict[str, Any]) -> dict[str, Any]:
        asyncio.get_running_loop().call_later(0.2, self._stop.set)
        return {"message": "Stopping"}

    async def cmd_restart_engine(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.engine and self.engine.returncode is None:
            self.engine.terminate()
        return {"message": "Restarting rclone"}

    async def cmd_log(self, args: dict[str, Any]) -> dict[str, Any]:
        lines = max(10, min(500, int(args.get("lines", 80))))
        source = str(args.get("source") or "rclone")
        if source not in ("rclone", "daemon"):
            raise UserError("The log source is rclone or daemon")
        path = paths.rclone_log() if source == "rclone" else paths.daemon_log()
        tail = await asyncio.get_running_loop().run_in_executor(
            self.rc_pool, util.tail_lines, path, lines
        )
        return {"lines": tail, "path": str(path)}

    async def cmd_set_setting(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key", ""))
        if key not in config.SETTABLE:
            raise UserError(f"Unknown setting '{key}'")
        try:
            value = config.coerce(key, args.get("value"))
        except ValueError as e:
            raise UserError(str(e))
        if key in ("mount_root", "local_root"):
            root = paths.expand(value)
            if str(root) in ("/", str(Path.home())) or any(
                paths.is_within(root, Path(s)) for s in _SYSTEM_DIRS
            ):
                raise UserError("Pick a dedicated folder for that")
            other_key = "local_root" if key == "mount_root" else "mount_root"
            if paths.overlaps(root, paths.expand(self.cfg[other_key])):
                raise UserError(
                    "The stream and sync folders must be separate (neither inside the other)"
                )
            if key == "mount_root":
                for f in self.folders.values():
                    if paths.overlaps(Path(f.local), root):
                        raise UserError(
                            f"{f.local} (kept local) can't be inside the streaming folder"
                        )
        old_local = str(self.local_root())
        old_points = {
            name: str(self.desired_mount_point(name))
            for name, d in self.drives.items()
            if d.mount_state != "mounted"
        }
        self.cfg[key] = value
        self.save_cfg()
        message = "Saved"
        if key == "mount_root":
            # Mounted drives move (and leave nothing behind); unmounted ones only leave their
            # empty folder at the old location
            for name, old in old_points.items():
                if old != str(self.desired_mount_point(name)):
                    try:
                        await self.fs(_remove_empty_dir, old, timeout=5)
                    except asyncio.TimeoutError:
                        pass
            for name in self.drives:
                self._spawn(self.reconcile_drive(name, force=True))
        elif key == "local_root":
            # Folders already kept stay where they are; an empty old root goes
            if old_local != str(self.local_root()):
                try:
                    await self.fs(_remove_empty_dir, old_local, timeout=5)
                except asyncio.TimeoutError:
                    pass
            await self._ensure_local_root()
        elif key == "sync_interval_min":
            for f in self.folders.values():
                if f.initialized:
                    f.next_due = min(f.next_due, f.last_sync + value * 60)
        elif key.startswith("cache_"):
            message = "Saved; applies the next time each drive is mounted"
        self.mark()
        return {"message": message}

    # --- streaming

    async def cmd_stream(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        on = bool(args.get("on", True))
        rc = self.remote_cfg(d.name)
        rc["stream"] = on
        self.save_cfg()
        if not on and d.mount_state == "mounted":
            async with d.lock:
                try:
                    await self._unmount(d, force=bool(args.get("force")))
                except UserError:
                    rc["stream"] = True
                    self.save_cfg()
                    raise
            return {"message": f"Stopped streaming {d.name}"}
        if on:
            d.retry_at, d.backoff = 0, 0
        await self.reconcile_drive(d.name, force=True, wait=True)
        if on and d.mount_state != "mounted":
            raise UserError(d.error or f"Could not mount {d.name}", code=d.error_kind)
        return {
            "message": f"Streaming {d.name} at {d.mount_point}"
            if on
            else f"{d.name} is not streaming"
        }

    async def cmd_stream_all(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        on = bool(args.get("on", True))
        names = list(self.remotes)
        for name in names:
            self.remote_cfg(name)["stream"] = on
            if on:
                self.drives[name].retry_at = 0
        self.save_cfg()
        results = await asyncio.gather(
            *(
                self.cmd_stream({"remote": n, "on": on, "force": args.get("force")})
                for n in names
            ),
            return_exceptions=True,
        )
        failed = [
            f"{n}: {r}" for n, r in zip(names, results) if isinstance(r, Exception)
        ]
        if failed:
            raise UserError("; ".join(failed))
        return {
            "message": (
                "Streaming all drives" if on else "Stopped streaming all drives"
            )
        }

    async def cmd_retry(self, args: dict[str, Any]) -> dict[str, Any]:
        d = self._drive(args)
        d.retry_at, d.backoff = 0, 0
        if d.mount_state in ("error", "waiting"):
            d.mount_state = "off"
        await self.reconcile_drive(d.name, force=True, wait=True)
        return {"message": d.error or f"{d.name}: {d.mount_state}"}

    async def cmd_takeover(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        mp = await self.fs(
            _kernel_path, str(self.desired_mount_point(d.name)), timeout=5
        )
        fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
        if fusermount:
            proc = await asyncio.create_subprocess_exec(
                fusermount,
                "-u",
                mp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), 15)
            if proc.returncode != 0:
                await self._lazy_unmount(mp)
        d.mount_state, d.retry_at = "off", 0
        self.remote_cfg(d.name)["stream"] = True
        self.save_cfg()
        await self.reconcile_drive(d.name, force=True, wait=True)
        if d.mount_state != "mounted":
            raise UserError(d.error or "Could not take over the mount")
        return {"message": f"{d.name} is now streamed by Guacamole"}

    async def cmd_set_drive(self, args: dict[str, Any]) -> dict[str, Any]:
        d = self._drive(args)
        rc = self.remote_cfg(d.name)
        if "label" in args:
            label = str(args.get("label") or "").strip()
            rc["label"] = label if label and label != d.name else None
        if "mount_path" in args:
            raw = str(args.get("mount_path") or "").strip()
            if raw:
                mp = paths.expand(raw)
                if str(mp) in ("/", str(Path.home())) or any(
                    paths.is_within(mp, Path(s)) for s in _SYSTEM_DIRS
                ):
                    raise UserError("Pick a dedicated empty folder to mount into")
                for f in self.folders.values():
                    if paths.overlaps(Path(f.local), mp):
                        raise UserError(
                            f"{f.local} (kept local) can't overlap a mount folder"
                        )
                for other in self.drives:
                    if other != d.name and paths.overlaps(
                        self.desired_mount_point(other), mp
                    ):
                        raise UserError(f"That overlaps where {other} is mounted")
                rc["mount_path"] = None if mp == self.mount_root() / d.name else raw
            else:
                rc["mount_path"] = None
        self.save_cfg()
        if d.mount_state == "mounted" and d.mount_point != str(
            self.desired_mount_point(d.name)
        ):
            async with d.lock:
                await self._unmount(d, force=False)
            await self.reconcile_drive(d.name, force=True)
        self.mark()
        return {"message": "Saved"}

    # --- browsing and folders kept local

    async def cmd_browse(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        path = clean_remote_path(args.get("path", ""))
        dirs: list[str] | None = None
        if d.mount_state == "mounted":
            try:
                dirs = await self.fs(
                    _list_dirs,
                    os.path.join(d.mount_point, path),
                    MAX_BROWSE_ENTRIES,
                    timeout=20,
                )
            except (OSError, asyncio.TimeoutError):
                dirs = None
        if dirs is None:
            res = await self.rc(
                "operations/list",
                {
                    "fs": f"{d.name}:",
                    "remote": path,
                    "opt": {"dirsOnly": True, "noModTime": True, "noMimeType": True},
                },
                timeout=60,
            )
            dirs = sorted(
                (
                    str(i.get("Name", ""))
                    for i in res.get("list") or []
                    if i.get("IsDir") and not str(i.get("Name", "")).startswith(".")
                ),
                key=str.lower,
            )
            dirs = dirs[:MAX_BROWSE_ENTRIES]
        kept = {f.path: f.id for f in self.folders.values() if f.remote == d.name}
        entries = []
        for n in dirs:
            p = f"{path}/{n}" if path else n
            inside = next(
                (
                    fid
                    for kp, fid in kept.items()
                    if kp == "" or p == kp or p.startswith(kp + "/")
                ),
                None,
            )
            contains = any(kp.startswith(p + "/") for kp in kept)
            entries.append(
                {
                    "name": util.neutralize_controls(n),
                    "path": p,
                    "kept": inside is not None,
                    "containsKept": contains,
                }
            )
        here = next(
            (
                fid
                for kp, fid in kept.items()
                if kp == "" or path == kp or path.startswith(kp + "/")
            ),
            None,
        )
        return {
            "remote": d.name,
            "path": path,
            "dirs": entries,
            "keptHere": here is not None,
            "containsKept": any(
                kp.startswith(path + "/") or (path == "" and kp) for kp in kept
            ),
            "suggestedLocal": str(paths.expand(self.cfg["local_root"]) / d.name / path)
            if path
            else str(paths.expand(self.cfg["local_root"]) / d.name),
            "truncated": len(dirs) >= MAX_BROWSE_ENTRIES,
        }

    async def cmd_folder_size(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        path = clean_remote_path(args.get("path", ""))
        rtype = self.remotes[d.name]["type"]
        res = await self.rc(
            "operations/size",
            {"fs": providers.bisync_remote_spec(d.name, rtype, path)},
            timeout=float(args.get("timeout", 60)),
        )
        return {"count": int(res.get("count", 0)), "bytes": int(res.get("bytes", 0))}

    def _validate_local(
        self, local: Path, remote: str, rpath: str, exclude: str = ""
    ) -> None:
        home = Path.home()
        if str(local) in ("/", str(home)) or any(
            paths.is_within(local, Path(s)) for s in _SYSTEM_DIRS
        ):
            raise UserError(
                "Pick a dedicated folder for this, not your whole home or a system folder"
            )
        if paths.overlaps(local, self.mount_root()):
            raise UserError(
                f"The local copy can't be inside the streaming folder {self.mount_root()}"
            )
        for name in self.drives:
            if paths.overlaps(local, self.desired_mount_point(name)):
                raise UserError(f"The local copy can't overlap where {name} is mounted")
        for p in (paths.state_dir(), paths.config_dir()):
            if paths.overlaps(local, p):
                raise UserError(
                    "The local copy can't be inside Guacamole's own folders"
                )
        for f in self.folders.values():
            if f.id == exclude:
                continue
            if paths.overlaps(local, Path(f.local)):
                raise UserError(f"That overlaps {f.local}, which is already kept local")
            if f.remote == remote and (
                f.path == ""
                or rpath == ""
                or rpath == f.path
                or rpath.startswith(f.path + "/")
                or f.path.startswith(rpath + "/")
            ):
                raise UserError(
                    f"That overlaps '{f.path or '/'}' on {remote}, which is already kept local"
                )

    async def cmd_add_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        d = self._drive(args)
        rpath = clean_remote_path(args.get("path", ""))
        raw_local = str(args.get("local") or "").strip()
        local = (
            paths.expand(raw_local)
            if raw_local
            else paths.expand(self.cfg["local_root"]) / d.name / rpath
        )
        self._validate_local(local, d.name, rpath)
        spec = providers.bisync_remote_spec(d.name, self.remotes[d.name]["type"], rpath)
        if _session_name_length(spec, str(paths.link_dir() / "00000000")) > 250:
            raise UserError(
                "That cloud path is too long for bisync's state files; pick a folder higher up"
            )
        size = int(args.get("size_bytes") or 0)
        if size:
            probe = local
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            free = shutil.disk_usage(probe).free
            if size > free - (1 << 30):
                raise UserError(
                    f"Not enough free space: the folder is {util.human_bytes(size)}, "
                    f"{util.human_bytes(free)} is free there"
                )
        fid = config.new_id()
        self.remote_cfg(d.name)["folders"].append(
            {"id": fid, "path": rpath, "local": str(local), "paused": False}
        )
        self.save_cfg()
        self.st["folders"].pop(fid, None)
        self._rebuild_folders()
        f = self.folders[fid]
        f.next_due = 0
        merging = await self.fs(_dir_nonempty, str(local), timeout=5)
        self.wake()
        self.mark()
        msg = f"Keeping {d.name}:{rpath or '/'} on this device at {local}"
        if merging:
            msg += (
                " (existing files there are merged; the newer copy of each file wins)"
            )
        return {"id": fid, "local": str(local), "message": msg}

    async def cmd_remove_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        f = self._folder(args)
        if f.job_id:
            await self._cancel_sync(f)
        r = self.remote_cfg(f.remote)
        r["folders"] = [x for x in r["folders"] if x["id"] != f.id]
        self.save_cfg()
        self._rebuild_folders()
        config.save_state(self.st)
        shutil.rmtree(paths.bisync_dir(f.id), ignore_errors=True)
        try:
            os.unlink(paths.link_dir() / f.id)
        except OSError:
            pass
        message = f"Stopped keeping {f.remote}:{f.path or '/'} local; the files in {f.local} were left in place"
        if args.get("delete_local"):
            gio = shutil.which("gio")
            if not gio:
                raise UserError(
                    "Stopped syncing, but couldn't move the local copy to the trash (gio missing)"
                )
            proc = await asyncio.create_subprocess_exec(
                gio,
                "trash",
                f.local,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), 120)
            if proc.returncode != 0:
                raise UserError(
                    "Stopped syncing, but moving the local copy to the trash failed: "
                    + util.clean_error(err.decode(errors="replace"))
                )
            message = f"Stopped keeping {f.remote}:{f.path or '/'} local and moved {f.local} to the trash"
        self.mark()
        return {"message": message}

    async def cmd_pause_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        """Pause: no rclone runs for the folder until resumed, so it can be worked on offline.

        Local edits are still watched, so resuming knows they wait. Resuming runs one normal
        bisync at once: changes from both sides meet there with the usual safety stop and
        conflict copies, and nothing is forced or resynced.
        """
        f = self._folder(args)
        paused = bool(args.get("paused", True))
        name = f.label or self._folder_default_name(f)
        if paused == f.paused:
            return {"message": f"{name} is already {'paused' if paused else 'syncing'}"}
        for spec in self.remote_cfg(f.remote)["folders"]:
            if spec["id"] == f.id:
                spec["paused"] = paused
        self.save_cfg()
        f.paused = paused
        if paused:
            # A sync under way is left to finish: stopping bisync part way loses its listings,
            # and the resync that repairs them brings back files deleted while paused
            f.rerun = False
            message = (
                f"Paused {name} once the sync under way ends; nothing else syncs until you resume"
                if f.job_id
                else f"Paused {name}; nothing syncs until you resume"
            )
        else:
            # Catch up now, whatever error or backoff was pending before the pause
            f.retry_at, f.backoff, f.next_due = 0, 0, 0
            f.error = f.error_kind = ""
            if not f.watching:
                self._watch_folder(f)
            self.wake()
            message = (
                f"Resumed {name}; it needs attention first"
                if f.attention
                else f"Resumed {name}; catching up now"
            )
        self.mark()
        return {"message": message}

    async def cmd_set_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        f = self._folder(args)
        if "label" in args:
            label = str(args.get("label") or "").strip()
            f.label = label if label != self._folder_default_name(f) else ""
            for spec in self.remote_cfg(f.remote)["folders"]:
                if spec["id"] == f.id:
                    spec["label"] = f.label or None
        self.save_cfg()
        self.mark()
        return {"message": "Saved"}

    async def cmd_set_excludes(self, args: dict[str, Any]) -> dict[str, Any]:
        """Never-synced patterns: filters.txt's without an id, else that folder's own list
        (an empty one goes back to filters.txt). Either way the folders whose rules change
        resync once, safely."""
        try:
            patterns = filters.clean_patterns(args.get("patterns") or [])
        except ValueError as e:
            raise UserError(str(e)) from None
        if not args.get("id"):
            text = filters.with_excludes(self._global_filters, patterns)
            if text != self._global_filters:
                await self.fs(_write_filters, paths.filters_file(), text, timeout=5)
            self._filters_sig = self._filters_signature()
            self._global_filters_changed()
            self.wake()
            return {
                "message": "Saved; folders using these patterns resync safely",
                "excludes": filters.excludes_of(self._global_filters),
            }
        f = self._folder(args)
        name = f.label or self._folder_default_name(f)
        if patterns != f.excludes:
            for spec in self.remote_cfg(f.remote)["folders"]:
                if spec["id"] == f.id:
                    spec["excludes"] = patterns or None
            self.save_cfg()
            f.excludes = patterns
            self._filters_changed(f)
            self._persist_folder(f)
            self.wake()
        return {
            "message": (
                f"{name} uses its own never-synced patterns"
                if patterns
                else f"{name} uses the global never-synced patterns"
            ),
            "excludes": f.excludes,
        }

    async def cmd_sync_now(self, args: dict[str, Any]) -> dict[str, Any]:
        targets = (
            [self._folder(args)] if args.get("id") else list(self.folders.values())
        )
        if args.get("id") and targets[0].paused:
            f = targets[0]
            raise UserError(
                f"{f.label or self._folder_default_name(f)} is paused; resume it to sync"
            )
        paused = 0
        for f in targets:
            if f.paused:
                paused += 1
                continue
            f.retry_at, f.backoff, f.next_due = 0, 0, 0
            f.error = ""
            # The running sync may have looked before the change this is asked for
            f.rerun = bool(f.job_id)
        self.wake()
        self.mark()
        if not targets:
            return {"message": "No folders are kept local"}
        if paused == len(targets):
            return {"message": "All folders are paused; nothing to sync"}
        return {"message": "Syncing now" + (f" ({paused} paused)" if paused else "")}

    async def cmd_resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        """Act on a folder that needs attention: force (apply), resync (restore, never deletes), dismiss."""
        f = self._folder(args)
        action = str(args.get("action", ""))
        if action == "force":
            if (
                f.resync_reason or not f.initialized
            ) and f.attention_code != "missing_local":
                raise UserError(
                    "The sync state needs a Resync first; nothing will be deleted"
                )
            f.force_next = True
        elif action == "resync":
            f.resync_reason = "manual"
            f.force_next = False
            f.recovery_attempted = False
        elif action != "dismiss":
            raise UserError("Unknown action")
        if f.attention_code == "missing_local" and action in ("resync", "force"):
            await self.fs(lambda p: os.makedirs(p, exist_ok=True), f.local, timeout=10)
            f.resync_reason = "manual"
            f.force_next = False
        f.attention = f.attention_code = f.error = f.error_kind = ""
        f.waiting_network = False
        f.retry_at, f.backoff, f.next_due = 0, 0, 0
        self._persist_folder(f)
        self.wake()
        self.mark()
        return {
            "message": {
                "force": "Syncing anyway",
                "resync": "Resyncing (nothing will be deleted)",
                "dismiss": "Dismissed",
            }[action]
        }

    async def cmd_clear_conflicts(self, args: dict[str, Any]) -> dict[str, Any]:
        f = self._folder(args)
        f.conflicts = []
        self._persist_folder(f)
        self.mark()
        return {}

    # --- accounts

    async def cmd_client_guide(self, args: dict[str, Any]) -> dict[str, Any]:
        """Steps to create an own OAuth client, by provider id or by an existing drive's name."""
        rtype = str(args.get("provider") or "")
        if args.get("remote"):
            rtype = self.remotes[self._drive(args).name]["type"]
        guide = providers.client_guide(rtype)
        if guide is None:
            raise UserError("rclone has no own-client setup for this kind of drive")
        return {"guide": guide}

    async def cmd_dismiss_hint(self, args: dict[str, Any]) -> dict[str, Any]:
        d = self._drive(args)
        code = str(args.get("code") or "")
        if not any(
            w["code"] == code and w.get("dismissible")
            for w in self.remotes[d.name]["warnings"]
        ):
            raise UserError("That suggestion can't be hidden")
        rc = self.remote_cfg(d.name)
        rc["hidden_hints"] = sorted(set(rc.get("hidden_hints") or []) | {code})
        self.save_cfg()
        self.mark()
        return {}

    async def _authorize(self, rtype: str, client_id: str, client_secret: str) -> str:
        if not self.rclone_bin:
            raise UserError("rclone is not installed")
        env = os.environ.copy()
        # The environment is private to this user; argv would be visible to everyone
        if client_id:
            env[f"RCLONE_{rtype.upper()}_CLIENT_ID"] = client_id
        if client_secret:
            env[f"RCLONE_{rtype.upper()}_CLIENT_SECRET"] = client_secret
        proc = await asyncio.create_subprocess_exec(
            self.rclone_bin,
            "authorize",
            rtype,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=util.die_with_parent(),
        )
        self._auth_proc = proc
        out, err = proc.stdout, proc.stderr
        assert out is not None and err is not None  # both are pipes
        stdout = bytearray()
        stderr_tail: list[str] = []

        async def read_out() -> None:
            while True:
                chunk = await out.read(65536)
                if not chunk:
                    return
                if len(stdout) < 1 << 20:
                    stdout.extend(chunk)

        async def read_err() -> None:
            while True:
                line = await err.readline()
                if not line:
                    return
                text = line.decode(errors="replace").strip()
                m = re.search(r"https?://127\.0\.0\.1:\d+/auth\S*", text)
                if m:
                    self.auth["url"] = m.group(0)
                    self.mark()
                stderr_tail.append(text)
                del stderr_tail[:-20]

        try:
            await asyncio.wait_for(
                asyncio.gather(read_out(), read_err(), proc.wait()), timeout=300
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise UserError("Sign-in timed out. Please try again.")
        finally:
            self._auth_proc = None
        if proc.returncode != 0:
            msg = util.redact(util.clean_error("\n".join(stderr_tail)), [client_secret])
            if proc.returncode in (-signal.SIGTERM, -signal.SIGKILL):
                raise UserError("Sign-in cancelled")
            raise UserError(
                f"Sign-in failed: {msg or 'rclone authorize exited with an error'}"
            )
        m = re.search(
            r"\{[\s\S]*\"access_token\"[\s\S]*\}", stdout.decode(errors="replace")
        )
        if not m:
            raise UserError("The browser sign-in did not return a token")
        return m.group(0).strip()

    async def _token_extras(self, rtype: str, token: str) -> dict[str, str]:
        """Settings rclone authorize can't hand back with the token."""
        if rtype == "pcloud":
            # US and EU accounts live on different API hosts; ask pCloud which one this is
            host = await asyncio.get_running_loop().run_in_executor(
                self.rc_pool, _resolve_pcloud_host, token
            )
            if host:
                return {"hostname": host}
        return {}

    def _begin_auth(self, remote: str) -> None:
        if self.auth.get("busy"):
            raise UserError("Another sign-in is already in progress")
        self.auth = {
            "busy": True,
            "waiting": True,
            "url": "",
            "error": "",
            "success": "",
            "remote": remote,
        }
        self.mark()

    def _end_auth(self, error: str = "", success: str = "") -> None:
        self.auth = {
            "busy": False,
            "waiting": False,
            "url": "",
            "error": error,
            "success": success,
            "remote": self.auth.get("remote", ""),
        }
        self.mark()

    async def cmd_cancel_auth(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._auth_proc and self._auth_proc.returncode is None:
            self._auth_proc.terminate()
        return {"message": "Cancelled"}

    async def cmd_dismiss_auth(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.auth.get("busy"):
            self.auth = {
                "busy": False,
                "waiting": False,
                "url": "",
                "error": "",
                "success": "",
                "remote": "",
            }
        return {}

    def _register_new_remote(self, name: str, stream: bool, mount_path: str) -> None:
        entry = config.normalize_remote(
            {"stream": stream, "mount_path": mount_path or None}
        )
        if (
            entry["mount_path"]
            and paths.expand(entry["mount_path"]) == self.mount_root() / name
        ):
            entry["mount_path"] = None
        self.cfg["remotes"][name] = entry
        self.save_cfg()

    async def cmd_add_oauth(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        provider_id = str(args.get("provider", ""))
        meta = providers.PROVIDERS.get(provider_id)
        if not meta or meta["auth"] != "oauth":
            raise UserError("That provider doesn't use browser sign-in")
        name = clean_remote_name(args.get("name") or meta["name"].replace(" ", ""))
        if name in self.remotes:
            raise UserError(f"A drive named '{name}' already exists")
        client_id = str(args.get("client_id") or "").strip()
        client_secret = str(args.get("client_secret") or "").strip()
        rtype = meta["rclone_type"]
        if client_id or client_secret:
            if rtype not in providers.CLIENT_GUIDES:
                raise UserError("rclone has no own-client setup for this kind of drive")
            problem = providers.check_client(rtype, client_id, client_secret)
            if problem:
                raise UserError(problem, code="bad_client")
        self._begin_auth(name)
        try:
            token = await self._authorize(rtype, client_id, client_secret)
            params: dict[str, Any] = {"token": token, "config_refresh_token": "false"}
            params.update(await self._token_extras(rtype, token))
            if client_id:
                params["client_id"] = client_id
            if client_secret:
                params["client_secret"] = client_secret
            if rtype == "drive":
                params["scope"] = "drive"
            if rtype == "onedrive":
                drive_id, drive_type = await asyncio.get_running_loop().run_in_executor(
                    self.rc_pool, _resolve_onedrive, token
                )
                if drive_id:
                    params["drive_id"], params["drive_type"] = (
                        drive_id,
                        drive_type or "personal",
                    )
            try:
                await self.rc(
                    "config/create",
                    {
                        "name": name,
                        "type": rtype,
                        "parameters": params,
                        "opt": {"nonInteractive": True, "obscure": True},
                    },
                    timeout=60,
                )
            except RcError as e:
                raise UserError(
                    "Could not save the account: "
                    + util.redact(util.clean_error(str(e)), [client_secret, token])
                )
            self._register_new_remote(
                name, bool(args.get("stream", True)), str(args.get("mount_path") or "")
            )
            await self.load_remotes()
            self._spawn(self.reconcile_drive(name, force=True))
            self._end_auth(success=f"Connected {name}")
            return {"remote": name, "message": f"Connected {name}"}
        except UserError as e:
            self._end_auth(error=str(e))
            raise

    async def cmd_add_credentials(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        provider_id = str(args.get("provider", ""))
        meta = providers.PROVIDERS.get(provider_id)
        if not meta or meta["auth"] != "credentials":
            raise UserError("That provider doesn't use a login form")
        name = clean_remote_name(args.get("name") or meta["name"].replace(" ", ""))
        if name in self.remotes:
            raise UserError(f"A drive named '{name}' already exists")
        raw = args.get("options")
        opts = raw if isinstance(raw, dict) else {}
        o = {k: str(v).strip() for k, v in opts.items() if v is not None}
        params: dict[str, str] = {}
        rtype = meta["rclone_type"]
        if provider_id in ("nextcloud", "webdav"):
            url = o.get("url", "")
            if not url:
                raise UserError("Server address is required")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if provider_id == "nextcloud":
                if not o.get("user") or not o.get("pass"):
                    raise UserError("Username and app password are required")
                if "/remote.php/" not in url:
                    url = url.rstrip("/") + f"/remote.php/dav/files/{o['user']}"
                params.update(
                    {
                        "url": url,
                        "vendor": "nextcloud",
                        "user": o["user"],
                        "pass": o["pass"],
                    }
                )
            else:
                params["url"] = url
                params["vendor"] = o.get("vendor") or "other"
                for k in ("user", "pass"):
                    if o.get(k):
                        params[k] = o[k]
        elif provider_id == "s3":
            params["provider"] = o.get("provider") or "Other"
            for k in ("endpoint", "access_key_id", "secret_access_key", "region"):
                if o.get(k):
                    params[k] = o[k]
            if not params.get("access_key_id") or not params.get("secret_access_key"):
                raise UserError("Access key and secret are required")
        elif provider_id == "protondrive":
            if not o.get("username") or not o.get("password"):
                raise UserError("Proton email and password are required")
            params.update({"username": o["username"], "password": o["password"]})
            if o.get("2fa"):
                params["2fa"] = o["2fa"]
        secrets = [v for k, v in params.items() if util.is_secret_key(k)]
        self._begin_auth(name)
        self.auth["waiting"] = False
        try:
            try:
                await self.rc(
                    "config/create",
                    {
                        "name": name,
                        "type": rtype,
                        "parameters": params,
                        "opt": {"nonInteractive": True, "obscure": True},
                    },
                    timeout=60,
                )
            except RcError as e:
                raise UserError(
                    "Could not save the account: "
                    + util.redact(util.clean_error(str(e)), secrets)
                )
            try:
                await self.rc(
                    "operations/list",
                    {"fs": f"{name}:", "remote": "", "opt": {"dirsOnly": True}},
                    timeout=30,
                )
            except RcError as e:
                try:
                    await self.rc("config/delete", {"name": name}, timeout=10)
                except RcError:
                    pass
                raise UserError(
                    "Connection failed: "
                    + util.redact(util.clean_error(str(e)), secrets)
                )
            self._register_new_remote(
                name, bool(args.get("stream", True)), str(args.get("mount_path") or "")
            )
            await self.load_remotes()
            self._spawn(self.reconcile_drive(name, force=True))
            self._end_auth(success=f"Connected {name}")
            return {"remote": name, "message": f"Connected {name}"}
        except UserError as e:
            self._end_auth(error=str(e))
            raise

    async def cmd_set_client_id(self, args: dict[str, Any]) -> dict[str, Any]:
        """Re-authorize a drive with the user's own OAuth client (fixes shared-client rate limits)."""
        self._need_engine()
        d = self._drive(args)
        rtype = self.remotes[d.name]["type"]
        if rtype not in providers.CLIENT_GUIDES:
            raise UserError("rclone has no own-client setup for this kind of drive")
        client_id = str(args.get("client_id") or "").strip()
        client_secret = str(args.get("client_secret") or "").strip()
        problem = providers.check_client(rtype, client_id, client_secret)
        if problem:
            raise UserError(problem, code="bad_client")
        self._begin_auth(d.name)
        try:
            token = await self._authorize(rtype, client_id, client_secret)
            try:
                await self.rc(
                    "config/update",
                    {
                        "name": d.name,
                        "parameters": dict(
                            await self._token_extras(rtype, token),
                            client_id=client_id,
                            client_secret=client_secret,
                            token=token,
                        ),
                        "opt": {"nonInteractive": True, "obscure": True},
                    },
                    timeout=60,
                )
                await self.rc("fscache/clear", {}, timeout=10)
            except RcError as e:
                raise UserError(
                    "Could not update the account: "
                    + util.redact(util.clean_error(str(e)), [client_secret, token])
                )
            await self.load_remotes()
            message = f"{d.name} now uses your own client ID"
            if d.mount_state == "mounted":
                try:
                    async with d.lock:
                        await self._unmount(d, force=False)
                    await self.reconcile_drive(d.name, force=True)
                except UserError:
                    message += " (remount it to apply)"
            self._end_auth(success=message)
            return {"message": message}
        except UserError as e:
            self._end_auth(error=str(e))
            raise

    async def cmd_reconnect(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run the browser sign-in again for an existing OAuth drive (expired or revoked token)."""
        self._need_engine()
        d = self._drive(args)
        rtype = self.remotes[d.name]["type"]
        if rtype not in providers.OAUTH_TYPES:
            raise UserError(
                "Reconnect this drive with `rclone config reconnect " + d.name + ":`"
            )
        self._begin_auth(d.name)
        try:
            # Reuses the drive's own client id if it has one
            token = await self._authorize_existing(d.name, rtype)
            try:
                await self.rc(
                    "config/update",
                    {
                        "name": d.name,
                        "parameters": dict(
                            await self._token_extras(rtype, token), token=token
                        ),
                        "opt": {"nonInteractive": True},
                    },
                    timeout=60,
                )
                await self.rc("fscache/clear", {}, timeout=10)
            except RcError as e:
                raise UserError(
                    "Could not update the account: "
                    + util.redact(util.clean_error(str(e)), [token])
                )
            d.retry_at, d.backoff, d.error, d.error_kind = 0, 0, "", ""
            if d.mount_state == "error":
                d.mount_state = "off"
            for f in self.folders.values():
                if f.remote == d.name and f.attention_code == "auth":
                    f.attention = f.attention_code = ""
                    f.retry_at = 0
            await self.load_remotes()
            self._spawn(self.reconcile_drive(d.name, force=True))
            self._end_auth(success=f"Reconnected {d.name}")
            return {"message": f"Reconnected {d.name}"}
        except UserError as e:
            self._end_auth(error=str(e))
            raise

    async def _authorize_existing(self, name: str, rtype: str) -> str:
        try:
            conf = await self.rc("config/get", {"name": name}, timeout=10)
        except RcError:
            conf = {}
        client_id = str(conf.get("client_id", "") or "")
        client_secret = str(conf.get("client_secret", "") or "")
        return await self._authorize(rtype, client_id, client_secret)

    async def cmd_remove_remote(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        for f in [f for f in self.folders.values() if f.remote == d.name]:
            await self.cmd_remove_folder({"id": f.id})
        if d.mount_state == "mounted":
            async with d.lock:
                await self._unmount(d, force=bool(args.get("force")))
        try:
            await self.rc("config/delete", {"name": d.name}, timeout=10)
            await self.rc("fscache/clear", {}, timeout=10)
        except RcError as e:
            raise UserError("Could not remove the account: " + util.clean_error(str(e)))
        self.cfg["remotes"].pop(d.name, None)
        self.st["quota"].pop(d.name, None)
        self.save_cfg()
        config.save_state(self.st)
        self.drives.pop(d.name, None)
        await self.load_remotes()
        return {
            "message": f"Removed {d.name}. Local copies of its folders were left in place."
        }


class _SafeLogFormatter(logging.Formatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        # Keep the entire entry header and message on one physical line.
        return util.neutralize_controls(super().formatMessage(record))

    def format(self, record: logging.LogRecord) -> str:
        # Tracebacks and stack dumps remain readable. Mark every continuation so
        # exception text cannot impersonate a new timestamped entry. Work on a
        # copy: logging caches message/exception text on records shared by handlers.
        text = super().format(copy.copy(record))
        return util.neutralize_controls(text, multiline=True).replace("\n", "\n    | ")


def _setup_logging(verbose: bool) -> None:
    paths.ensure_private_dir(paths.state_dir())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = _SafeLogFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        paths.daemon_log(), maxBytes=1 << 20, backupCount=1, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    try:
        os.chmod(paths.daemon_log(), 0o600)
    except OSError:
        pass
    if sys.stderr and (sys.stderr.isatty() or os.environ.get("JOURNAL_STREAM")):
        sh = logging.StreamHandler()
        sh.setFormatter(_SafeLogFormatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)


def main(verbose: bool = False) -> int:
    paths.ensure_runtime_dir()
    lock_fd = os.open(paths.daemon_lock(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("guacd is already running", file=sys.stderr)
        return 0
    _setup_logging(verbose)
    try:
        return asyncio.run(Daemon().run())
    finally:
        os.close(lock_fd)
