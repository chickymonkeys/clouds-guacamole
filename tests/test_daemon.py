#!/usr/bin/env python3
"""End-to-end test of guacd against local rclone "alias" remotes.

Everything runs in throwaway directories with its own rclone config, so your real drives,
~/Cloud and rclone.conf are never touched. Needs rclone and FUSE (fusermount3).

    tests/test_daemon.py            # run all scenarios
    tests/test_daemon.py -k sync    # only scenarios whose name contains "sync"
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


class Sandbox:
    def __init__(self) -> None:
        uid = os.getuid()
        self.tmp = Path(tempfile.mkdtemp(prefix="guac-test-"))
        # Sockets and bisync's session names need short paths: keep those in the runtime dir
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
        self.run = Path(tempfile.mkdtemp(prefix="gt", dir=base))
        for name in ("remoteA", "remoteB", "state", "mnt"):
            (self.tmp / name).mkdir()
        (self.run / "st").symlink_to(self.tmp / "state")
        (self.run / "mn").symlink_to(self.tmp / "mnt")
        self.remote_a = self.tmp / "remoteA"
        self.remote_b = self.tmp / "remoteB"
        self.local_root = self.tmp / "local"
        self.mnt = self.run / "mn"
        (self.run / "rA").symlink_to(self.remote_a)
        (self.run / "rB").symlink_to(self.remote_b)
        (self.remote_a / "Photos").mkdir()
        (self.remote_a / "a.txt").write_text("hello A\n")
        (self.remote_a / "Photos" / "p.jpg").write_text("pic\n")
        (self.remote_b / "docs" / "sub").mkdir(parents=True)
        (self.remote_b / "docs" / "one.md").write_text("doc one\n")
        (self.remote_b / "docs" / "sub" / "two.md").write_text("deep\n")
        (self.remote_b / "docs" / "sub" / "three.md").write_text("three\n")
        conf = self.tmp / "rclone.conf"
        conf.write_text(
            f"[CloudA]\ntype = alias\nremote = {self.run}/rA\n\n[CloudB]\ntype = alias\nremote = {self.run}/rB\n"
        )
        cfg_dir = self.tmp / "cfg" / "guacamole"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mount_root": str(self.mnt),
                    "local_root": str(self.local_root),
                    "notifications": False,
                    "remotes": {},
                }
            )
        )
        self.env = dict(os.environ)
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
                "XDG_STATE_HOME": str(self.run / "st"),
                "XDG_CACHE_HOME": str(self.tmp / "cache"),
                "XDG_RUNTIME_DIR": str(self.run),
                "RCLONE_CONFIG": str(conf),
            }
        )
        os.environ.update(self.env)
        sys.path.insert(0, str(REPO / "lib"))
        self.proc = None

    def start(self) -> None:
        log = open(self.tmp / "daemon.out", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "bin" / "guac"), "daemon", "-v"],
            env=self.env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        wait(lambda: self.state()["engine"]["state"] == "running", 20, "daemon ready")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.request("shutdown")
            except Exception:  # noqa: BLE001
                self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cleanup(self, keep: bool = False) -> None:
        self.stop()
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mp = line.split()[4]
            if mp.startswith(str(self.tmp)) or mp.startswith(str(self.run)):
                subprocess.run(["fusermount3", "-uz", mp], capture_output=True)
        shutil.rmtree(self.run, ignore_errors=True)
        if not keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self):
        from guac import client

        return client.Connection(timeout=30)

    def state(self) -> dict:
        try:
            c = self.conn()
        except Exception:  # noqa: BLE001
            return {"engine": {"state": "down"}, "drives": []}
        try:
            return c.first_state()
        finally:
            c.close()

    def request(self, cmd: str, **args) -> dict:
        c = self.conn()
        try:
            return c.request(cmd, args, timeout=120)
        finally:
            c.close()

    def ok(self, cmd: str, **args) -> dict:
        reply = self.request(cmd, **args)
        assert reply.get("ok"), f"{cmd} failed: {reply.get('error')}"
        return reply.get("data") or {}

    def drive(self, name: str) -> dict:
        return next(d for d in self.state()["drives"] if d["name"] == name)

    def folder(self, fid: str) -> dict:
        for d in self.state()["drives"]:
            for f in d["folders"]:
                if f["id"] == fid:
                    return f
        raise KeyError(fid)


def wait(cond, timeout: float, what: str, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            value = cond()
            if value:
                return value
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(interval)
    raise AssertionError(
        f"timed out waiting for {what}" + (f" ({last_err})" if last_err else "")
    )


# --------------------------------------------------------------------------- scenarios


def test_stream(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    mp = Path(sb.drive("CloudA")["mountPath"])
    assert (mp / "a.txt").read_text() == "hello A\n"
    (mp / "new.txt").write_text("via mount\n")
    wait(lambda: (sb.remote_a / "new.txt").exists(), 15, "write-through upload")
    sb.ok("stream", remote="CloudA", on=False)
    assert not os.path.ismount(mp)
    assert not mp.exists(), "the empty mount folder is removed after unmounting"


def test_busy_unmount(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    mp = sb.drive("CloudA")["mountPath"]
    holder = subprocess.Popen(["sleep", "30"], cwd=mp)
    try:
        reply = sb.request("stream", remote="CloudA", on=False)
        assert not reply["ok"] and reply.get("code") == "busy", reply
        assert sb.drive("CloudA")["mounted"], (
            "a refused unmount must leave the drive mounted"
        )
        assert sb.drive("CloudA")["stream"], (
            "a refused unmount must keep the drive marked as streaming"
        )
        sb.ok("stream", remote="CloudA", on=False, force=True)
    finally:
        holder.kill()
        holder.wait()
    wait(lambda: not os.path.ismount(mp), 10, "lazy unmount")


def test_keep_local_sync(sb: Sandbox) -> None:
    # Created by guacd at startup, while it also sets up its private socket
    assert sb.local_root.stat().st_mode & 0o777 == 0o755, "the sync root must be usable"
    data = sb.ok("add_folder", remote="CloudB", path="docs")
    fid, local = data["id"], Path(data["local"])
    wait(lambda: sb.folder(fid)["state"] in ("synced", "changes"), 20, "first sync")
    assert (local / "sub" / "two.md").read_text() == "deep\n"

    # A local edit reaches the cloud within seconds, without asking
    (local / "one.md").write_text("doc one\nedited locally\n")
    (local / "sub" / "four.md").write_text("new local file\n")
    wait(
        lambda: "edited locally" in (sb.remote_b / "docs" / "one.md").read_text(),
        20,
        "local edit uploaded",
    )
    wait(
        lambda: (sb.remote_b / "docs" / "sub" / "four.md").exists(),
        10,
        "new local file uploaded",
    )

    # A cloud edit arrives on the next sync
    (sb.remote_b / "docs" / "sub" / "two.md").write_text("deep\ncloud edit\n")
    time.sleep(1.1)
    sb.ok("sync_now", id=fid)
    wait(
        lambda: "cloud edit" in (local / "sub" / "two.md").read_text(),
        20,
        "cloud edit downloaded",
    )

    # A single deletion propagates (bisync's 50% safety threshold isn't hit)
    time.sleep(1)
    (local / "sub" / "three.md").unlink()
    wait(
        lambda: not (sb.remote_b / "docs" / "sub" / "three.md").exists(),
        20,
        "deletion propagated",
    )
    sb._fid = fid  # for the following scenarios


def test_conflict(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    wait(lambda: sb.folder(fid)["state"] == "synced", 30, "settled before conflict")
    (sb.remote_b / "docs" / "one.md").write_text("cloud version\n")
    time.sleep(1.2)
    (local / "one.md").write_text("local version\n")
    wait(lambda: sb.folder(fid)["conflictCount"] > 0, 30, "conflict detected")
    assert (local / "one.md").read_text() == "local version\n", "the newer side wins"
    loser = local / "one.conflict1.md"
    assert loser.exists() and loser.read_text() == "cloud version\n", (
        "the other version is kept with its extension"
    )
    wait(
        lambda: (sb.remote_b / "docs" / "one.conflict1.md").exists(),
        15,
        "conflict copy synced to the cloud",
    )


def test_safety_stop(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    wait(lambda: sb.folder(fid)["state"] == "synced", 30, "settled before mass delete")
    before = sorted(p.name for p in (sb.remote_b / "docs").rglob("*") if p.is_file())
    for p in list(local.rglob("*")):
        if p.is_file():
            p.unlink()
    wait(lambda: sb.folder(fid)["state"] == "attention", 30, "safety stop")
    assert sb.folder(fid)["attentionCode"] in ("safety", "empty"), sb.folder(fid)
    after = sorted(p.name for p in (sb.remote_b / "docs").rglob("*") if p.is_file())
    assert after == before, (
        "nothing may be deleted in the cloud by a mass local deletion"
    )
    sb.ok("resolve", id=fid, action="resync")
    wait(
        lambda: sorted(p.name for p in local.rglob("*") if p.is_file()) == before,
        30,
        "resync restored files",
    )


def test_recent(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    mp = Path(sb.drive("CloudA")["mountPath"])
    (mp / "Photos" / "p.jpg").read_text()
    sb.ok("touch")
    wait(
        lambda: any(
            r["where"] == "stream" and r["name"] == "p.jpg"
            for r in sb.state()["recent"]
        ),
        20,
        "file opened through the mount listed as recent",
    )
    assert any(r["where"] == "local" for r in sb.state()["recent"]), (
        "local folder files listed as recent"
    )


def test_engine_recovery(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    mp = Path(sb.drive("CloudA")["mountPath"])
    subprocess.run(
        ["pkill", "-f", f"rclone rcd --rc-addr=unix://{sb.run}"], check=False
    )
    # Until guacd notices, its last state still says "mounted": judge by the mount itself
    wait(
        lambda: (
            sb.state()["engine"]["state"] != "running"
            or not sb.drive("CloudA")["mounted"]
        ),
        10,
        "crash noticed",
    )
    wait(
        lambda: (
            sb.state()["engine"]["state"] == "running"
            and sb.drive("CloudA")["mounted"]
            and (mp / "a.txt").read_text() == "hello A\n"
        ),
        30,
        "rclone restarted and drive remounted",
    )


def test_takeover(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudB", on=False)
    mp = Path(sb.drive("CloudB")["mountPath"])
    mp.mkdir(parents=True, exist_ok=True)
    # The resolved path: rclone's own --daemon readiness check doesn't follow symlinks
    subprocess.run(
        ["rclone", "mount", "CloudB:", os.path.realpath(mp), "--daemon"],
        env=sb.env,
        check=True,
    )
    reply = sb.request("stream", remote="CloudB", on=True)
    assert not reply["ok"] and sb.drive("CloudB")["mountState"] == "foreign", reply
    sb.ok("takeover", remote="CloudB")
    assert sb.drive("CloudB")["mounted"]
    wait(
        lambda: (
            subprocess.run(
                ["pgrep", "-f", f"rclone mount CloudB: {os.path.realpath(mp)}"],
                capture_output=True,
            ).returncode
            != 0
        ),
        10,
        "the other rclone mount exited",
    )


def test_parallel_sync(sb: Sandbox) -> None:
    names = ("par1", "par2")
    for name in names:
        (sb.remote_b / name).mkdir()
        # A few files, so changing one isn't bisync's "all files were changed" safety stop
        for doc in ("shared.md", "a.md", "b.md"):
            (sb.remote_b / name / doc).write_text("start\n")
    ids = [sb.ok("add_folder", remote="CloudB", path=name)["id"] for name in names]
    for fid in ids:
        wait(lambda: sb.folder(fid)["state"] == "synced", 30, "first sync")
    locals_ = [Path(sb.folder(fid)["local"]) for fid in ids]

    # Enough new files that each sync is still running when the other starts
    for name in names:
        for i in range(400):
            (sb.remote_b / name / f"f{i}.txt").write_text(f"{name} {i}\n")
    # A conflict in par1 only: it must not be reported on par2
    (sb.remote_b / "par1" / "shared.md").write_text("cloud version\n")
    time.sleep(1.2)
    log = sb.tmp / "state" / "guacamole" / "daemon.log"
    mark = log.stat().st_size
    (locals_[0] / "shared.md").write_text("local version\n")
    sb.ok("sync_now")

    def since_mark() -> str:
        return log.read_bytes()[mark:].decode(errors="replace")

    wait(
        lambda: all(f"sync {fid} done" in since_mark() for fid in ids),
        60,
        "both folders synced",
    )
    text = since_mark()
    first_done = min(text.index(f"sync {fid} done") for fid in ids)
    assert all(text.index(f"sync {fid} (CloudB:") < first_done for fid in ids), (
        "folders of the same drive sync at the same time"
    )
    wait(
        lambda: sb.folder(ids[0])["conflictCount"] > 0, 15, "conflict reported on par1"
    )
    assert sb.folder(ids[1])["conflictCount"] == 0, (
        "par1's conflict must not be reported on par2"
    )
    for local in locals_:
        assert len(list(local.glob("f*.txt"))) == 400, local
    wait(
        lambda: (
            not list((sb.run / "guacamole").glob("sync-*.sock"))
            and subprocess.run(
                [
                    "pgrep",
                    "-f",
                    f"rclone rcd --rc-addr=unix://{sb.run}/guacamole/sync-",
                ],
                capture_output=True,
            ).returncode
            != 0
        ),
        30,
        "each sync's rclone process ends with it",
    )
    for fid in ids:
        sb.ok("remove_folder", id=fid)


def test_remove_folder_keeps_files(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    sb.ok("remove_folder", id=fid)
    assert local.is_dir() and any(local.rglob("*.md")), (
        "local files stay when a folder stops syncing"
    )
    assert all(f["id"] != fid for d in sb.state()["drives"] for f in d["folders"])


def test_validation(sb: Sandbox) -> None:
    for local, why in (
        (str(Path.home()), "whole home"),
        (str(sb.mnt / "x"), "inside the mount root"),
        ("/etc/x", "system"),
    ):
        reply = sb.request("add_folder", remote="CloudA", path="Photos", local=local)
        assert not reply["ok"], f"{why} must be refused"
    reply = sb.request("browse", remote="CloudA", path="../etc")
    assert not reply["ok"], "'..' must be refused"


def test_move_roots(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    old_mp = Path(sb.drive("CloudA")["mountPath"])
    sb.ok("stream", remote="CloudB", on=False)
    stale = Path(sb.drive("CloudB")["mountPath"])  # not streamed: only an empty folder
    stale.mkdir(parents=True, exist_ok=True)
    new_root = sb.tmp / "m2"
    sb.ok("set_setting", key="mount_root", value=str(new_root))
    assert not stale.exists(), (
        "an unmounted drive's empty folder doesn't stay at the old root"
    )
    wait(
        lambda: (
            sb.drive("CloudA")["mounted"]
            and sb.drive("CloudA")["mountPath"] == str(new_root / "CloudA")
            and os.path.ismount(new_root / "CloudA")
        ),
        20,
        "drive moved to the new stream root",
    )
    assert not old_mp.exists(), "the old mount folder is cleaned up"
    assert (new_root / "CloudA" / "a.txt").read_text() == "hello A\n"
    # Stream and sync roots can't nest; siblings share a parent that "open" shows
    for key, value in (
        ("local_root", str(new_root / "x")),
        ("mount_root", str(sb.local_root / "x")),
    ):
        reply = sb.request("set_setting", key=key, value=value)
        assert not reply["ok"] and "separate" in reply["error"], reply
    assert sb.state()["cloudRoot"] == str(sb.tmp), "sibling roots: open their parent"
    sb.ok("set_setting", key="local_root", value=str(sb.tmp / "deeper" / "sync"))
    assert (sb.tmp / "deeper" / "sync").is_dir(), "the sync root is created right away"
    assert sb.state()["cloudRoot"] == str(new_root), (
        "unrelated roots: open the stream root"
    )
    sb.ok("set_setting", key="local_root", value=str(sb.local_root))
    third = sb.tmp / "m3"
    sb.ok("set_setting", key="mount_root", value=str(third))
    wait(
        lambda: sb.drive("CloudA")["mounted"] and os.path.ismount(third / "CloudA"),
        20,
        "drive moved again",
    )
    assert not (new_root / "CloudA").exists()


def test_commands(sb: Sandbox) -> None:
    guide = sb.ok("client_guide", provider="dropbox")["guide"]
    assert guide["provider"] == "dropbox" and guide["steps"] and guide["id"]["pattern"]
    for cmd, args in (
        ("client_guide", {"remote": "CloudA"}),
        ("dismiss_hint", {"remote": "CloudA", "code": "shared_app"}),
        (
            "set_client_id",
            {"remote": "CloudA", "client_id": "a" * 20, "client_secret": "b" * 20},
        ),
    ):
        reply = sb.request(cmd, **args)
        assert not reply["ok"], f"{cmd} must be refused for an alias remote"
    reply = sb.request(
        "add_oauth",
        name="Bad",
        provider="drive",
        client_id="nope",
        client_secret="x" * 20,
    )
    assert not reply["ok"] and reply.get("code") == "bad_client", reply
    assert not sb.state()["auth"]["busy"], "a rejected client never starts a sign-in"
    for source, name in (("daemon", "daemon.log"), ("rclone", "rclone.log")):
        data = sb.ok("log", source=source, lines=20)
        assert data["path"].endswith(name) and (data["lines"] or source == "rclone"), (
            source
        )
    # Replaced files are counted, so the widget can say when there are none
    backups = Path(sb.state()["paths"]["backups"])
    before = sb.state()["backupFiles"]
    (backups / "f0" / "2026-10-04").mkdir(parents=True, exist_ok=True)
    (backups / "f0" / "2026-10-04" / "old.md").write_text("old\n")
    wait(lambda: sb.state()["backupFiles"] == before + 1, 15, "backup counted")


def test_shutdown_unmounts(sb: Sandbox) -> None:
    sb.ok("stream", remote="CloudA", on=True)
    mp = sb.drive("CloudA")["mountPath"]
    sb.stop()
    assert not os.path.ismount(mp), "stopping guacd unmounts its drives"
    assert (
        subprocess.run(
            ["pgrep", "-f", f"rclone rcd --rc-addr=unix://{sb.run}"],
            capture_output=True,
        ).returncode
        != 0
    ), "rclone stops with guacd"
    sb.start()
    wait(
        lambda: sb.drive("CloudA")["mounted"],
        20,
        "streaming state restored after restart",
    )


SCENARIOS = [
    test_stream,
    test_busy_unmount,
    test_keep_local_sync,
    test_conflict,
    test_safety_stop,
    test_recent,
    test_engine_recovery,
    test_takeover,
    test_parallel_sync,
    test_remove_folder_keeps_files,
    test_validation,
    test_move_roots,
    test_commands,
    test_shutdown_unmounts,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-k", default="", help="only run scenarios whose name contains this"
    )
    args = ap.parse_args()
    if not shutil.which("rclone") or not shutil.which("fusermount3"):
        print("SKIP: needs rclone and fusermount3")
        return 0
    sb = Sandbox()
    failed = 0
    try:
        sb.start()
        for scenario in SCENARIOS:
            name = scenario.__name__
            if args.k and args.k not in name:
                continue
            t0 = time.monotonic()
            try:
                scenario(sb)
                print(f"PASS {name} ({time.monotonic() - t0:.1f}s)")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    finally:
        if failed:
            print(f"logs kept in {sb.tmp}/state/guacamole/ (daemon.log, rclone.log)")
        sb.cleanup(keep=bool(failed))
    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
