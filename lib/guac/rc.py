"""Client for rclone's remote-control API over a private unix socket.

All credentials and tokens travel in request bodies on a socket inside the user's
0700 runtime directory, never on command lines (which every local user can read).
"""

import http.client
import json
import socket
from typing import Any

# Bisync returns its whole log on completion; bound what we are willing to hold
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class RcError(Exception):
    """rclone answered with an error. The message never contains request parameters."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class RcUnavailable(RcError):
    """The rclone daemon could not be reached at all."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float | None):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class RcClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        conn = _UnixHTTPConnection(self.socket_path, timeout)
        try:
            conn.request(
                "POST",
                "/" + method,
                body=json.dumps(params or {}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
        except TimeoutError as e:
            raise RcError(f"rclone {method} timed out") from e
        except (OSError, http.client.HTTPException) as e:
            raise RcUnavailable(f"rclone is not reachable: {e}") from e
        finally:
            conn.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RcError(f"rclone {method} returned too much data")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {"result": data}
        if status != 200:
            # rclone echoes the request under "input"; only the error text is safe to surface
            raise RcError(
                str(data.get("error") or f"rclone {method} failed (HTTP {status})"),
                status,
            )
        return data

    def ping(self, timeout: float = 2.0) -> bool:
        try:
            self.call("rc/noop", {}, timeout=timeout)
            return True
        except RcError:
            return False
