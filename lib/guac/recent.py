"""Recently used cloud files, found without touching any mount.

Two local sources, both cheap to read:
  * rclone's VFS cache metadata: every file opened or written through a mount has a small
    record under vfsMeta whose modification time is updated when the file is used. So the
    newest records are the files you recently opened, served from the cache instantly.
  * folders kept local: plain local directories, scanned with a strict entry budget.

The previous design crawled the FUSE mounts every refresh, which turned each refresh into
hundreds of remote API calls and made the file manager hang while it ran.
"""

import heapq
import os
from collections.abc import Iterable

MAX_SCAN_ENTRIES = 20000


def _walk_newest(
    root: str, limit: int, budget: int, skip_hidden: bool
) -> list[tuple[float, str, int]]:
    """(mtime, path, size) of the newest files under root, examining at most `budget` entries."""
    heap: list[tuple[float, str, int]] = []
    stack = [root]
    while stack and budget > 0:
        directory = stack.pop()
        try:
            it = os.scandir(directory)
        except OSError:
            continue
        with it:
            for entry in it:
                budget -= 1
                if budget <= 0:
                    break
                if skip_hidden and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item = (st.st_mtime, entry.path, st.st_size)
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                else:
                    heapq.heappushpop(heap, item)
    return heap


def from_vfs_cache(
    meta_root: str, data_root: str, mount_point: str, remote: str, limit: int
) -> list[dict]:
    out = []
    if not meta_root or not os.path.isdir(meta_root):
        return out
    for mtime, meta_path, _ in _walk_newest(
        meta_root, limit, MAX_SCAN_ENTRIES, skip_hidden=False
    ):
        rel = os.path.relpath(meta_path, meta_root)
        name = os.path.basename(rel)
        if name.startswith("."):
            continue
        size = 0
        try:
            size = os.stat(os.path.join(data_root, rel)).st_size
        except OSError:
            pass
        folder = os.path.dirname(rel)
        out.append(
            {
                "name": name,
                "path": os.path.join(mount_point, rel),
                "folder": folder or "/",
                "remote": remote,
                "where": "stream",
                "modifiedTs": int(mtime),
                "sizeBytes": size,
            }
        )
    return out


def from_local_folder(
    local_root: str, remote: str, remote_path: str, limit: int
) -> list[dict]:
    out = []
    if not os.path.isdir(local_root):
        return out
    for mtime, path, size in _walk_newest(
        local_root, limit, MAX_SCAN_ENTRIES, skip_hidden=True
    ):
        rel = os.path.relpath(path, local_root)
        folder = os.path.dirname(rel)
        shown = "/".join(p for p in (remote_path, folder) if p)
        out.append(
            {
                "name": os.path.basename(rel),
                "path": path,
                "folder": shown or "/",
                "remote": remote,
                "where": "local",
                "modifiedTs": int(mtime),
                "sizeBytes": size,
            }
        )
    return out


def merge(groups: Iterable[list[dict]], limit: int) -> list[dict]:
    seen = set()
    merged = []
    for item in sorted(
        (i for g in groups for i in g), key=lambda i: i["modifiedTs"], reverse=True
    ):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged
