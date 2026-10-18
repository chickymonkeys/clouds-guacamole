#!/usr/bin/env python3
"""Local IPC security regressions; only throwaway paths and fake credentials.

python3 tests/test_ipc.py
"""

import json
import os
import queue
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

from guac import client, paths
from guac.daemon import main as daemon_main


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gi-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.root / "run"))
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)

    def server(self) -> "Server":
        paths.ensure_runtime_dir()
        server = Server(paths.daemon_socket())
        self.addCleanup(server.close)
        return server


class RuntimeSecurity(Sandbox):
    def test_missing_and_long_xdg_select_fallback(self) -> None:
        fallback = Path(f"/tmp/guacamole-{os.getuid()}")
        for value in ("", "/" + "x" * 100, "/" + "é" * 45):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": value}),
            ):
                self.assertEqual(paths.runtime_dir(), fallback)
        with patch.dict(os.environ):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertEqual(paths.runtime_dir(), fallback)

    def test_creates_private_directory_without_changing_umask(self) -> None:
        with patch("guac.paths.os.umask") as umask:
            path = paths.ensure_runtime_dir()
        umask.assert_not_called()
        self.assertEqual(path, self.root / "run" / "guacamole")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(paths.ensure_runtime_dir(), path)

    def test_rejects_insecure_directory_without_chmod_or_unlink(self) -> None:
        path = paths.ensure_runtime_dir()
        for name in ("daemon.sock", "daemon.lock"):
            (path / name).write_text("leave me alone")
        for mode in (0o755, 0o770, 0o777):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with self.assertRaises(PermissionError):
                    daemon_main()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                for name in ("daemon.sock", "daemon.lock"):
                    self.assertEqual((path / name).read_text(), "leave me alone")

    def test_rejects_foreign_owned_directory_even_when_private(self) -> None:
        path = paths.ensure_runtime_dir()
        original = os.fstat
        inode = path.stat().st_ino

        def foreign_owner(fd):
            info = original(fd)
            if info.st_ino != inode:
                return info
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)

        with (
            patch("guac.paths.os.fstat", side_effect=foreign_owner),
            self.assertRaises(PermissionError),
        ):
            paths.ensure_runtime_dir()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_rejects_symlink_leaf_and_ancestor(self) -> None:
        target = self.root / "target"
        target.mkdir(mode=0o700)
        base = self.root / "run"
        base.mkdir(mode=0o700)
        (base / "guacamole").symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            paths.ensure_runtime_dir()
        (base / "guacamole").unlink()
        base.rmdir()
        base.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            paths.ensure_runtime_dir()
        self.assertFalse((target / "guacamole").exists())

    def test_rejects_writable_ancestor_and_non_directory(self) -> None:
        base = self.root / "run"
        base.mkdir(mode=0o700)
        base.chmod(0o777)
        with self.assertRaises(PermissionError):
            paths.ensure_runtime_dir()
        self.assertFalse((base / "guacamole").exists())
        base.chmod(0o700)
        (base / "guacamole").write_text("not a directory")
        with self.assertRaises(OSError):
            paths.ensure_runtime_dir()

    def test_rejects_relative_and_parent_paths(self) -> None:
        for value in ("relative", str(self.root / ".." / "elsewhere")):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": value}),
                self.assertRaises(PermissionError),
            ):
                paths.ensure_runtime_dir()


class Server:
    """A real socket server that records every byte clients send; never runs guacd."""

    def __init__(self, path: Path):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.received = bytearray()
        self.connections = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with conn:
                conn.settimeout(0.1)
                try:
                    state = {
                        "type": "state",
                        "data": {
                            "engine": {"state": "running"},
                            "version": f"ipc-test-{self.connections}",
                        },
                    }
                    conn.sendall(json.dumps(state).encode() + b"\n")
                    pending = b""
                    drop = False
                    while not self.stopping.is_set():
                        try:
                            data = conn.recv(65536)
                        except TimeoutError:
                            continue
                        if not data:
                            break
                        self.received.extend(data)
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            request = json.loads(line)
                            if request["cmd"] == "drop":
                                drop = True
                                break
                            reply = {
                                "type": "reply",
                                "id": request["id"],
                                "ok": True,
                                "data": {"echo": request["args"]},
                            }
                            conn.sendall(json.dumps(reply).encode() + b"\n")
                        if drop:
                            break
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.stopping.set()
        self.listener.close()
        self.thread.join(timeout=3)


class Connections(Sandbox):
    def test_same_uid_server_can_exchange_state_and_requests(self) -> None:
        server = self.server()
        conn = client.connect(autostart=False)
        self.addCleanup(conn.close)
        self.assertEqual(conn.first_state()["version"], "ipc-test-1")
        args = {"client_secret": "fake-test-secret"}
        self.assertEqual(conn.request("set_client_id", args)["data"]["echo"], args)
        self.assertIn(b"fake-test-secret", server.received)

    def test_foreign_peer_rejected_before_reading_state_or_sending_credentials(self):
        server = self.server()
        real_socket = socket.socket

        def foreign_socket(*args, **kwargs):
            sock = real_socket(*args, **kwargs)
            proxy = Mock(wraps=sock)
            proxy.getsockopt.return_value = struct.pack("3i", 42, os.getuid() + 1, 42)
            return proxy

        with (
            patch("guac.client.socket.socket", side_effect=foreign_socket),
            patch("guac.client.start") as start,
        ):
            with self.assertRaises(client.UnsafeDaemonError):
                conn = client.connect()
                conn.request("add_credentials", {"password": "must-not-leak"})
            start.assert_not_called()
        self.assertEqual(server.received, b"")

    def test_unavailable_peer_credentials_fail_closed(self):
        self.server()
        real_socket = socket.socket
        created = []

        def unverifiable_socket(*args, **kwargs):
            sock = real_socket(*args, **kwargs)
            proxy = Mock(wraps=sock)
            proxy.getsockopt.side_effect = OSError("cannot read credentials")
            created.append(proxy)
            return proxy

        with (
            patch("guac.client.socket.socket", side_effect=unverifiable_socket),
            self.assertRaises(client.UnsafeDaemonError),
        ):
            client.Connection()
        created[0].makefile.assert_not_called()
        created[0].close.assert_called_once()

    def test_unsafe_path_is_not_connected_or_autostarted(self):
        path = paths.ensure_runtime_dir()
        path.chmod(0o777)
        with (
            patch("guac.client.socket.socket") as socket_factory,
            patch("guac.client.start") as start,
        ):
            with self.assertRaises(client.UnsafeDaemonError):
                client.connect()
            with self.assertRaises(client.UnsafeDaemonError):
                client.is_running()
            socket_factory.return_value.connect.assert_not_called()
            start.assert_not_called()

    def test_bridge_round_trip_and_stdin_eof(self):
        server = self.server()
        request = {"id": 7, "cmd": "add_credentials", "args": {"password": "fake"}}
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "bin/guac"), "bridge"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        assert stdin is not None and stdout is not None and stderr is not None
        lines = queue.Queue()

        def collect():
            for line in stdout:
                lines.put(json.loads(line))

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        self.assertEqual(lines.get(timeout=5), {"type": "connected"})
        self.assertEqual(lines.get(timeout=5)["data"]["version"], "ipc-test-1")
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        reply = lines.get(timeout=5)
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["data"]["echo"], request["args"])
        stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0)
        reader.join(timeout=3)
        self.assertEqual(stderr.read(), "")
        stdout.close()
        stderr.close()
        self.assertIn(b'"password": "fake"', server.received)

    def test_cli_and_bridge_refuse_unsafe_socket(self):
        server = self.server()
        paths.runtime_dir().chmod(0o777)
        for args, secret in (
            (
                ["bridge"],
                '{"id":1,"cmd":"set_client_id","args":{"client_secret":"fake"}}',
            ),
            (["add-credentials", "Test", "nextcloud"], '{"pass":"fake"}'),
            (["set-client-id", "Test", "--client-id", "fake-id"], "fake-secret"),
            (["stop"], ""),
            (["watch"], ""),
            (["daemon", "--ensure"], ""),
            (["daemon"], ""),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(REPO / "bin/guac"), *args],
                    input=secret + "\n",
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("runtime directory", result.stderr)
                self.assertNotIn("fake-secret", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.received, b"")


@unittest.skipUnless(shutil.which("qs"), "widget coverage needs Quickshell")
class Widget(Sandbox):
    def widget(self, unsafe: bool) -> str:
        shell = self.root / "shell"
        shell.mkdir()
        (shell / "gui").symlink_to(REPO / "ui")
        shutil.copy(REPO / "tests/ipc_widget.qml", shell / "shell.qml")
        env = dict(self.env)
        # The real Wayland socket must be visible even with Qt's offscreen platform.
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
        if not os.path.isabs(display):
            display = str(Path(f"/run/user/{os.getuid()}") / display)
        env.update(
            WAYLAND_DISPLAY=display,
            QT_QPA_PLATFORM="offscreen",
            GUAC_TEST_REPO=str(REPO),
            GUAC_TEST_UNSAFE="1" if unsafe else "0",
        )
        result = subprocess.run(
            ["qs", "-p", str(shell / "shell.qml")],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("IPC_WIDGET_PASS", output, output)
        self.assertNotIn("IPC_WIDGET_FAIL", output, output)
        return output

    def test_widget_requests_disconnect_and_reconnect(self):
        server = self.server()
        self.widget(unsafe=False)
        self.assertEqual(server.connections, 2)
        self.assertEqual(server.received.count(b"widget-fake-secret"), 2)

    def test_widget_never_connects_to_unsafe_directory(self):
        server = self.server()
        paths.runtime_dir().chmod(0o777)
        self.widget(unsafe=True)
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.received, b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
