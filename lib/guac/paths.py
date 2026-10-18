"""Where Guacamole keeps its files. Everything follows the XDG base directory spec."""

import os
import stat
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
    if len(os.fsencode(path)) + len(b"/sync-00000000.sock") > _MAX_SOCKET_PATH:
        path = Path(f"/tmp/{APP}-{os.getuid()}")
    return path


def ensure_runtime_dir() -> Path:
    """Create/validate the socket directory without following symlinks.

    Every ancestor must prevent other users from replacing its children. Root's
    sticky /tmp is safe only because we also check ownership of each child. Never
    chmod an existing directory: it may belong to somebody else, or contain files
    planted while it was writable by others.
    """
    path = runtime_dir()
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError(f"Unsafe Guacamole runtime directory: {path}")
    uid = os.getuid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for i, part in enumerate(path.parts[1:], 1):
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if i == len(path.parts) - 1:
                safe = info.st_uid == uid and mode == 0o700
            else:
                safe = info.st_uid in (0, uid) and (
                    not mode & 0o022 or (info.st_uid == 0 and mode & stat.S_ISVTX)
                )
            if not safe:
                raise PermissionError(f"Unsafe Guacamole runtime directory: {path}")
    finally:
        os.close(fd)
    return path


def config_file() -> Path:
    return config_dir() / "config.json"


def filters_file() -> Path:
    return config_dir() / "filters.txt"


def folder_filters_file(folder_id: str) -> Path:
    """A folder's own never-synced patterns, written from config.json for bisync."""
    return state_dir() / "filters" / f"{folder_id}.txt"


def state_file() -> Path:
    return state_dir() / "state.json"


def daemon_socket() -> Path:
    return runtime_dir() / "daemon.sock"


def daemon_lock() -> Path:
    return runtime_dir() / "daemon.lock"


def rclone_socket() -> Path:
    return runtime_dir() / "rclone.sock"


def sync_socket(folder_id: str) -> Path:
    """rc socket of the rclone process running a folder's sync."""
    return runtime_dir() / f"sync-{folder_id}.sock"


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
