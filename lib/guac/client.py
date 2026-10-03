"""Talking to guacd from the CLI, and starting it when it isn't running."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "guac"
# The daemon outlives whichever Python started it: prefer the system one over version managers'
PYTHON = (
    "/usr/bin/python3" if os.access("/usr/bin/python3", os.X_OK) else sys.executable
)
UNIT = "guacamole.service"
# Session variables the daemon needs to open a browser (OAuth) and send notifications
_PASS_ENV = [
    "PATH",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
    "DBUS_SESSION_BUS_ADDRESS",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "BROWSER",
    "RCLONE_CONFIG",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "LANG",
]


class DaemonError(Exception):
    pass


class Connection:
    def __init__(self, timeout: float = 10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(paths.daemon_socket()))
        except OSError as e:
            self.sock.close()
            raise DaemonError(f"guacd is not running ({e})") from e
        self.file = self.sock.makefile("rb")
        self._next_id = 1
        self.state: Optional[Dict[str, Any]] = None

    def close(self) -> None:
        try:
            self.file.close()
            self.sock.close()
        except OSError:
            pass

    def read_message(self) -> Dict[str, Any]:
        line = self.file.readline()
        if not line:
            raise DaemonError("guacd closed the connection")
        msg = json.loads(line)
        if msg.get("type") == "state":
            self.state = msg.get("data")
        return msg

    def first_state(self) -> Dict[str, Any]:
        while self.state is None:
            self.read_message()
        return self.state

    def request(
        self, cmd: str, args: Optional[Dict[str, Any]] = None, timeout: float = 120.0
    ) -> Dict[str, Any]:
        mid = self._next_id
        self._next_id += 1
        self.sock.sendall(
            json.dumps({"id": mid, "cmd": cmd, "args": args or {}}).encode() + b"\n"
        )
        self.sock.settimeout(timeout)
        while True:
            msg = self.read_message()
            if msg.get("type") == "reply" and msg.get("id") == mid:
                return msg


def is_running() -> bool:
    try:
        conn = Connection(timeout=3)
    except DaemonError:
        return False
    try:
        conn.first_state()
        return True
    except (DaemonError, OSError, ValueError):
        return False
    finally:
        conn.close()


def _systemd_env() -> Dict[str, str]:
    """Environment for talking to the user's systemd: it lives in the standard runtime dir,
    whatever XDG_RUNTIME_DIR the caller has (the daemon still gets the caller's via --setenv)."""
    env = dict(os.environ)
    standard = f"/run/user/{os.getuid()}"
    if os.path.isdir(standard):
        env["XDG_RUNTIME_DIR"] = standard
    return env


def _systemctl(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_systemd_env(),
    )


def _systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        res = _systemctl("is-system-running", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.stdout.strip() in ("running", "degraded", "starting", "initializing")


def start(wait: float = 15.0) -> str:
    """Start guacd outside the caller's process tree; returns how it was started."""
    if is_running():
        return "running"
    env_args = [f"--setenv={k}={os.environ[k]}" for k in _PASS_ENV if os.environ.get(k)]
    how = ""
    if paths.systemd_unit_file().exists() and _systemd_user_available():
        _systemctl("start", UNIT, timeout=30)
        how = "systemd"
    elif shutil.which("systemd-run") and _systemd_user_available():
        # A transient user unit: independent of the shell (restarting it keeps drives mounted),
        # stopped cleanly at logout, logs in the journal. Nothing is written to disk.
        _systemctl("reset-failed", UNIT, timeout=10)
        res = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                f"--unit={UNIT}",
                "--description=Clouds Guacamole",
                "--collect",
                "--property=KillMode=mixed",
                "--property=TimeoutStopSec=45",
                *env_args,
                "--",
                PYTHON,
                str(LAUNCHER),
                "daemon",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=_systemd_env(),
        )
        how = "systemd-run" if res.returncode == 0 else ""
    if not how:
        subprocess.Popen(
            [PYTHON, str(LAUNCHER), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        how = "detached"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_running():
            return how
        time.sleep(0.2)
    raise DaemonError(
        f"guacd did not come up (started via {how}); see {paths.daemon_log()}"
    )


def connect(autostart: bool = True) -> Connection:
    try:
        conn = Connection()
    except DaemonError:
        if not autostart:
            raise
        start()
        conn = Connection()
    conn.first_state()
    return conn
