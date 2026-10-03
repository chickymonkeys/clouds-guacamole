"""Where Guacamole keeps its files. Everything follows the XDG base directory spec."""

import os
from pathlib import Path

APP = "guacamole"
# AF_UNIX socket paths are limited to 108 bytes including the terminator
_MAX_SOCKET_PATH = 100


def _xdg(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / APP if base else Path(f"/tmp/{APP}-{os.getuid()}")
    if len(str(path)) + len("/daemon.sock") > _MAX_SOCKET_PATH:
        path = Path(f"/tmp/{APP}-{os.getuid()}")
    return path


def config_file() -> Path:
    return config_dir() / "config.json"


def filters_file() -> Path:
    return config_dir() / "filters.txt"


def state_file() -> Path:
    return state_dir() / "state.json"


def daemon_socket() -> Path:
    return runtime_dir() / "daemon.sock"


def daemon_lock() -> Path:
    return runtime_dir() / "daemon.lock"


def rclone_socket() -> Path:
    return runtime_dir() / "rclone.sock"


def rclone_log() -> Path:
    return state_dir() / "rclone.log"


def daemon_log() -> Path:
    return state_dir() / "daemon.log"


def bisync_dir(folder_id: str) -> Path:
    return state_dir() / "bisync" / folder_id


def link_dir() -> Path:
    """Short symlinks to local folders: bisync names its state files after both paths, and a
    long local path would push those names past the 255-byte filename limit."""
    return state_dir() / "l"


def backup_root() -> Path:
    return state_dir() / "backups"


def backup_dir(folder_id: str) -> Path:
    return backup_root() / folder_id


def systemd_unit_file() -> Path:
    return (
        _xdg("XDG_CONFIG_HOME", Path.home() / ".config")
        / "systemd"
        / "user"
        / f"{APP}.service"
    )


def ensure_private_dir(path: Path) -> Path:
    """Create a directory only this user can read, tightening it if it already exists."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def expand(path: str) -> Path:
    """Expand ~ and make absolute without touching the filesystem (a dead FUSE mount would hang resolve())."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def is_within(child: Path, parent: Path) -> bool:
    """Whether child is parent or lies inside it (purely lexical)."""
    child_s, parent_s = str(child).rstrip("/"), str(parent).rstrip("/")
    return child_s == parent_s or child_s.startswith(parent_s + "/")


def overlaps(a: Path, b: Path) -> bool:
    return is_within(a, b) or is_within(b, a)
