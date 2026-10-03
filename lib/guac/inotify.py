"""Minimal inotify binding over ctypes (no third-party modules)."""

import ctypes
import ctypes.util
import os
import struct
from typing import List, NamedTuple

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000
IN_ISDIR = 0x40000000

IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC

# What a folder kept local is watched for: anything that changes its content or tree
WATCH_MASK = (
    IN_CLOSE_WRITE
    | IN_ATTRIB
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)

_HEADER = struct.Struct("iIII")


class Event(NamedTuple):
    wd: int
    mask: int
    cookie: int
    name: str


class Inotify:
    def __init__(self) -> None:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        self._add = libc.inotify_add_watch
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add.restype = ctypes.c_int
        self._rm = libc.inotify_rm_watch
        self._rm.argtypes = [ctypes.c_int, ctypes.c_int]
        self._rm.restype = ctypes.c_int
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        fd = init(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self.fd = fd

    def add_watch(self, path: str, mask: int = WATCH_MASK) -> int:
        wd = self._add(self.fd, os.fsencode(path), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), path)
        return wd

    def rm_watch(self, wd: int) -> None:
        self._rm(self.fd, wd)

    def read(self) -> List[Event]:
        events: List[Event] = []
        while True:
            try:
                data = os.read(self.fd, 256 * 1024)
            except BlockingIOError:
                break
            if not data:
                break
            pos = 0
            while pos + _HEADER.size <= len(data):
                wd, mask, cookie, length = _HEADER.unpack_from(data, pos)
                pos += _HEADER.size
                raw = data[pos : pos + length].split(b"\0", 1)[0]
                pos += length
                events.append(Event(wd, mask, cookie, os.fsdecode(raw)))
        return events

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
