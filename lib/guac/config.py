"""User configuration (~/.config/guacamole/config.json) and runtime state (~/.local/state/guacamole/state.json)."""

import copy
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger("guac.config")

DEFAULTS: dict[str, Any] = {
    "version": 1,
    # Where cloud drives are streamed (mounted)
    "mount_root": "~/Cloud/Stream",
    # Default parent for folders kept on this device (two-way sync)
    "local_root": "~/Cloud/Sync",
    # rclone VFS cache: files opened through a mount stay here, so reopening them is instant.
    # Eviction is least-recently-used once max size is reached, so a long max age is cheap.
    "cache_max_size_gb": 20,
    "cache_max_age": "720h",
    "cache_min_free_gb": 5,
    # Folders kept local are also synced at this interval (local edits sync within seconds)
    "sync_interval_min": 5,
    # Desktop notifications for conflicts and problems that need attention
    "notifications": True,
    # Days to keep local files that a sync overwrote or deleted
    "backup_days": 30,
    "remotes": {},
}

# Settings the UI and CLI may change, with their types
SETTABLE = {
    "mount_root": str,
    "local_root": str,
    "cache_max_size_gb": int,
    "cache_max_age": str,
    "cache_min_free_gb": int,
    "sync_interval_min": int,
    "notifications": bool,
    "backup_days": int,
}

DEFAULT_FILTERS = """\
# Files and folders that are never synced between the cloud and folders kept local.
# rclone filter syntax: https://rclone.org/filtering/
# Changing this file makes the next sync of every folder a full (safe, non-deleting) resync.
- .~lock.*#
- ~$*
- .~*
- *.tmp
- *.temp
- *.swp
- *.swx
- *~
- *.part
- *.partial
- *.crdownload
- .DS_Store
- ._*
- Thumbs.db
- desktop.ini
- .directory
- .Trash-*/**
- .goutputstream-*
# Obsidian keeps per-device window layout here; syncing it only produces conflicts
- .obsidian/workspace*.json
"""


def new_id() -> str:
    return secrets.token_hex(4)


def _open_private(path: Path, mode: str = "w"):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, mode, encoding="utf-8")


def write_json_atomic(path: Path, data: Any) -> None:
    paths.ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with _open_private(tmp) as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Any | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


def normalize_remote(entry: Any) -> dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    folders = []
    for f in entry.get("folders") or []:
        if not isinstance(f, dict) or not isinstance(f.get("local"), str):
            continue
        folders.append(
            {
                "id": str(f.get("id") or new_id()),
                "path": str(f.get("path") or "").strip("/"),
                "local": f["local"],
                "paused": bool(f.get("paused", False)),
            }
        )
    label = entry.get("label")
    mount_path = entry.get("mount_path")
    hidden = entry.get("hidden_hints")
    return {
        "label": label if isinstance(label, str) and label.strip() else None,
        "stream": bool(entry.get("stream", False)),
        "mount_path": mount_path
        if isinstance(mount_path, str) and mount_path.strip()
        else None,
        "folders": folders,
        # Dismissed suggestions on the drive card (warning codes)
        "hidden_hints": sorted({str(h) for h in hidden})
        if isinstance(hidden, list)
        else [],
    }


def load() -> dict[str, Any]:
    raw = _read_json(paths.config_file())
    fresh = not isinstance(raw, dict)
    if fresh:
        raw = {}
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key == "remotes" and isinstance(value, dict):
            cfg["remotes"] = {str(k): normalize_remote(v) for k, v in value.items()}
        elif key in SETTABLE:
            try:
                cfg[key] = coerce(key, value)
            except ValueError:
                pass
    if fresh:
        save(cfg)
    return cfg


def save(cfg: dict[str, Any]) -> None:
    write_json_atomic(paths.config_file(), cfg)


def coerce(key: str, value: Any) -> Any:
    kind = SETTABLE[key]
    if kind is bool:
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("1", "true", "yes", "on"):
            return True
        if str(value).lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} must be true or false")
    if kind is int:
        n = int(str(value).strip())
        if n < 0:
            raise ValueError(f"{key} cannot be negative")
        if key == "sync_interval_min" and n < 1:
            raise ValueError("sync_interval_min must be at least 1")
        return n
    text = str(value).strip()
    if not text:
        raise ValueError(f"{key} cannot be empty")
    return text


def ensure_filters() -> Path:
    path = paths.filters_file()
    if not path.exists():
        paths.ensure_private_dir(path.parent)
        with _open_private(path) as f:
            f.write(DEFAULT_FILTERS)
    return path


def load_state() -> dict[str, Any]:
    raw = _read_json(paths.state_file())
    state = raw if isinstance(raw, dict) else {}
    state.setdefault("folders", {})
    state.setdefault("quota", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    try:
        write_json_atomic(paths.state_file(), state)
    except OSError as e:
        log.warning("could not save state: %s", e)
