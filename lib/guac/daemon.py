"""guacd: the Clouds Guacamole background service.

One long-running process per user that:
  * runs a single `rclone rcd` hosting every mount and sync job, talking to it over a private
    unix socket (no rclone process is ever spawned for a status check);
  * streams drives with a tuned VFS cache and keeps chosen folders on this device with
    `bisync`, triggered by local changes (inotify), a timer, and resume-from-suspend;
  * pushes state to the bar widget over its own unix socket only when something changed.
"""

import asyncio
import concurrent.futures
import errno
import fcntl
import fnmatch
import hashlib
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import __version__, config, paths, providers, recent, util
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
MAX_SYNC_JOBS = 2
DEBOUNCE = 4.0  # quiet time after a local change before syncing it
FOLLOWUP_DEBOUNCE = 10.0  # changes seen while a sync ran (mostly its own writes)
MAX_DIRTY_WAIT = 30.0  # never hold local changes back longer than this
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
    job_id: int = 0
    job_started: float = 0.0
    progress: Dict[str, Any] = field(default_factory=dict)
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
    force_next: bool = False
    conflicts: List[str] = field(default_factory=list)
    watching: bool = False
    watch_error: str = ""
    waiting_network: bool = False


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


def _top_dirs(path: str, limit: int) -> List[str]:
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


def _list_dirs(path: str, limit: int) -> List[str]:
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


def _collect_dirs(top: str, limit: int) -> List[str]:
    found = []
    stack = [top]
    while stack and len(found) < limit:
        d = stack.pop()
        found.append(d)
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(
                            follow_symlinks=False
                        ) and not entry.name.startswith(".Trash-"):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


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
    canon = lambda s: re.sub(r"[\\/:?*<>|]", "_", s.strip("\\/"))  # noqa: E731
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


def _resolve_onedrive(token_json: str) -> Tuple[Optional[str], Optional[str]]:
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
    except Exception:
        return None, None


def _resolve_pcloud_host(token_json: str) -> Optional[str]:
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
        except Exception:  # noqa: BLE001 - any failure means "not this host"
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


def _parse_conflicts(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        if "Renaming Path1 copy" in line or "Renaming Path2 copy" in line:
            target = line.rsplit(" - ", 1)[-1].strip()
            name = os.path.basename(target)
            if name and name not in out:
                out.append(util.neutralize_controls(name))
    return out


def _transient_failure(text: str) -> str:
    """"rate" or "network" when bisync's critical error came from throttling or the network.

    bisync ends every critical error with "Must run --resync to recover", so that phrase alone
    doesn't mean the sync state is broken: a throttled or dropped listing only needs a retry.
    """
    critical = "\n".join(l for l in text.splitlines() if "critical error" in l.lower())
    kind = util.classify_error(critical) if critical else "other"
    return kind if kind in ("rate", "network") else ""


def _stats_speed(stats: Dict[str, Any]) -> int:
    """Bytes per second of a core/stats answer.

    rclone leaves the per-file speeds empty for transfers driven by sync and bisync, so fall
    back to the group's average while something is moving.
    """
    moving = stats.get("transferring") or []
    per_file = sum(int(t.get("speedAvg") or 0) for t in moving)
    return int(per_file or (float(stats.get("speed") or 0) if moving else 0))


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
        self.engine: Optional[asyncio.subprocess.Process] = None
        self.engine_state = "starting"
        self.engine_error = ""
        self.rclone_version = ""
        self.rclone_config_path = ""
        self._rclone_conf_sig: Any = None
        self.remotes: Dict[str, Dict[str, Any]] = {}
        self.drives: Dict[str, Drive] = {}
        self.folders: Dict[str, Folder] = {}
        self.clients: Set[Client] = set()
        self.online = util.has_default_route()
        self.transfers: Dict[str, Any] = {"count": 0, "speed": 0, "names": []}
        self.recent: List[Dict[str, Any]] = []
        self._recent_at = 0.0
        self._recent_due = 0.0
        self.auth: Dict[str, Any] = {
            "busy": False,
            "waiting": False,
            "url": "",
            "error": "",
            "success": "",
            "remote": "",
        }
        self._auth_proc: Optional[asyncio.subprocess.Process] = None
        self._auth_task: Optional[asyncio.Task] = None
        self.inotify: Optional[Inotify] = None
        self._wd: Dict[int, Tuple[str, str]] = {}
        self._folder_wds: Dict[str, Set[int]] = {}
        self._last_state_line = ""
        self._boot_offset = self._boottime_offset()
        self._notified: Dict[str, float] = {}
        self._backups_pruned = 0.0
        self.backup_files = 0  # local files syncs replaced or deleted, kept in backups/
        self._filters_md5 = ""
        self._tasks: List[asyncio.Task] = []
        self._dirty: Optional[asyncio.Event] = None
        self._stop: Optional[asyncio.Event] = None
        self._wake: Optional[asyncio.Event] = None
        self._tick_now: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _boottime_offset() -> float:
        # CLOCK_MONOTONIC stops during suspend, CLOCK_BOOTTIME doesn't: their gap grows on resume
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    def mark(self) -> None:
        if self._dirty is not None:
            self._dirty.set()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def rc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            self.rc_pool, self.rcc.call, method, params or {}, timeout
        )
        try:
            return await asyncio.wait_for(fut, timeout + 5)
        except asyncio.TimeoutError:
            raise RcError(f"rclone {method} timed out")

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

    def remote_cfg(self, name: str) -> Dict[str, Any]:
        if name not in self.cfg["remotes"]:
            self.cfg["remotes"][name] = config.normalize_remote({})
        return self.cfg["remotes"][name]

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tick_now = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: self._spawn(self.reload()))

        paths.ensure_private_dir(paths.state_dir())
        paths.ensure_private_dir(paths.backup_root())
        config.ensure_filters()
        self._spawn(self._ensure_local_root())
        self._filters_md5 = self._filters_hash()
        if self.st.get("filters_md5") and self.st["filters_md5"] != self._filters_md5:
            self.st["filters_changed"] = True
        self.st["filters_md5"] = self._filters_md5
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
        sock_path = paths.daemon_socket()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(
                self._handle_client, path=str(sock_path), limit=CLIENT_LINE_LIMIT
            )
        finally:
            os.umask(old_umask)
        return server

    async def _shutdown(self) -> None:
        if self.engine_state == "running":
            for f in list(self.folders.values()):
                if f.job_id:
                    try:
                        await self.rc("job/stop", {"jobid": f.job_id}, timeout=5)
                    except RcError:
                        pass
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(
                f.job_id for f in self.folders.values()
            ):
                for f in list(self.folders.values()):
                    if f.job_id:
                        try:
                            js = await self.rc(
                                "job/status", {"jobid": f.job_id}, timeout=3
                            )
                            if js.get("finished"):
                                f.job_id = 0
                        except RcError:
                            f.job_id = 0
                await asyncio.sleep(0.3)
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
                await self._start_engine()
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
            except Exception:  # noqa: BLE001
                log.exception("post-start setup failed")
            code = await self.engine.wait()
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

    async def _start_engine(self) -> None:
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
        log_path = paths.rclone_log()
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        args = [
            self.rclone_bin,
            "rcd",
            f"--rc-addr=unix://{sock}",
            "--rc-no-auth",
            "--rc-job-expire-duration=10m",
            "--rc-job-expire-interval=1m",
            f"--log-file={log_path}",
            "--log-level=INFO",
            "--log-file-max-size=5M",
            "--log-file-max-backups=1",
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
        ]
        try:
            self.engine = await asyncio.create_subprocess_exec(
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
            if self.engine.returncode is not None:
                tail = " ".join(util.tail_lines(log_path, 3))
                raise RuntimeError(f"rclone exited during startup: {tail}")
            if sock.exists() and await loop.run_in_executor(
                self.rc_pool, self.rcc.ping, 1.0
            ):
                break
            if time.monotonic() > deadline:
                self.engine.kill()
                raise RuntimeError("rclone did not start in time")
            await asyncio.sleep(0.05)
        try:
            ver = await self.rc("core/version", timeout=5)
            self.rclone_version = str(ver.get("version", ""))
            cp = await self.rc("config/paths", timeout=5)
            self.rclone_config_path = str(cp.get("config", ""))
        except RcError:
            pass
        log.info("rclone %s ready", self.rclone_version)

    async def _after_engine_start(self) -> None:
        await self.load_remotes()
        for name in list(self.drives):
            self._spawn(self.reconcile_drive(name, force=True))

    def _on_engine_lost(self) -> None:
        for d in self.drives.values():
            if d.mount_state in ("mounted", "mounting", "unmounting"):
                d.mount_state = "off"
                d.retry_at = 0
        for f in self.folders.values():
            if f.job_id:
                f.job_id = 0
                f.progress = {}
                f.retry_at = time.time() + 5

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
        except Exception as e:  # noqa: BLE001 - never leave a drive stuck in "mounting"
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
                params: Dict[str, Any] = {"fs": d.mounted_fs}
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
                    )
                    f.initialized = bool(saved.get("initialized"))
                    f.last_sync = float(saved.get("last_sync", 0))
                    f.last_changes = int(saved.get("last_changes", 0))
                    f.conflicts = list(saved.get("conflicts", []))[:20]
                    f.attention = str(saved.get("attention", ""))
                    f.attention_code = str(saved.get("attention_code", ""))
                    f.error = str(saved.get("error", ""))
                    if self.st.get("filters_changed") and f.initialized:
                        f.resync_reason = "filters"
                    # Catch up shortly after start, staggered so they don't all list at once
                    f.next_due = now + 5 + len(self.folders) * 2
                    self.folders[fid] = f
                    self._watch_folder(f)
                else:
                    f.paused = spec.get("paused", False)
        self.st.pop("filters_changed", None)
        for fid in list(self.folders):
            if fid not in seen:
                self._unwatch_folder(fid)
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

    async def _scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.engine_state != "running":
                continue
            now = time.time()
            running = [f for f in self.folders.values() if f.job_id]
            busy = {f.remote for f in running}
            # Local edits first, then never-synced folders, then routine checks
            order = sorted(
                self.folders.values(),
                key=lambda f: (not f.dirty_since, f.initialized, f.next_due),
            )
            for f in order:
                if len(running) >= MAX_SYNC_JOBS:
                    break
                if f.remote in busy or not self._folder_due(f, now):
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
                except Exception as e:  # noqa: BLE001
                    log.exception("could not start sync of %s", f.id)
                    # Shown on the folder, and retried soon: most causes are passing
                    if isinstance(e, OSError):
                        f.error = f"Can't use {e.filename or f.local}: {e.strerror or e}"
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
                    busy.add(f.remote)

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
        params: Dict[str, Any] = {
            "path1": providers.bisync_remote_spec(f.remote, rtype, f.path),
            "path2": link,
            "workdir": str(paths.ensure_private_dir(paths.bisync_dir(f.id))),
            "filtersFile": str(paths.filters_file()),
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
        res = await self.rc("sync/bisync", params, timeout=30)
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
        try:
            js = await self.rc("job/status", {"jobid": f.job_id}, timeout=5)
        except RcError as e:
            if "not found" in str(e).lower():
                f.job_id, f.progress, f.retry_at = 0, {}, time.time() + 5
                self.mark()
            return
        if not js.get("finished"):
            try:
                stats = await self.rc("core/stats", {"group": group}, timeout=5)
            except RcError:
                return
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

    async def _finish_sync(self, f: Folder, js: Dict[str, Any]) -> None:
        group = f"guac/{f.id}"
        changes = 0
        try:
            stats = await self.rc(
                "core/stats", {"group": group, "short": True}, timeout=5
            )
            changes = (
                int(stats.get("transfers", 0))
                + int(stats.get("deletes", 0))
                + int(stats.get("renames", 0))
            )
            await self.rc("core/stats-delete", {"group": group}, timeout=5)
        except RcError:
            pass
        out = js.get("output")
        text = str(out.get("output", "")) if isinstance(out, dict) else ""
        err = str(js.get("error") or "")
        now = time.time()
        was_resync = bool(f.resync_reason) or not f.initialized
        f.job_id, f.progress = 0, {}
        conflicts = _parse_conflicts(text)
        if js.get("success"):
            first_sync = not f.initialized
            f.initialized, f.resync_reason = True, ""
            f.last_sync, f.last_changes = now, changes
            f.error = f.error_kind = f.attention = f.attention_code = ""
            f.backoff = 0
            f.next_due = now + self.cfg["sync_interval_min"] * 60
            if conflicts:
                f.conflicts = (
                    conflicts + [c for c in f.conflicts if c not in conflicts]
                )[:20]
                self.notify(
                    f"conflict:{f.id}",
                    f"Sync conflict in {os.path.basename(f.local)}",
                    "Kept both versions: " + ", ".join(conflicts[:3]),
                )
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
            detail = util.clean_error(err or text)
            blob = (err + "\n" + text[-6000:]).lower()
            transient = _transient_failure(blob)
            log.warning(
                "sync %s failed: %s%s", f.id, detail, f" ({transient})" if transient else ""
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
                    "Check both copies, then choose Sync anyway (applies it) or Resync (restores, never deletes).",
                )
            elif "cannot sync to an empty directory" in blob:
                self._folder_attention(
                    f,
                    "empty",
                    "One side is empty, so nothing was synced. "
                    "Resync restores files to the empty side; Sync anyway would empty the other side too.",
                )
            elif not transient and (
                "must run --resync" in blob
                or "cannot find prior" in blob
                or "md5 hash not found" in blob
            ):
                if was_resync:
                    self._folder_attention(
                        f, "resync_failed", "Could not repair the sync state: " + detail
                    )
                else:
                    f.resync_reason, f.retry_at = "recover", now + 2
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
                if transient and f.initialized and "must run --resync" in blob:
                    # Retried later; bisync won't run again without a resync
                    f.resync_reason = f.resync_reason or "recover"
                kind = transient or util.classify_error(blob)
                if kind == "auth":
                    self._folder_attention(
                        f,
                        "auth",
                        "Sign-in expired or was revoked. Reconnect this account.",
                    )
                else:
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
            f"{os.path.basename(f.local)} needs attention",
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
        if self._dirty is None:
            return
        self._spawn(self._add_watches(f.id, f.local))

    async def _add_watches(self, fid: str, top: str) -> None:
        f = self.folders.get(fid)
        if f is None or self.inotify is None:
            if f is not None:
                f.watch_error = "File watching is unavailable; syncing on a timer only"
            return
        try:
            dirs = await self.fs(_collect_dirs, top, MAX_WATCH_DIRS, timeout=120)
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
        touched: Set[str] = set()
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
            if ev.name and any(
                fnmatch.fnmatchcase(ev.name, p) for p in _IGNORED_EVENT_NAMES
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
            except Exception:  # noqa: BLE001
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
        try:
            mounts = await self.rc("mount/listmounts", timeout=5)
            ours = {m.get("MountPoint") for m in mounts.get("mountPoints") or []}
        except RcError:
            return
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
                    st = await self.rc("vfs/stats", {"fs": d.mounted_fs}, timeout=5)
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
            stats = await self.rc("core/stats", {}, timeout=5)
            moving = stats.get("transferring") or []
            transfers = {
                "count": len(moving),
                "speed": _stats_speed(stats),
                "names": [
                    util.neutralize_controls(os.path.basename(str(t.get("name", ""))))
                    for t in moving[:3]
                ],
            }
            if transfers != self.transfers:
                self.transfers = transfers
                self.mark()
        except RcError:
            pass

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
                md5 = self._filters_hash()
                if md5 != self._filters_md5:
                    log.info("filters changed: next syncs are resyncs")
                    self._filters_md5 = self.st["filters_md5"] = md5
                    for f in self.folders.values():
                        if f.initialized:
                            f.resync_reason = "filters"
                    config.save_state(self.st)
                    self.wake()
                if now - self._backups_pruned > 6 * 3600:
                    self._backups_pruned = now
                    await self.fs(
                        _prune_backups,
                        str(paths.backup_root()),
                        int(self.cfg.get("backup_days", 30)),
                        timeout=120,
                    )
                count = await self.fs(_count_files, str(paths.backup_root()), timeout=20)
                if count != self.backup_files:
                    self.backup_files = count
                    self.mark()
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed")

    def _filters_hash(self) -> str:
        try:
            return hashlib.md5(paths.filters_file().read_bytes()).hexdigest()
        except OSError:
            return ""

    async def _refresh_quotas(self, now: float, only: Optional[str] = None) -> None:
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

        def scan() -> List[Dict[str, Any]]:
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

    def _folder_state(self, f: Folder, now: float) -> Dict[str, Any]:
        if f.paused:
            state = "paused"
        elif f.attention:
            state = "attention"
        elif f.job_id:
            state = "syncing"
        elif f.waiting_network:
            state = "waiting"
        elif f.error and f.retry_at > now:
            state = "error"
        elif not f.initialized:
            state = "pending"
        elif f.dirty_since and not f.followup_only:
            state = "changes"
        else:
            state = "synced"
        name = (
            os.path.basename(f.path)
            if f.path
            else (self.remote_cfg(f.remote).get("label") or f.remote)
        )
        return {
            "id": f.id,
            "remote": f.remote,
            "path": f.path,
            "name": name,
            "local": f.local,
            "state": state,
            "paused": f.paused,
            "initialized": f.initialized,
            "lastSync": int(f.last_sync),
            "lastChanges": f.last_changes,
            "nextSync": int(max(f.next_due, f.retry_at)) if not f.job_id else 0,
            "error": f.error,
            "errorKind": f.error_kind,
            "attention": f.attention,
            "attentionCode": f.attention_code,
            "resync": bool(f.resync_reason) or not f.initialized,
            "progress": f.progress,
            "conflicts": f.conflicts[:5],
            "conflictCount": len(f.conflicts),
            "watching": f.watching,
            "watchError": f.watch_error,
        }

    def build_state(self) -> Dict[str, Any]:
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
            attention += sum(1 for x in folders if x["state"] in ("attention", "error"))
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

    async def _dispatch(self, client: Client, msg: Dict[str, Any]) -> None:
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
        except Exception as e:  # noqa: BLE001
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

    def _drive(self, args: Dict[str, Any]) -> Drive:
        name = str(args.get("remote", ""))
        if name not in self.remotes or name not in self.drives:
            raise UserError(f"No drive named '{name}'")
        return self.drives[name]

    def _folder(self, args: Dict[str, Any]) -> Folder:
        f = self.folders.get(str(args.get("id", "")))
        if f is None:
            raise UserError("That folder is no longer kept local")
        return f

    # --- general

    async def cmd_ping(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"pong": True, "version": __version__}

    async def cmd_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"state": self.build_state()}

    async def cmd_touch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The panel opened: freshen cheap things now, stale quotas in the background."""
        now = time.time()
        if now - self._recent_at > 10:
            self._recent_due = now
        for name in self.remotes:
            q = self.st["quota"].get(name)
            if q and now - q.get("at", 0) > 10 * 60 and not q.get("unsupported"):
                q["at"] = 0
        return {}

    async def cmd_shutdown(self, args: Dict[str, Any]) -> Dict[str, Any]:
        asyncio.get_running_loop().call_later(0.2, self._stop.set)
        return {"message": "Stopping"}

    async def cmd_restart_engine(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.engine and self.engine.returncode is None:
            self.engine.terminate()
        return {"message": "Restarting rclone"}

    async def cmd_log(self, args: Dict[str, Any]) -> Dict[str, Any]:
        lines = max(10, min(500, int(args.get("lines", 80))))
        source = str(args.get("source") or "rclone")
        if source not in ("rclone", "daemon"):
            raise UserError("The log source is rclone or daemon")
        path = paths.rclone_log() if source == "rclone" else paths.daemon_log()
        tail = await asyncio.get_running_loop().run_in_executor(
            self.rc_pool, util.tail_lines, path, lines
        )
        return {"lines": tail, "path": str(path)}

    async def cmd_set_setting(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_stream(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_stream_all(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_retry(self, args: Dict[str, Any]) -> Dict[str, Any]:
        d = self._drive(args)
        d.retry_at, d.backoff = 0, 0
        if d.mount_state in ("error", "waiting"):
            d.mount_state = "off"
        await self.reconcile_drive(d.name, force=True, wait=True)
        return {"message": d.error or f"{d.name}: {d.mount_state}"}

    async def cmd_takeover(self, args: Dict[str, Any]) -> Dict[str, Any]:
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
            _, err = await asyncio.wait_for(proc.communicate(), 15)
            if proc.returncode != 0:
                await self._lazy_unmount(mp)
        d.mount_state, d.retry_at = "off", 0
        self.remote_cfg(d.name)["stream"] = True
        self.save_cfg()
        await self.reconcile_drive(d.name, force=True, wait=True)
        if d.mount_state != "mounted":
            raise UserError(d.error or "Could not take over the mount")
        return {"message": f"{d.name} is now streamed by Guacamole"}

    async def cmd_set_drive(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_browse(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._need_engine()
        d = self._drive(args)
        path = clean_remote_path(args.get("path", ""))
        dirs: Optional[List[str]] = None
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

    async def cmd_folder_size(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_add_folder(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_remove_folder(self, args: Dict[str, Any]) -> Dict[str, Any]:
        f = self._folder(args)
        if f.job_id:
            try:
                await self.rc("job/stop", {"jobid": f.job_id}, timeout=5)
            except RcError:
                pass
            for _ in range(50):
                try:
                    js = await self.rc("job/status", {"jobid": f.job_id}, timeout=3)
                    if js.get("finished"):
                        break
                except RcError:
                    break
                await asyncio.sleep(0.2)
            f.job_id = 0
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

    async def cmd_pause_folder(self, args: Dict[str, Any]) -> Dict[str, Any]:
        f = self._folder(args)
        paused = bool(args.get("paused", True))
        for spec in self.remote_cfg(f.remote)["folders"]:
            if spec["id"] == f.id:
                spec["paused"] = paused
        self.save_cfg()
        f.paused = paused
        if not paused:
            f.next_due = 0
            self.wake()
        self.mark()
        return {"message": "Paused" if paused else "Resumed"}

    async def cmd_sync_now(self, args: Dict[str, Any]) -> Dict[str, Any]:
        targets = (
            [self._folder(args)] if args.get("id") else list(self.folders.values())
        )
        for f in targets:
            if f.paused:
                continue
            f.retry_at, f.backoff, f.next_due = 0, 0, 0
            f.error = ""
        self.wake()
        self.mark()
        return {"message": "Syncing now" if targets else "No folders are kept local"}

    async def cmd_resolve(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Act on a folder that needs attention: force (apply), resync (restore, never deletes), dismiss."""
        f = self._folder(args)
        action = str(args.get("action", ""))
        if action == "force":
            f.force_next = True
        elif action == "resync":
            f.resync_reason = "manual"
        elif action != "dismiss":
            raise UserError("Unknown action")
        if f.attention_code == "missing_local" and action in ("resync", "force"):
            await self.fs(lambda p: os.makedirs(p, exist_ok=True), f.local, timeout=10)
            f.resync_reason = "manual"
            f.force_next = False
        f.attention = f.attention_code = f.error = ""
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

    async def cmd_clear_conflicts(self, args: Dict[str, Any]) -> Dict[str, Any]:
        f = self._folder(args)
        f.conflicts = []
        self._persist_folder(f)
        self.mark()
        return {}

    # --- accounts

    async def cmd_client_guide(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Steps to create an own OAuth client, by provider id or by an existing drive's name."""
        rtype = str(args.get("provider") or "")
        if args.get("remote"):
            rtype = self.remotes[self._drive(args).name]["type"]
        guide = providers.client_guide(rtype)
        if guide is None:
            raise UserError("rclone has no own-client setup for this kind of drive")
        return {"guide": guide}

    async def cmd_dismiss_hint(self, args: Dict[str, Any]) -> Dict[str, Any]:
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
        stdout = bytearray()
        stderr_tail: List[str] = []

        async def read_out() -> None:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    return
                if len(stdout) < 1 << 20:
                    stdout.extend(chunk)

        async def read_err() -> None:
            while True:
                line = await proc.stderr.readline()
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

    async def _token_extras(self, rtype: str, token: str) -> Dict[str, str]:
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

    async def cmd_cancel_auth(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._auth_proc and self._auth_proc.returncode is None:
            self._auth_proc.terminate()
        return {"message": "Cancelled"}

    async def cmd_dismiss_auth(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_add_oauth(self, args: Dict[str, Any]) -> Dict[str, Any]:
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
            params: Dict[str, Any] = {"token": token, "config_refresh_token": "false"}
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

    async def cmd_add_credentials(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._need_engine()
        provider_id = str(args.get("provider", ""))
        meta = providers.PROVIDERS.get(provider_id)
        if not meta or meta["auth"] != "credentials":
            raise UserError("That provider doesn't use a login form")
        name = clean_remote_name(args.get("name") or meta["name"].replace(" ", ""))
        if name in self.remotes:
            raise UserError(f"A drive named '{name}' already exists")
        opts = args.get("options") if isinstance(args.get("options"), dict) else {}
        o = {k: str(v).strip() for k, v in opts.items() if v is not None}
        params: Dict[str, str] = {}
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

    async def cmd_set_client_id(self, args: Dict[str, Any]) -> Dict[str, Any]:
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

    async def cmd_reconnect(self, args: Dict[str, Any]) -> Dict[str, Any]:
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
                        "parameters": dict(await self._token_extras(rtype, token), token=token),
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

    async def cmd_remove_remote(self, args: Dict[str, Any]) -> Dict[str, Any]:
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


def _setup_logging(verbose: bool) -> None:
    paths.ensure_private_dir(paths.state_dir())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
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
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)


def main(verbose: bool = False) -> int:
    paths.ensure_private_dir(paths.runtime_dir())
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
