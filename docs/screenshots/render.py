#!/usr/bin/env python3
"""Render the README screenshots: the real widget, off-screen, against a demo guacd.

The demo guacd speaks guacd's socket protocol and serves made-up drives, folders and files
(built with the real provider catalog), so the screenshots never show a real account and no
rclone, mount or sync is involved. Quickshell runs with a throwaway home, so no real path or
file name can reach a screenshot; only the current Omarchy theme is linked in.

    docs/screenshots/render.py            # writes docs/images/*.png
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "docs" / "images"
SHELL = Path("/usr/share/omarchy/shell")

sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "lib"))
from guac import __version__, config, providers

# Throwaway home: what the widget shows as ~
ROOT = Path(tempfile.mkdtemp(prefix="guac-shots-"))
HOME = ROOT / "home" / "you"
NOW = time.time()
GB = 1024**3
MB = 1024**2


def ago(seconds: float) -> int:
    return int(NOW - seconds)


# ---------------------------------------------------------------- demo data


def folder(remote: str, path: str, fid: str, **kw):
    f = {
        "id": fid,
        "remote": remote,
        "path": path,
        "name": os.path.basename(path),
        "local": str(HOME / "Cloud" / "Sync" / remote / path),
        "state": "synced",
        "paused": False,
        "initialized": True,
        "lastSync": ago(120),
        "lastChanges": 0,
        "nextSync": int(NOW + 180),
        "error": "",
        "errorKind": "",
        "attention": "",
        "attentionCode": "",
        "resync": False,
        "progress": {},
        "conflicts": [],
        "conflictCount": 0,
        "watching": True,
        "watchError": "",
    }
    f.update(kw)
    return f


def drive(
    name: str,
    rtype: str,
    label: str = "",
    vendor: str = "",
    stream: bool = True,
    used: int = 0,
    total: int = 0,
    custom: bool = True,
    folders=(),
    **kw,
):
    prov = providers.detect({"type": rtype, "vendor": vendor})
    remote = {"type": rtype}
    if custom and rtype in providers.OAUTH_TYPES:
        remote["client_id"] = "demo"
    d = {
        "name": name,
        "label": label or name,
        "type": rtype,
        "provider": prov["name"],
        "providerId": prov["id"],
        "glyph": prov["glyph"],
        "color": prov["color"],
        "stream": stream,
        "mountState": "mounted" if stream else "off",
        "mounted": stream,
        "mountPath": str(HOME / "Cloud" / "Stream" / name),
        "customPath": False,
        "error": "",
        "errorKind": "",
        "retrying": False,
        "quotaKnown": total > 0,
        "quotaTotal": total,
        "quotaUsed": used,
        "quotaFree": max(0, total - used),
        "quotaPercent": round(used / total * 100, 1) if total else 0,
        "uploads": 0,
        "cacheBytes": 0,
        "warnings": providers.remote_warnings(remote),
        "customClient": custom and rtype in providers.OAUTH_TYPES,
        "canCustomClient": rtype in providers.CLIENT_GUIDES,
        "folders": list(folders),
    }
    d.update(kw)
    return d


def recent(name: str, remote: str, folder_: str, where: str, age: float, size: int):
    base = (
        HOME / "Cloud" / ("Sync" if where == "local" else "Stream") / remote / folder_
    )
    return {
        "name": name,
        "path": str(base / name),
        "folder": folder_,
        "remote": remote,
        "where": where,
        "modifiedTs": ago(age),
        "sizeBytes": size,
    }


def thesis(**kw):
    return folder("Drive", "Work/Thesis", "5c1e9a02", **kw)


def notes(**kw):
    return folder("Drive", "Documents/Notes", "a7f3d210", lastSync=ago(70), **kw)


def photos(**kw):
    return folder("Dropbox", "Photos/Camera", "e04b6c3f", lastSync=ago(14 * 60), **kw)


SYNCING = {
    "state": "syncing",
    "nextSync": 0,
    "progress": {
        "bytes": 412 * MB,
        "totalBytes": int(1.1 * GB),
        "transfers": 214,
        "totalTransfers": 530,
        "checks": 530,
        "current": "results-03.pdf",
        "speed": int(8.4 * MB),
    },
}

RECENT = [
    recent("chapter-3.md", "Drive", "Work/Thesis/chapters", "local", 60, 48_200),
    recent("meeting-notes.md", "Drive", "Documents/Notes", "local", 25 * 60, 6_100),
    recent("Q3 report.pdf", "Drive", "Work/Reports", "stream", 2 * 3600, 3_400_000),
    recent("IMG_4821.jpg", "Dropbox", "Photos/Camera", "stream", 5 * 3600, 4_100_000),
    recent("budget-2026.xlsx", "Drive", "Finance", "stream", 26 * 3600, 91_000),
]


def gdrive(**kw):
    return drive("Drive", "drive", used=int(61.4 * GB), total=100 * GB, **kw)


def dropbox(**kw):
    return drive("Dropbox", "dropbox", used=int(734 * GB), total=2048 * GB, **kw)


def scene(name: str):
    nextcloud = drive("Nextcloud", "webdav", vendor="nextcloud", stream=False)
    transfers = {"count": 0, "speed": 0, "names": []}
    if name == "overview":
        drives = [
            gdrive(folders=[notes(), thesis(**SYNCING)]),
            dropbox(uploads=2, folders=[photos()]),
            nextcloud,
        ]
        transfers = {
            "count": 3,
            "speed": int(8.9 * MB),
            "names": ["results-03.pdf", "IMG_4890.jpg", "IMG_4891.jpg"],
        }
    elif name == "attention":
        conflicts = ["todo.conflict1.md", "ideas.conflict1.md"]
        safety = (
            "Safety stop: most files changed or were deleted on one side. Check both copies, then choose "
            "Sync anyway (applies it) or Resync (restores, never deletes)."
        )
        drives = [
            gdrive(
                folders=[
                    notes(conflicts=conflicts, conflictCount=2, lastChanges=3),
                    thesis(lastSync=ago(4 * 60)),
                ]
            ),
            dropbox(
                folders=[
                    photos(state="attention", attentionCode="safety", attention=safety)
                ]
            ),
            nextcloud,
        ]
    elif name == "many":
        # One drive keeping nine folders: folded down to the ones that need you
        kept = [
            folder("Drive", p, f"b{i:07x}", lastSync=ago(60 * (i + 2)))
            for i, p in enumerate(
                (
                    "Documents/Papers",
                    "Documents/Receipts",
                    "Obsidian",
                    "Finance/Taxes",
                    "Music/Scores",
                    "Photos/2024",
                )
            )
        ]
        new = folder(
            "Drive",
            "Work/Projects",
            "b7a41e5c",
            state="syncing",
            nextSync=0,
            initialized=False,
            resync=True,
            progress={
                "bytes": 96 * MB,
                "totalBytes": int(3.2 * GB),
                "transfers": 41,
                "totalTransfers": 2210,
                "checks": 2210,
                "current": "site/assets/map.pdf",
                "speed": int(4.2 * MB),
            },
        )
        conflicted = notes(conflicts=["todo.conflict1.md"], conflictCount=1)
        drives = [
            gdrive(folders=[conflicted, thesis(**SYNCING), *kept[:3], new, *kept[3:]]),
            dropbox(folders=[photos()]),
            nextcloud,
        ]
        transfers = {
            "count": 2,
            "speed": int(12.6 * MB),
            "names": ["results-03.pdf", "map.pdf"],
        }
    elif name == "shared":
        drives = [gdrive(custom=False)]
    else:
        raise ValueError(name)

    folders = [f for d in drives for f in d["folders"]]
    states = [f["state"] for f in folders]
    paths = HOME / ".local" / "state" / "guacamole"
    return {
        "version": __version__,
        "engine": {"state": "running", "error": "", "version": "v1.75.1"},
        "online": True,
        "mountRoot": str(HOME / "Cloud" / "Stream"),
        "localRoot": str(HOME / "Cloud" / "Sync"),
        "cloudRoot": str(HOME / "Cloud"),
        "settings": {k: config.DEFAULTS[k] for k in config.SETTABLE},
        "drives": drives,
        "summary": {
            "drives": len(drives),
            "streaming": sum(1 for d in drives if d["stream"]),
            "mounted": sum(1 for d in drives if d["mounted"]),
            "folders": len(folders),
            "syncing": states.count("syncing"),
            "attention": states.count("attention") + states.count("error"),
            "uploads": sum(d["uploads"] for d in drives),
        },
        "transfers": transfers,
        "recent": RECENT if name in ("overview", "attention") else [],
        "auth": {
            "busy": False,
            "waiting": False,
            "url": "",
            "error": "",
            "success": "",
            "remote": "",
        },
        "paths": {
            "log": str(paths / "daemon.log"),
            "rcloneLog": str(paths / "rclone.log"),
            "filters": str(HOME / ".config" / "guacamole" / "filters.txt"),
            "config": str(HOME / ".config" / "guacamole" / "config.json"),
            "backups": str(paths / "backups"),
        },
        "backupFiles": 12,
    }


TREE = {
    "": ["Documents", "Finance", "Music", "Photos", "Shared with me", "Work"],
    "Documents": ["Archive", "Notes", "Papers", "Receipts", "Templates"],
    "Documents/Papers": ["Economics", "Machine learning", "Statistics", "To read"],
}
SIZES = {"Documents/Papers": (1284, int(2.7 * GB))}
KEPT = {"Documents/Notes", "Work/Thesis"}


def browse(path: str):
    entries = []
    for n in TREE.get(path, []):
        p = f"{path}/{n}" if path else n
        entries.append(
            {
                "name": n,
                "path": p,
                "kept": p in KEPT,
                "containsKept": any(k.startswith(p + "/") for k in KEPT),
            }
        )
    return {
        "remote": "Drive",
        "path": path,
        "dirs": entries,
        "keptHere": path in KEPT,
        "containsKept": any(k.startswith(path + "/") or not path for k in KEPT),
        "suggestedLocal": str(HOME / "Cloud" / "Sync" / "Drive" / path),
        "truncated": False,
    }


# ---------------------------------------------------------------- demo guacd


class DemoDaemon:
    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self.state = scene("overview")
        self.clients = []
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(sock_path))
        self.server.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def _send(self, conn, obj):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.lock:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def push(self):
        for c in list(self.clients):
            self._send(c, {"type": "state", "data": self.state})

    def _accept(self):
        while True:
            conn, _ = self.server.accept()
            self.clients.append(conn)
            self._send(conn, {"type": "state", "data": self.state})
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                try:
                    data = self.handle(msg.get("cmd", ""), msg.get("args") or {})
                    reply = {
                        "type": "reply",
                        "id": msg.get("id"),
                        "ok": True,
                        "data": data,
                    }
                except Exception as e:  # noqa: BLE001 - shown in the harness output
                    reply = {
                        "type": "reply",
                        "id": msg.get("id"),
                        "ok": False,
                        "error": str(e),
                    }
                self._send(conn, reply)

    def handle(self, cmd: str, args: dict):
        if cmd == "demo_scene":
            self.state = scene(args["name"])
            self.push()
        elif cmd == "browse":
            return browse(args.get("path", ""))
        elif cmd == "folder_size":
            count, size = SIZES.get(args.get("path", ""), (0, 0))
            return {"count": count, "bytes": size}
        elif cmd == "client_guide":
            rtype = args.get("provider") or next(
                d["type"] for d in self.state["drives"] if d["name"] == args["remote"]
            )
            return {"guide": providers.client_guide(rtype)}
        return {}


# ---------------------------------------------------------------- run


def main() -> int:
    if not shutil.which("qs") or not SHELL.is_dir():
        sys.exit("needs Quickshell (qs) and the Omarchy shell in " + str(SHELL))
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        return render()
    finally:
        shutil.rmtree(ROOT)


def render() -> int:
    # The theme (colors, fonts, rounding) is all the shell reads from home
    for rel in (".config/omarchy", ".local/state/omarchy"):
        if (Path.home() / rel).exists():
            (HOME / rel).parent.mkdir(parents=True, exist_ok=True)
            (HOME / rel).symlink_to(Path.home() / rel)
    shell = ROOT / "shell"
    shell.mkdir()
    shutil.copy(HERE / "shots.qml", shell / "shell.qml")
    for name in ("Commons", "Ui"):
        (shell / name).symlink_to(SHELL / name)
    (shell / "gui").symlink_to(REPO / "ui")
    runtime = ROOT / "run"
    (runtime / "guacamole").mkdir(parents=True, mode=0o700)
    DemoDaemon(runtime / "guacamole" / "daemon.sock")

    drop = (
        "DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    )
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        {
            "HOME": str(HOME),
            "XDG_RUNTIME_DIR": str(runtime),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": "2",
            "SHOTS": str(OUT),
        }
    )
    # Quickshell refuses to start without a Wayland socket path it can see
    real_rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["WAYLAND_DISPLAY"] = os.path.join(
        real_rt, os.environ.get("WAYLAND_DISPLAY", "wayland-1")
    )
    out = subprocess.run(
        ["qs", "-p", str(shell / "shell.qml")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = (out.stdout + out.stderr).splitlines()
    report = [
        l for l in lines if "SHOT" in l or "FAIL" in l or "Error" in l or "qml:" in l
    ]
    print("\n".join(report) or "\n".join(lines[-40:]))
    return (
        0
        if any("SHOT done" in l for l in lines) and not any("FAIL" in l for l in lines)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
