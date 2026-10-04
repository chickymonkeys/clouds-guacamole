"""Small helpers shared by the daemon and the CLI."""

import ctypes
import os
import re
import signal
from collections.abc import Iterable

_PR_SET_PDEATHSIG = 1
# Loaded up front: the forked child should only make the prctl call, not load libraries
_LIBC = ctypes.CDLL(None, use_errno=True)


def die_with_parent():
    """preexec_fn: the kernel SIGTERMs the child when the spawning thread exits.

    PDEATHSIG follows the *thread* that forked, so only spawn with it from the main thread.
    """
    parent = os.getpid()

    def preexec() -> None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() != parent:
            os._exit(1)

    return preexec


# C0 controls (but tab/newline), DEL and C1 controls: what a terminal would act on
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def neutralize_controls(text: str) -> str:
    """Render control characters visibly; cloud file names can carry raw escape sequences."""

    def picture(m: "re.Match") -> str:
        code = ord(m.group(0))
        if code < 0x20:
            return chr(0x2400 + code)
        return "␡" if code == 0x7F else f"\\x{code:02x}"

    return _CONTROL_CHARS_RE.sub(picture, text or "")


_SECRET_KEY_RE = re.compile(
    r"pass|secret|token|2fa|key_id|private|credential", re.IGNORECASE
)
_TOKEN_FIELD_RE = re.compile(
    r'("(?:access_token|refresh_token|id_token)"\s*:\s*")[^"]*(")'
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    out = _TOKEN_FIELD_RE.sub(r"\1***\2", text or "")
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, "***")
    return out


_LOG_PREFIX_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (?:ERROR|CRITICAL|NOTICE|INFO|DEBUG)\s*: "
)


def clean_error(text: str, limit: int = 300) -> str:
    """One readable line out of an rclone error: no timestamps, no control characters, bounded.

    Google API errors span many lines (a message, a JSON "Details" dump, then ", reason"), so the
    first prose line is kept and the trailing reason code appended.
    """
    lines = [_LOG_PREFIX_RE.sub("", l).strip() for l in str(text or "").splitlines()]
    lines = [l for l in lines if l]
    prose = [
        l for l in lines if not l.startswith(("[", "]", "{", "}", '"', "Details:", ","))
    ]
    msg = (
        prose[-1]
        if prose and len(prose) == len(lines)
        else (prose[0] if prose else (lines[-1] if lines else ""))
    )
    if lines and lines[-1].startswith(",") and lines[-1] != msg:
        msg += f" ({lines[-1].strip(', ')})"
    msg = neutralize_controls(msg)
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def _unescape_mount_field(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def read_mountinfo() -> dict[str, dict[str, str]]:
    """Mount points of this namespace -> {fstype, source}. Reads /proc, never touches the mounts."""
    mounts: dict[str, dict[str, str]] = {}
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                fields = left.split()
                rfields = right.split()
                if len(fields) < 5 or len(rfields) < 2:
                    continue
                mounts[_unescape_mount_field(fields[4])] = {
                    "fstype": rfields[0],
                    "source": _unescape_mount_field(rfields[1]),
                }
    except OSError:
        pass
    return mounts


def has_default_route() -> bool:
    """Cheap online check: is there an IPv4 or IPv6 default route at all?"""
    try:
        with open("/proc/net/route", "r", encoding="ascii", errors="replace") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if (
                    len(parts) > 3
                    and parts[1] == "00000000"
                    and int(parts[3], 16) & 0x1
                ):
                    return True
    except OSError:
        return True  # can't tell: assume online rather than stalling everything
    try:
        with open("/proc/net/ipv6_route", "r", encoding="ascii", errors="replace") as f:
            for line in f:
                parts = line.split()
                if (
                    len(parts) >= 10
                    and parts[0] == "0" * 32
                    and parts[1] == "00"
                    and parts[9] != "lo"
                ):
                    return True
    except OSError:
        pass
    return False


_NETWORK_RE = re.compile(
    r"no such host|dial tcp|connection refused|network is unreachable|i/o timeout|"
    r"context deadline exceeded|tls handshake timeout|connection reset|temporary failure in name resolution|"
    r"server misbehaving|no route to host|eof$|unexpected eof|timeout awaiting response",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"ratelimitexceeded|rate limit|too many requests|\b429\b|userratelimit|quota exceeded",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"invalid_grant|token expired|unauthorized|\b401\b|invalid_client|couldn't fetch token|"
    r"re-authenticate|reauthorize|authentication failed|access denied|invalid credentials",
    re.IGNORECASE,
)


def classify_error(text: str) -> str:
    """network | rate | auth | other"""
    if _AUTH_RE.search(text or ""):
        return "auth"
    if _RATE_RE.search(text or ""):
        return "rate"
    if _NETWORK_RE.search(text or ""):
        return "network"
    return "other"


def human_bytes(n: float) -> str:
    n = float(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"


def tail_lines(
    path: os.PathLike, max_lines: int = 80, max_bytes: int = 256 * 1024
) -> list[str]:
    """The last lines of a (possibly huge) text file, read from the end only."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read(max_bytes)
    except OSError:
        return []
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1 :] if nl != -1 else b""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return [neutralize_controls(l) for l in lines[-max_lines:]]
