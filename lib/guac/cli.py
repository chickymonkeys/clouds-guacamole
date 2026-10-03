"""guac: command line for Clouds Guacamole."""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__, client, config, paths, providers, util

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)


def _color(enabled: bool):
    def c(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if enabled else text

    return c


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def print_status(state: Dict[str, Any]) -> None:
    c = _color(sys.stdout.isatty())
    eng = state.get("engine", {})
    print(
        c(BOLD, "Clouds Guacamole")
        + c(
            DIM,
            f"  {state.get('version', '')} · {eng.get('version') or 'rclone'}"
            + ("" if state.get("online") else " · offline"),
        )
    )
    if eng.get("state") != "running":
        print(c(RED, f"  engine {eng.get('state')}: {eng.get('error', '')}"))
    drives = state.get("drives", [])
    if not drives:
        print(
            c(
                DIM,
                "  No drives yet. Add one from the bar widget, or: guac add-oauth MyDrive drive",
            )
        )
        return
    for d in drives:
        ms = d["mountState"]
        mark = {
            "mounted": c(GREEN, "● streaming"),
            "off": c(DIM, "○ not streaming"),
            "mounting": c(YELLOW, "◌ mounting"),
            "unmounting": c(YELLOW, "◌ unmounting"),
            "waiting": c(YELLOW, "◌ waiting"),
            "error": c(RED, "✗ error"),
            "foreign": c(RED, "✗ in use"),
        }.get(ms, ms)
        quota = ""
        if d.get("quotaKnown"):
            quota = f"{util.human_bytes(d['quotaUsed'])} of {util.human_bytes(d['quotaTotal'])} ({d['quotaPercent']}%)"
        print(
            f"\n  {c(BOLD, d['label'])} {c(DIM, d['provider'])}  {mark}  {c(DIM, quota)}"
        )
        print(
            c(DIM, f"    {d['mountPath']}")
            + (c(YELLOW, f"  {d['uploads']} uploading") if d.get("uploads") else "")
        )
        if d.get("error"):
            print(c(RED, f"    {d['error']}"))
        for w in d.get("warnings", []):
            print(c(YELLOW, f"    ! {w['text']}"))
        for f in d.get("folders", []):
            st = f["state"]
            col = {
                "synced": GREEN,
                "syncing": YELLOW,
                "changes": YELLOW,
                "pending": YELLOW,
                "attention": RED,
                "error": RED,
                "paused": DIM,
                "waiting": YELLOW,
            }.get(st, "")
            detail = f"synced {_ago(f['lastSync'])}" if st == "synced" else st
            if st == "syncing" and f.get("progress", {}).get("totalTransfers"):
                p = f["progress"]
                detail = f"syncing {p['transfers']}/{p['totalTransfers']}"
            print(
                f"    {c(DIM, '└')} {f['path'] or '/'} → {f['local']}  {c(col, detail)}  {c(DIM, f['id'])}"
            )
            if f.get("attention"):
                print(c(RED, f"        {f['attention']}"))
            elif f.get("error"):
                print(c(RED, f"        {f['error']}"))
            if f.get("conflictCount"):
                print(
                    c(
                        YELLOW,
                        f"        {f['conflictCount']} conflict(s): "
                        + ", ".join(f["conflicts"][:3]),
                    )
                )
    print()


def _run(
    cmd: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 600.0,
    quiet: bool = False,
) -> Dict[str, Any]:
    try:
        conn = client.connect()
    except client.DaemonError as e:
        sys.exit(f"guac: {e}")
    try:
        reply = conn.request(cmd, args or {}, timeout=timeout)
    except (client.DaemonError, OSError) as e:
        sys.exit(f"guac: {e}")
    finally:
        conn.close()
    if not reply.get("ok"):
        sys.exit(f"guac: {reply.get('error') or 'failed'}")
    if not quiet and reply.get("message"):
        print(reply["message"])
    return reply.get("data") or {}


def _state() -> Dict[str, Any]:
    try:
        conn = client.connect()
    except client.DaemonError as e:
        sys.exit(f"guac: {e}")
    try:
        return conn.first_state()
    finally:
        conn.close()


def _find_folder(spec: str) -> str:
    state = _state()
    for d in state.get("drives", []):
        for f in d.get("folders", []):
            if spec in (
                f["id"],
                f"{f['remote']}:{f['path']}",
                f["local"],
                os.path.abspath(os.path.expanduser(spec)),
            ):
                return f["id"]
    sys.exit(f"guac: no folder kept local matches '{spec}' (see `guac status`)")


def _split_remote(spec: str):
    if ":" not in spec:
        return spec, ""
    remote, path = spec.split(":", 1)
    return remote, path


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().strip()


def print_client_guide(guide: Dict[str, Any]) -> None:
    c = _color(sys.stdout.isatty())
    print(c(BOLD, guide["title"]) + c(DIM, f"  (about {guide['minutes']} minutes)"))
    print(guide["why"])
    for i, step in enumerate(guide["steps"], 1):
        print(f"\n{c(BOLD, f'{i}. ' + step['title'])}\n   {step['text']}")
        if step.get("link"):
            print(f"   {step['link']['label']}: {c(GREEN, step['link']['url'])}")
        for item in step.get("copy", []):
            print(f"   {item['label']}: {c(YELLOW, item['value'])}")
    if guide.get("signin_note"):
        print(f"\n{c(DIM, 'When signing in: ' + guide['signin_note'])}")
    print(c(DIM, f"\nrclone's instructions: {guide['docs']}"))


def _ask_client(guide: Dict[str, Any]) -> Dict[str, str]:
    """Prompt for the client id and secret, checking each like the widget does."""
    if not sys.stdin.isatty():
        sys.exit("guac: pass --client-id (the secret is then read from stdin)")

    def ask(spec: Dict[str, Any], secret: bool) -> str:
        while True:
            prompt = f"{spec['label']}: "
            value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
            if not value:
                sys.exit("guac: cancelled")
            if spec.get("reject") and re.search(spec["reject"], value):
                print(f"  {spec['reject_error']}", file=sys.stderr)
            elif not re.search(spec["pattern"], value):
                print(f"  {spec['error']}", file=sys.stderr)
            else:
                return value

    print()
    try:
        return {"client_id": ask(guide["id"], False), "client_secret": ask(guide["secret"], True)}
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nguac: cancelled")


SERVICE_TEMPLATE = """[Unit]
Description=Clouds Guacamole (stream and keep-local cloud drives)
After=graphical-session.target network-online.target

[Service]
Type=simple
ExecStart={python} {launcher} daemon
KillMode=mixed
TimeoutStopSec=45
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def cmd_service(action: str) -> None:
    unit = paths.systemd_unit_file()
    if action == "install":
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            SERVICE_TEMPLATE.format(python=client.PYTHON, launcher=client.LAUNCHER)
        )
        if client.is_running():
            _run("shutdown", quiet=True)
            time.sleep(1)
        client._systemctl("daemon-reload")
        client._systemctl("enable", "--now", client.UNIT, timeout=60)
        print(f"Installed and started {unit}")
    elif action == "uninstall":
        client._systemctl("disable", "--now", client.UNIT, timeout=60)
        try:
            unit.unlink()
        except FileNotFoundError:
            pass
        client._systemctl("daemon-reload")
        print("Removed the systemd unit; the widget will start guacd on demand")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(
        prog="guac",
        description="Stream cloud drives and keep chosen folders on this device.",
    )
    p.add_argument("--version", action="version", version=f"guac {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", help="Drives, folders kept local and their state")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser(
        "daemon",
        help="Run guacd in the foreground (or --ensure it runs in the background)",
    )
    s.add_argument(
        "--ensure",
        action="store_true",
        help="Start guacd in the background unless it is running",
    )
    s.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("stop", help="Stop guacd (unmounts drives)")
    sub.add_parser("watch", help="Print state updates as JSON lines")

    s = sub.add_parser("stream", help="Stream (mount) a drive, or stop streaming it")
    s.add_argument("remote")
    s.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    s.add_argument(
        "--force",
        action="store_true",
        help="Unmount even with uploads pending or files open",
    )
    s = sub.add_parser("mount", help="Same as: stream REMOTE on")
    s.add_argument("remote")
    s = sub.add_parser("unmount", help="Same as: stream REMOTE off")
    s.add_argument("remote")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("retry", help="Retry mounting a drive now")
    s.add_argument("remote")
    s = sub.add_parser(
        "takeover",
        help="Unmount another program's mount at a drive's location and stream it here",
    )
    s.add_argument("remote")
    s = sub.add_parser("stream-all", help="Stream every drive, or none")
    s.add_argument("state", choices=["on", "off"])

    s = sub.add_parser("keep", help="Keep a cloud folder on this device (two-way sync)")
    s.add_argument("spec", help="REMOTE:PATH, e.g. Drive:Documents/Notes")
    s.add_argument(
        "local",
        nargs="?",
        default="",
        help="Local folder (default: <local_root>/REMOTE/PATH)",
    )
    s = sub.add_parser(
        "unkeep",
        help="Stop keeping a folder local (local files stay unless --delete-local)",
    )
    s.add_argument("folder", help="Folder id, REMOTE:PATH or local path")
    s.add_argument(
        "--delete-local",
        action="store_true",
        help="Also move the local copy to the trash",
    )
    s = sub.add_parser("sync", help="Sync folders kept local now")
    s.add_argument("folder", nargs="?", default="")
    s = sub.add_parser("pause", help="Pause syncing a folder")
    s.add_argument("folder")
    s = sub.add_parser("resume", help="Resume syncing a folder")
    s.add_argument("folder")
    s = sub.add_parser("resolve", help="Act on a folder that needs attention")
    s.add_argument("folder")
    s.add_argument("action", choices=["force", "resync", "dismiss"])
    s = sub.add_parser("browse", help="List the folders inside REMOTE:PATH")
    s.add_argument("spec")
    s = sub.add_parser("size", help="Size of REMOTE:PATH")
    s.add_argument("spec")

    s = sub.add_parser("add-oauth", help="Connect a drive through browser sign-in")
    s.add_argument("name")
    s.add_argument(
        "provider", choices=["drive", "onedrive", "dropbox", "box", "pcloud"]
    )
    s.add_argument(
        "--own-client",
        action="store_true",
        help="Walk through creating your own OAuth client, then use it",
    )
    s.add_argument("--client-id", default="")
    s.add_argument(
        "--client-secret-stdin",
        action="store_true",
        help="Read your OAuth client secret from stdin",
    )
    s.add_argument("--mount-path", default="")
    s.add_argument("--no-stream", action="store_true")
    s = sub.add_parser(
        "client-guide",
        help="How to create your own OAuth client (drive, dropbox, onedrive, box)",
    )
    s.add_argument("provider", choices=sorted(providers.CLIENT_GUIDES))
    s = sub.add_parser(
        "add-credentials",
        help="Connect Nextcloud, WebDAV, S3 or Proton (options as JSON on stdin)",
    )
    s.add_argument("name")
    s.add_argument("provider", choices=["nextcloud", "webdav", "s3", "protondrive"])
    s.add_argument("--mount-path", default="")
    s.add_argument("--no-stream", action="store_true")
    s = sub.add_parser(
        "set-client-id",
        help="Use your own OAuth client for a drive; without --client-id, a guided setup",
    )
    s.add_argument("remote")
    s.add_argument("--client-id", default="", help="The secret is then read from stdin")
    s = sub.add_parser("reconnect", help="Sign in to a drive again")
    s.add_argument("remote")
    s = sub.add_parser("remove", help="Remove a drive's account (local copies stay)")
    s.add_argument("remote")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("label", help="Display name for a drive")
    s.add_argument("remote")
    s.add_argument("label")
    s = sub.add_parser("set-path", help="Where a drive is mounted (empty: default)")
    s.add_argument("remote")
    s.add_argument("path")
    s = sub.add_parser("config", help="Show or change settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s = sub.add_parser("log", help="Recent rclone log lines")
    s.add_argument("-n", "--lines", type=int, default=80)
    s.add_argument("--daemon", action="store_true", help="guacd's own log instead")
    s = sub.add_parser(
        "open", help="Open a drive (or the cloud folder) in the file manager"
    )
    s.add_argument("remote", nargs="?", default="")
    s = sub.add_parser(
        "service",
        help="Install a systemd user unit so guacd starts at login without the widget",
    )
    s.add_argument("action", choices=["install", "uninstall"])

    a = p.parse_args(argv)
    if a.cmd is None:
        a.cmd, a.json = "status", False

    if a.cmd == "daemon":
        if a.ensure:
            try:
                how = client.start()
            except client.DaemonError as e:
                sys.exit(f"guac: {e}")
            print(
                f"guacd {'is running' if how == 'running' else 'started (' + how + ')'}"
            )
            return
        from . import daemon

        sys.exit(daemon.main(verbose=a.verbose))

    if a.cmd == "status":
        state = _state()
        if a.json:
            print(json.dumps(state, indent=2))
        else:
            print_status(state)
    elif a.cmd == "stop":
        if not client.is_running():
            print("guacd is not running")
            return
        _run("shutdown")
    elif a.cmd == "watch":
        conn = client.connect()
        conn.sock.settimeout(None)
        print(json.dumps(conn.state), flush=True)
        try:
            while True:
                msg = conn.read_message()
                if msg.get("type") == "state":
                    print(json.dumps(msg["data"]), flush=True)
        except KeyboardInterrupt:
            pass
    elif a.cmd in ("stream", "mount", "unmount"):
        on = a.cmd == "mount" or (a.cmd == "stream" and a.state == "on")
        _run(
            "stream",
            {"remote": a.remote, "on": on, "force": getattr(a, "force", False)},
        )
    elif a.cmd in ("retry", "takeover"):
        _run(a.cmd, {"remote": a.remote})
    elif a.cmd == "stream-all":
        _run("stream_all", {"on": a.state == "on"})
    elif a.cmd == "keep":
        remote, path = _split_remote(a.spec)
        _run("add_folder", {"remote": remote, "path": path, "local": a.local})
    elif a.cmd == "unkeep":
        _run(
            "remove_folder",
            {"id": _find_folder(a.folder), "delete_local": a.delete_local},
        )
    elif a.cmd == "sync":
        _run("sync_now", {"id": _find_folder(a.folder)} if a.folder else {})
    elif a.cmd in ("pause", "resume"):
        _run("pause_folder", {"id": _find_folder(a.folder), "paused": a.cmd == "pause"})
    elif a.cmd == "resolve":
        _run("resolve", {"id": _find_folder(a.folder), "action": a.action})
    elif a.cmd == "browse":
        remote, path = _split_remote(a.spec)
        data = _run("browse", {"remote": remote, "path": path}, quiet=True)
        for d in data.get("dirs", []):
            print(d["path"] + ("  [kept local]" if d.get("kept") else ""))
    elif a.cmd == "size":
        remote, path = _split_remote(a.spec)
        data = _run(
            "folder_size", {"remote": remote, "path": path, "timeout": 600}, quiet=True
        )
        print(f"{data['count']} files, {util.human_bytes(data['bytes'])}")
    elif a.cmd == "client-guide":
        print_client_guide(providers.client_guide(a.provider))
    elif a.cmd == "add-oauth":
        guide = providers.client_guide(a.provider)
        creds = {"client_id": a.client_id, "client_secret": ""}
        if a.own_client:
            if guide is None:
                sys.exit(f"guac: rclone has no own-client setup for {a.provider}")
            print_client_guide(guide)
            creds = _ask_client(guide)
        elif a.client_secret_stdin:
            creds["client_secret"] = _read_secret("OAuth client secret: ")
        elif guide and guide["required"] and not a.client_id:
            print(
                f"Note: {guide['name']} needs your own client; rclone's shared one is throttled and "
                f"retiring. Add --own-client for a guided setup.",
                file=sys.stderr,
            )
        print("Opening your browser to sign in…", file=sys.stderr)
        _run(
            "add_oauth",
            dict(
                creds,
                name=a.name,
                provider=a.provider,
                mount_path=a.mount_path,
                stream=not a.no_stream,
            ),
            timeout=400,
        )
    elif a.cmd == "add-credentials":
        if sys.stdin.isatty():
            print(
                'Paste the options as JSON (e.g. {"url": "...", "user": "...", "pass": "..."}), then Ctrl-D:',
                file=sys.stderr,
            )
        raw = sys.stdin.read().strip()
        try:
            options = json.loads(raw) if raw else {}
        except ValueError as e:
            sys.exit(f"guac: options must be JSON ({e.__class__.__name__})")
        _run(
            "add_credentials",
            {
                "name": a.name,
                "provider": a.provider,
                "options": options,
                "mount_path": a.mount_path,
                "stream": not a.no_stream,
            },
        )
    elif a.cmd == "set-client-id":
        if a.client_id:
            creds = {"client_id": a.client_id, "client_secret": _read_secret("OAuth client secret: ")}
        else:
            guide = _run("client_guide", {"remote": a.remote}, quiet=True)["guide"]
            print_client_guide(guide)
            creds = _ask_client(guide)
        print("Opening your browser to sign in with your own client…", file=sys.stderr)
        _run("set_client_id", dict(creds, remote=a.remote), timeout=400)
    elif a.cmd == "reconnect":
        print("Opening your browser to sign in…", file=sys.stderr)
        _run("reconnect", {"remote": a.remote}, timeout=400)
    elif a.cmd == "remove":
        if not a.yes:
            try:
                ok = (
                    input(f"Remove the account '{a.remote}'? Local copies stay. [y/N] ")
                    .strip()
                    .lower()
                )
            except EOFError:
                sys.exit("guac: pass --yes to remove without a prompt")
            if ok not in ("y", "yes"):
                return
        _run("remove_remote", {"remote": a.remote})
    elif a.cmd == "label":
        _run("set_drive", {"remote": a.remote, "label": a.label})
    elif a.cmd == "set-path":
        _run("set_drive", {"remote": a.remote, "mount_path": a.path})
    elif a.cmd == "config":
        if a.key and a.value is not None:
            _run("set_setting", {"key": a.key, "value": a.value})
        else:
            settings = _state().get("settings", {})
            if a.key:
                print(settings.get(a.key))
            else:
                print(json.dumps(settings, indent=2))
    elif a.cmd == "log":
        source = "daemon" if a.daemon else "rclone"
        for line in _run("log", {"lines": a.lines, "source": source}, quiet=True).get("lines", []):
            print(line)
    elif a.cmd == "open":
        state = _state()
        target = state.get("cloudRoot") or state.get("mountRoot") or str(paths.expand(config.DEFAULTS["mount_root"]))
        for d in state.get("drives", []):
            if a.remote and a.remote in (d["name"], d["label"]):
                target = d["mountPath"]
        # gio launches terminal apps in a terminal; xdg-open (generic mode) does not
        opener = ["gio", "open"] if shutil.which("gio") else ["xdg-open"] if shutil.which("xdg-open") else None
        if opener:
            subprocess.Popen(
                opener + [target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            print(target)
    elif a.cmd == "service":
        cmd_service(a.action)
