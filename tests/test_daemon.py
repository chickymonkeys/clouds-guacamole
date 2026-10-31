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
import re
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
        self._fid = (
            ""  # the folder test_keep_local_sync keeps, for the scenarios after it
        )

    def start(self) -> None:
        # The daemon keeps its own copy of the descriptor
        with open(self.tmp / "daemon.out", "ab") as log:
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
            if mp.startswith((str(self.tmp), str(self.run))):
                subprocess.run(
                    ["fusermount3", "-uz", mp], capture_output=True, check=False
                )
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


def guac_cli(sb: Sandbox, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "guac"), *argv],
        env=sb.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def sync_running(sb: Sandbox, fid: str) -> bool:
    return (sb.run / "guacamole" / f"sync-{fid}.sock").exists()


def settle(sb: Sandbox, fid: str, quiet: float = 12.0) -> None:
    """Wait out the follow-up check a sync's own downloads schedule, so the next scenario
    doesn't race it."""
    log = sb.tmp / "state" / "guacamole" / "daemon.log"

    def lines() -> int:
        return log.read_text(errors="replace").count(f"sync {fid} ")

    seen, since = lines(), time.monotonic()
    deadline = since + 60
    while time.monotonic() - since < quiet or sync_running(sb, fid):
        assert time.monotonic() < deadline, "folder never went quiet"
        time.sleep(0.3)
        if (n := lines()) != seen:
            seen, since = n, time.monotonic()


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


def test_folder_alias(sb: Sandbox) -> None:
    fid = sb._fid
    f = sb.folder(fid)
    assert f["name"] == f["defaultName"] == "docs" and f["label"] == "", f
    sb.ok("set_folder", id=fid, label="  Team docs  ")
    f = sb.folder(fid)
    assert f["name"] == f["label"] == "Team docs" and f["defaultName"] == "docs", f
    cfg_file = sb.tmp / "cfg" / "guacamole" / "config.json"
    spec = json.loads(cfg_file.read_text())["remotes"]["CloudB"]["folders"][0]
    assert spec["label"] == "Team docs", "the alias is saved"

    # The CLI sets it, finds the folder by it and shows it
    def guac(*argv: str) -> subprocess.CompletedProcess:
        return guac_cli(sb, *argv)

    r = guac("alias", "CloudB:docs", "Shared notes")
    assert r.returncode == 0, r.stderr
    assert sb.folder(fid)["name"] == "Shared notes"
    r = guac("pause", "Shared notes")
    assert r.returncode == 0 and sb.folder(fid)["paused"], r.stderr
    r = guac("resume", "Shared notes")
    assert r.returncode == 0 and not sb.folder(fid)["paused"], r.stderr
    r = guac("status")
    assert r.returncode == 0 and "Shared notes docs → " in r.stdout, r.stdout

    # Empty, or the folder's own name, falls back to the folder's name
    r = guac("alias", "Shared notes")
    assert r.returncode == 0, r.stderr
    f = sb.folder(fid)
    assert f["name"] == "docs" and f["label"] == "", f
    sb.ok("set_folder", id=fid, label="docs")
    assert sb.folder(fid)["label"] == ""
    spec = json.loads(cfg_file.read_text())["remotes"]["CloudB"]["folders"][0]
    assert spec["label"] is None, "a cleared alias isn't saved"
    reply = sb.request("set_folder", id="nope", label="x")
    assert not reply["ok"], "an unknown folder is refused"


def test_excludes(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    cloud = sb.remote_b / "docs"
    log = sb.tmp / "state" / "guacamole" / "daemon.log"
    filters_txt = sb.tmp / "cfg" / "guacamole" / "filters.txt"
    cfg_file = sb.tmp / "cfg" / "guacamole" / "config.json"
    original = filters_txt.read_text()
    glob = sb.state()["filters"]["excludes"]
    assert "*.tmp" in glob and ".DS_Store" in glob, glob

    def settle() -> None:
        wait(lambda: sb.folder(fid)["state"] == "synced", 40, "settled")

    def resynced(mark: int) -> bool:
        return (
            re.search(rf"sync {fid} \(.*\) started \[resync\]", log.read_text()[mark:])
            is not None
        )

    def stays_quiet(mark: int) -> None:
        time.sleep(7)  # past the 4s debounce
        assert not sb.folder(fid)["localChanges"], "a never-synced file woke the syncer"
        assert f"sync {fid} (" not in log.read_text()[mark:], "a sync started"

    try:
        settle()
        # Global list: saved into filters.txt, comments kept, one safe resync
        mark = len(log.read_text())
        data = sb.ok("set_excludes", patterns=glob + ["*.log"])
        assert data["excludes"][-1] == "*.log"
        assert "# rclone filter syntax" in filters_txt.read_text()
        assert filters_txt.read_text().rstrip().endswith("- *.log")
        wait(lambda: resynced(mark), 20, "resync after a global change")
        settle()

        # Excluded both ways, and never wakes the syncer
        mark = len(log.read_text())
        (local / "debug.log").write_text("local only\n")
        (local / "sub" / "deep.log").write_text("local only\n")
        stays_quiet(mark)
        (cloud / "server.log").write_text("cloud only\n")
        time.sleep(1.1)
        sb.ok("sync_now", id=fid)
        time.sleep(1)
        settle()
        assert (
            not (cloud / "debug.log").exists() and not (local / "server.log").exists()
        )

        # A folder's own list replaces the global one: *.log syncs again, *.bak doesn't
        mark = len(log.read_text())
        assert sb.request("set_excludes", id=fid, patterns=["[oops"])["ok"] is False
        sb.ok("set_excludes", id=fid, patterns=["*.bak", "- *.bak", " *.old "])
        assert sb.folder(fid)["excludes"] == ["*.bak", "*.old"]
        spec = json.loads(cfg_file.read_text())["remotes"]["CloudB"]["folders"][0]
        assert spec["excludes"] == ["*.bak", "*.old"], spec
        wait(lambda: resynced(mark), 20, "resync after the folder's own list")
        wait(lambda: (cloud / "debug.log").exists(), 20, "no longer excluded: uploaded")
        wait(
            lambda: (local / "server.log").exists(),
            20,
            "no longer excluded: downloaded",
        )
        settle()
        mark = len(log.read_text())
        (local / "draft.bak").write_text("local only\n")
        stays_quiet(mark)

        # Empty falls back to global; the resync deletes nothing
        mark = len(log.read_text())
        r = guac_cli(sb, "exclude", fid, "--global")
        assert r.returncode == 0 and "global" in r.stdout, r.stdout + r.stderr
        assert sb.folder(fid)["excludes"] == []
        spec = json.loads(cfg_file.read_text())["remotes"]["CloudB"]["folders"][0]
        assert spec["excludes"] is None, spec
        wait(lambda: resynced(mark), 20, "resync back on the global list")
        settle()
        assert (cloud / "debug.log").exists() and (local / "draft.bak").exists()
        r = guac_cli(sb, "exclude", fid, "--add", "*.psd")
        assert r.returncode == 0, r.stderr
        own = sb.folder(fid)["excludes"]
        assert own[-1] == "*.psd" and "*.log" in own, (
            "a first change starts from global"
        )
        r = guac_cli(sb, "exclude", fid)
        assert r.returncode == 0 and "own list" in r.stdout and "*.psd" in r.stdout
        sb.ok("set_excludes", id=fid, patterns=[])
        settle()

        # filters.txt edited by hand still works
        mark = len(log.read_text())
        filters_txt.write_text(filters_txt.read_text() + "- *.raw\n")
        wait(lambda: "*.raw" in sb.state()["filters"]["excludes"], 20, "hand edit read")
        wait(lambda: resynced(mark), 20, "resync after a hand edit")
        settle()
    finally:
        sb.request("set_excludes", id=fid, patterns=[])
        filters_txt.write_text(original)
        wait(lambda: "*.raw" not in sb.state()["filters"]["excludes"], 20, "restored")
        for p in (
            local / "debug.log",
            local / "sub" / "deep.log",
            local / "server.log",
            local / "draft.bak",
            cloud / "debug.log",
            cloud / "server.log",
            cloud / "draft.bak",
            cloud / "sub" / "deep.log",
        ):
            p.unlink(missing_ok=True)
        time.sleep(1.1)
        sb.request("sync_now", id=fid)
        settle()
        time.sleep(11)  # the follow-up sync after a sync's own writes, then quiet
        settle()


def test_paused_roundtrip(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    cloud = sb.remote_b / "docs"
    log = sb.tmp / "state" / "guacamole" / "daemon.log"
    wait(lambda: sb.folder(fid)["state"] == "synced", 30, "settled before pausing")

    try:
        r = guac_cli(sb, "pause", fid)
        assert r.returncode == 0 and "Paused" in r.stdout, r.stdout + r.stderr
        f = sb.folder(fid)
        assert f["paused"] and f["state"] == "paused" and not f["localChanges"], f
        mark = log.stat().st_size

        # Offline work: edits on both sides, and every way a sync is usually started
        (local / "one.md").write_text("doc one\nwritten offline\n")
        (local / "offline.md").write_text("new while paused\n")
        (cloud / "sub" / "two.md").write_text("deep\ncloud edit while paused\n")
        (cloud / "cloud-new.md").write_text("new in the cloud\n")
        time.sleep(1.1)
        assert "paused" in sb.request("sync_now")["message"]
        r = guac_cli(sb, "sync", fid)
        assert r.returncode != 0 and "paused" in r.stderr, r.stdout + r.stderr
        wait(lambda: sb.folder(fid)["localChanges"], 10, "local edits noticed")
        deadline = time.monotonic() + 10  # well past the 4s debounce after local edits
        while time.monotonic() < deadline:
            assert not sync_running(sb, fid), "no rclone runs for a paused folder"
            time.sleep(0.2)
        since = log.read_bytes()[mark:].decode(errors="replace")
        assert f"sync {fid} (" not in since, since
        assert "written offline" not in (cloud / "one.md").read_text()
        assert (
            not (cloud / "offline.md").exists()
            and not (local / "cloud-new.md").exists()
        )
        assert "while paused" not in (local / "sub" / "two.md").read_text()
        assert sb.folder(fid)["state"] == "paused"
        r = guac_cli(sb, "status")
        assert "paused, changes waiting" in r.stdout, r.stdout
    except BaseException:
        sb.request("pause_folder", id=fid, paused=False)
        raise

    # Resume: one ordinary bisync (no resync, no force) brings both sides together
    mark = log.stat().st_size
    r = guac_cli(sb, "resume", fid)
    assert r.returncode == 0 and "catching up" in r.stdout, r.stdout + r.stderr

    def since_mark() -> str:
        return log.read_bytes()[mark:].decode(errors="replace")

    wait(lambda: f"sync {fid} done" in since_mark(), 30, "catch-up sync")
    text = since_mark()
    catchup = text[: text.index(f"sync {fid} done")]
    assert catchup.count(f"sync {fid} (") == 1, catchup
    assert "[resync]" not in catchup, "catching up is a normal sync"
    assert (cloud / "one.md").read_text() == "doc one\nwritten offline\n"
    assert (cloud / "offline.md").read_text() == "new while paused\n"
    assert (local / "sub" / "two.md").read_text() == "deep\ncloud edit while paused\n"
    assert (local / "cloud-new.md").read_text() == "new in the cloud\n"
    assert not list(local.rglob("*.conflict*")), "nothing conflicted"
    f = sb.folder(fid)
    assert not f["paused"] and f["state"] in ("synced", "changes", "syncing"), f
    wait(lambda: sb.folder(fid)["state"] == "synced", 40, "settled after catch-up")
    settle(sb, fid)


def test_pause_while_syncing(sb: Sandbox) -> None:
    (sb.remote_b / "bulk").mkdir()
    (sb.remote_b / "bulk" / "seed.md").write_text("seed\n")
    fid = sb.ok("add_folder", remote="CloudB", path="bulk")["id"]
    wait(lambda: sb.folder(fid)["state"] == "synced", 30, "first sync")
    local = Path(sb.folder(fid)["local"])
    log = sb.tmp / "state" / "guacamole" / "daemon.log"
    for i in range(3000):
        (sb.remote_b / "bulk" / f"f{i}.txt").write_text(f"{i}\n")
    time.sleep(1.1)
    mark = log.stat().st_size
    sb.ok("sync_now", id=fid)
    wait(lambda: sync_running(sb, fid), 10, "sync started")

    # The sync under way ends normally (stopping bisync part way would force a resync)
    reply = sb.request("pause_folder", id=fid, paused=True)
    assert reply["ok"] and "once the sync under way ends" in reply["message"], reply
    wait(lambda: sb.folder(fid)["state"] == "paused", 60, "sync finished, then paused")
    assert len(list(local.glob("f*.txt"))) == 3000
    (local / "edited-offline.txt").write_text("offline\n")
    time.sleep(8)
    text = log.read_bytes()[mark:].decode(errors="replace")
    assert text.count(f"sync {fid} (") == 1, "nothing starts after pausing"
    assert not sync_running(sb, fid)
    assert not (sb.remote_b / "bulk" / "edited-offline.txt").exists()

    mark = log.stat().st_size
    sb.ok("pause_folder", id=fid, paused=False)
    wait(
        lambda: (sb.remote_b / "bulk" / "edited-offline.txt").exists(),
        30,
        "caught up after resuming",
    )
    wait(lambda: sb.folder(fid)["state"] == "synced", 30, "settled after catch-up")
    text = log.read_bytes()[mark:].decode(errors="replace")
    assert "[resync]" not in text and "failed" not in text, text
    sb.ok("remove_folder", id=fid)


def test_work_offline_roundtrip(sb: Sandbox) -> None:
    fid = sb._fid
    local = Path(sb.folder(fid)["local"])
    cloud = sb.remote_b / "docs"
    log = sb.tmp / "state" / "guacamole" / "daemon.log"
    wait(
        lambda: sb.folder(fid)["state"] == "synced", 30, "settled before going offline"
    )
    (sb.remote_b / "kept").mkdir()
    (sb.remote_b / "kept" / "seed.md").write_text("seed\n")
    paused = sb.ok("add_folder", remote="CloudB", path="kept")["id"]
    wait(lambda: sb.folder(paused)["state"] == "synced", 30, "second folder synced")
    sb.ok("pause_folder", id=paused, paused=True)
    streamed_b = sb.drive("CloudB")["stream"]
    sb.ok("stream", remote="CloudB", on=False)
    sb.ok("stream", remote="CloudA", on=True)
    mount = Path(sb.drive("CloudA")["mountPath"])

    def since(mark: int) -> str:
        return log.read_bytes()[mark:].decode(errors="replace")

    try:
        # Saved just before: inside the 5s write-back, so an upload holds the drive
        (mount / "last-save.txt").write_text("saved before leaving\n")
        r = guac_cli(sb, "offline", "on")
        assert r.returncode == 0 and "offline" in r.stdout, r.stdout + r.stderr
        assert "is on" in guac_cli(sb, "offline").stdout
        wait(
            lambda: (
                sb.drive("CloudA")["mountState"] == "mounted"
                and "still uploading" in sb.drive("CloudA")["error"]
            ),
            5,
            "the drive waits for its upload",
        )
        wait(lambda: sb.drive("CloudA")["mountState"] == "offline", 15, "disconnected")
        assert (sb.remote_a / "last-save.txt").exists(), "the upload went first"
        assert not os.path.ismount(mount) and not sb.drive("CloudA")["error"]
        assert sb.drive("CloudA")["stream"], "its own switch is kept"
        assert sb.drive("CloudB")["mountState"] == "off", "this one was off already"
        f = sb.folder(fid)
        assert f["state"] == "offline" and not f["paused"], f
        assert sb.folder(paused)["state"] == "paused", "a folder's own pause shows"
        mark = log.stat().st_size

        # Edits on both sides, and every way a sync is usually started
        (local / "one.md").write_text("doc one\nwritten on a plane\n")
        (local / "plane.md").write_text("new on a plane\n")
        (cloud / "cloud-side.md").write_text("new in the cloud\n")
        time.sleep(1.1)
        assert "offline" in sb.request("sync_now")["message"]
        r = guac_cli(sb, "sync", fid)
        assert r.returncode != 0 and "offline" in r.stderr, r.stdout + r.stderr
        wait(lambda: sb.folder(fid)["localChanges"], 10, "local edits noticed")
        deadline = time.monotonic() + 10  # well past the 4s debounce after local edits
        while time.monotonic() < deadline:
            assert not sync_running(sb, fid), "no rclone runs while working offline"
            time.sleep(0.2)
        r = guac_cli(sb, "status")
        assert "working offline" in r.stdout, r.stdout
        assert "offline, changes waiting" in r.stdout, r.stdout
        assert "○ offline" in r.stdout, r.stdout

        # Connecting is refused, and a drive can't be brought back behind the switch's back
        reply = sb.request("stream", remote="CloudB", on=True)
        assert not reply["ok"] and "offline" in reply["error"], reply
        assert not sb.drive("CloudB")["stream"]

        # A folder added now waits too, with nothing downloaded
        (sb.remote_b / "later").mkdir()
        (sb.remote_b / "later" / "file.md").write_text("later\n")
        added = sb.ok("add_folder", remote="CloudB", path="later")
        assert "go back online" in added["message"], added
        assert sb.folder(added["id"])["state"] == "offline"
        time.sleep(1)

        # It all outlives a restart: the switch, and knowing that changes wait
        sb.stop()
        sb.start()
        wait(lambda: sb.folder(fid)["state"] == "offline", 10, "still offline")
        assert sb.folder(fid)["localChanges"], "changes waiting are not forgotten"
        assert sb.folder(paused)["state"] == "paused"
        time.sleep(8)  # past the catch-up every folder gets after a start
        assert f"sync {fid} (" not in since(mark), since(mark)
        assert sb.drive("CloudA")["mountState"] == "offline", "no mount after a start"
        assert not os.path.ismount(mount)
        assert "written on a plane" not in (cloud / "one.md").read_text()
        assert not (cloud / "plane.md").exists()
        assert not (local / "cloud-side.md").exists()
        assert not (Path(added["local"]) / "file.md").exists()
    except BaseException:
        sb.ok("set_setting", key="work_offline", value=False)
        sb.ok("pause_folder", id=paused, paused=False)
        sb.ok("stream", remote="CloudA", on=False, force=True)
        sb.ok("stream", remote="CloudB", on=streamed_b)
        raise

    # Turned off: one ordinary bisync brings both sides together, and the pause stays
    mark = log.stat().st_size
    r = guac_cli(sb, "offline", "off")
    assert r.returncode == 0 and "catching up" in r.stdout, r.stdout + r.stderr
    assert "1 paused" in r.stdout, r.stdout
    try:
        wait(lambda: sb.drive("CloudA")["mounted"], 15, "the drive that was on is back")
        assert (mount / "last-save.txt").read_text() == "saved before leaving\n"
        assert sb.drive("CloudB")["mountState"] == "off", (
            "the one that was off stays off"
        )
        wait(lambda: f"sync {fid} done" in since(mark), 30, "catch-up sync")
        wait(lambda: (Path(added["local"]) / "file.md").exists(), 30, "first download")
        text = since(mark)
        catchup = text[: text.index(f"sync {fid} done")]
        # The new folder's first sync is a resync, as every first sync is
        starts = [l for l in catchup.splitlines() if f"sync {fid} (" in l]
        assert len(starts) == 1, catchup
        assert "[resync]" not in starts[0], "catching up is a normal sync"
        assert (cloud / "one.md").read_text() == "doc one\nwritten on a plane\n"
        assert (cloud / "plane.md").read_text() == "new on a plane\n"
        assert (local / "cloud-side.md").read_text() == "new in the cloud\n"
        assert sb.folder(paused)["state"] == "paused", "turning it off is not a resume"
        assert f"sync {paused} (" not in since(mark), since(mark)
        wait(lambda: sb.folder(fid)["state"] == "synced", 40, "settled after catch-up")
        settle(sb, fid)
    finally:
        # Left behind, they would take sync slots from the scenarios after this one
        sb.ok("remove_folder", id=paused)
        sb.ok("remove_folder", id=added["id"])
        sb.ok("stream", remote="CloudA", on=False, force=True)
        sb.ok("stream", remote="CloudB", on=streamed_b)


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
                check=False,
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
        wait(lambda fid=fid: sb.folder(fid)["state"] == "synced", 30, "first sync")
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
                check=False,
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


def test_terminal_security(sb: Sandbox) -> None:
    # Real filesystem -> rclone -> daemon -> CLI, including a terminal stdout.
    from guac import util

    names = (
        "OSC\x1b]52;c;ZmFrZQ==\x07",
        "ST\x1b]52;c;ZmFrZQ==\x1b\\",
        "C1\x9d52;c;ZmFrZQ==\x9c",
        "CSI\x1b[2J\x1b[H",
        "lines\nforged\trow\r\x08\x7f",
        "bidi\u202ereversed\u202c",
        "普通 Résumé",
    )
    # rclone itself maps local C0/DEL names to control pictures. C1 and bidi
    # controls reach the daemon unchanged; the remote/mounted contract test in
    # test_security.py separately covers providers returning raw C0 controls.
    encoding = {code: chr(0x2400 + code) for code in range(32)}
    encoding[127] = "␡"
    remote_names = tuple(name.translate(encoding) for name in names)
    root = sb.remote_a / "security"
    root.mkdir()
    for name in names:
        (root / name / "child").mkdir(parents=True)
    try:
        entries = sb.ok("browse", remote="CloudA", path="security")["dirs"]
        assert {e["path"] for e in entries} == {
            f"security/{n}" for n in remote_names
        }, entries
        assert {e["name"] for e in entries} == {
            util.neutralize_controls(n) for n in names
        }, entries
        # A path containing controls still addresses the original folder.
        nested = sb.ok("browse", remote="CloudA", path=f"security/{names[2]}")["dirs"]
        assert [e["path"] for e in nested] == [f"security/{names[2]}/child"], nested
        expected = {util.neutralize_controls(f"security/{n}") for n in names}
        result = guac_cli(sb, "browse", "CloudA:security")
        assert result.returncode == 0 and set(result.stdout.splitlines()) == expected, (
            result
        )

        master, slave = os.openpty()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "bin" / "guac"),
                    "browse",
                    "CloudA:security",
                ],
                env=sb.env,
                stdout=slave,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        finally:
            os.close(slave)
        try:
            chunks = []
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError as e:
                    if e.errno != 5:  # EIO: all slave descriptors have closed
                        raise
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            output = b"".join(chunks).decode()
            assert result.returncode == 0 and set(output.splitlines()) == expected, (
                result,
                output,
            )
            assert "\x1b" not in output and "\x9d" not in output, repr(output)
        finally:
            os.close(master)
    finally:
        shutil.rmtree(root)


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
            check=False,
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
    test_folder_alias,
    test_excludes,
    test_paused_roundtrip,
    test_pause_while_syncing,
    test_work_offline_roundtrip,
    test_conflict,
    test_safety_stop,
    test_recent,
    test_engine_recovery,
    test_takeover,
    test_parallel_sync,
    test_remove_folder_keeps_files,
    test_validation,
    test_terminal_security,
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
            if args.k and not any(k in name for k in args.k.split(",")):
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
