#!/usr/bin/env python3
"""Security boundaries: hostile display text and private atomic writes, no cloud access."""

import copy
import io
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from guac import cli, client, config, paths, util
from guac.daemon import Daemon, _SafeLogFormatter

# Both OSC forms, CSI, DCS, C0/C1, and visual prompt/line spoofing. All fake data.
PAYLOADS = (
    "shared\x1b]52;c;ZmFrZQ==\x07",
    "shared\x1b]52;c;ZmFrZQ==\x1b\\",
    "shared\x9d52;c;ZmFrZQ==\x9c",
    "shared\x1b[2J\x1b[H",
    "shared\x1bPfake\x1b\\",
    "shared\x00\x08\r\x7f",
    "shared\nforged status\trow",
    "shared\u202ereversed\u202c\u2066hidden\u2069",
)
CONTROLS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
SGR = re.compile(r"\x1b\[(?:0|1|2|31|32|33)m")


class DisplaySecurity(unittest.TestCase):
    def assert_safe(self, text: str, *, color: bool = False) -> None:
        if color:
            text = SGR.sub("", text)
        self.assertIsNone(CONTROLS.search(text.replace("\n", "")), repr(text))

    def test_all_terminal_and_bidi_controls_are_visible(self) -> None:
        controls = "".join(map(chr, (*range(32), *range(127, 160))))
        bidi = (
            "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        )
        escaped = util.neutralize_controls(controls + bidi)
        self.assertIsNone(CONTROLS.search(escaped))
        self.assertIn("␛", escaped)
        self.assertIn("\\x9d", escaped)
        self.assertIn("\\u202e", escaped)
        self.assertEqual(
            util.neutralize_controls("Résumé/日本語 👩‍💻"), "Résumé/日本語 👩‍💻"
        )
        self.assertEqual(util.neutralize_controls("a\t\nb\r", multiline=True), "a␉\nb␍")

    def test_browse_escapes_paths_on_terminal_and_redirected_output(self) -> None:
        for payload in PAYLOADS:
            for tty in (False, True):
                with self.subTest(payload=payload, tty=tty):
                    out = io.StringIO()
                    data = {
                        "dirs": [
                            {"path": payload, "kept": True},
                            {"path": "普通 folder"},
                        ]
                    }
                    original = copy.deepcopy(data)
                    with (
                        patch.object(cli, "_run", return_value=data) as run,
                        patch.object(out, "isatty", return_value=tty),
                        redirect_stdout(out),
                    ):
                        cli.main(["browse", f"Cloud:{payload}"])
                    self.assertEqual(
                        out.getvalue(),
                        util.neutralize_controls(payload)
                        + "  [kept local]\n普通 folder\n",
                    )
                    self.assert_safe(out.getvalue())
                    self.assertEqual(data, original)
                    run.assert_called_once_with(
                        "browse", {"remote": "Cloud", "path": payload}, quiet=True
                    )

    def test_status_covers_paths_labels_errors_conflicts_and_unknown_states(
        self,
    ) -> None:
        for tty in (False, True):
            for payload in PAYLOADS:
                with self.subTest(tty=tty, payload=payload):
                    state = {
                        "version": payload,
                        "engine": {
                            "version": payload,
                            "state": payload,
                            "error": payload,
                        },
                        "drives": [
                            {
                                "label": payload,
                                "provider": payload,
                                "mountState": payload,
                                "mountPath": payload,
                                "error": payload,
                                "warnings": [{"text": payload}],
                                "folders": [
                                    {
                                        "id": payload,
                                        "label": payload,
                                        "path": payload,
                                        "local": payload,
                                        "state": payload,
                                        "attention": payload,
                                        "conflictCount": 1,
                                        "conflicts": [payload],
                                    }
                                ],
                            }
                        ],
                    }
                    original = copy.deepcopy(state)
                    out = io.StringIO()
                    with (
                        patch.object(out, "isatty", return_value=tty),
                        redirect_stdout(out),
                    ):
                        cli.print_status(state)
                    self.assert_safe(out.getvalue(), color=tty)
                    self.assertEqual(state, original)
                    self.assertEqual("\x1b[1m" in out.getvalue(), tty)

    def test_other_human_readable_commands_escape_data(self) -> None:
        for payload in PAYLOADS:
            for argv, reply, state in (
                (["log"], {"lines": [payload]}, {}),
                (["config", "mount_root"], {}, {"settings": {"mount_root": payload}}),
                (["exclude"], {}, {"filters": {"excludes": [payload]}}),
                (["open"], {}, {"cloudRoot": payload}),
                (["size", "Cloud:"], {"count": payload, "bytes": 1}, {}),
            ):
                with self.subTest(argv=argv, payload=payload):
                    out = io.StringIO()
                    with (
                        patch.object(cli, "_run", return_value=reply),
                        patch.object(cli, "_state", return_value=state),
                        patch.object(cli.shutil, "which", return_value=None),
                        redirect_stdout(out),
                    ):
                        cli.main(argv)
                    self.assert_safe(out.getvalue())
                    self.assertIn(util.neutralize_controls(payload), out.getvalue())

    def test_reply_messages_and_errors_are_escaped_without_mutating_data(self) -> None:
        for payload in PAYLOADS:
            conn = Mock()
            conn.request.return_value = {
                "ok": True,
                "message": payload,
                "data": {"path": payload},
            }
            out = io.StringIO()
            with (
                patch.object(client, "connect", return_value=conn),
                redirect_stdout(out),
            ):
                self.assertEqual(cli._run("test", {"path": payload}), {"path": payload})
            self.assert_safe(out.getvalue())
            conn.request.assert_called_once_with(
                "test", {"path": payload}, timeout=600.0
            )
            conn.close.assert_called_once()
            conn.request.return_value = {"ok": False, "error": payload}
            with (
                patch.object(client, "connect", return_value=conn),
                self.assertRaises(SystemExit) as error,
            ):
                cli._run("test")
            self.assert_safe(str(error.exception))
            with (
                patch.object(
                    client, "connect", side_effect=client.DaemonError(payload)
                ),
                self.assertRaises(SystemExit) as error,
            ):
                cli._state()
            self.assert_safe(str(error.exception))

    def test_confirmation_and_parser_errors_escape_arguments(self) -> None:
        for payload in PAYLOADS:
            with (
                patch("builtins.input", return_value="y") as prompt,
                patch.object(cli, "_run") as run,
            ):
                cli.main(["remove", payload])
            self.assert_safe(prompt.call_args.args[0])
            run.assert_called_once_with("remove_remote", {"remote": payload})
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit):
                cli.main(["stream", "Cloud", payload])
            self.assert_safe(err.getvalue())

    def test_json_modes_preserve_original_data(self) -> None:
        state = {"path": "".join(PAYLOADS)}
        for argv in (["status", "--json"], ["config"], ["watch"]):
            out = io.StringIO()
            conn = Mock(state=state)
            conn.read_message.side_effect = KeyboardInterrupt
            with (
                patch.object(
                    cli,
                    "_state",
                    return_value=state if argv[0] != "config" else {"settings": state},
                ),
                patch.object(client, "connect", return_value=conn),
                redirect_stdout(out),
            ):
                cli.main(argv)
            self.assertEqual(json.loads(out.getvalue()), state)
            self.assert_safe(out.getvalue())

    def test_daemon_logs_escape_controls_in_messages_and_exceptions(self) -> None:
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        handler.setFormatter(_SafeLogFormatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("guac-security-test")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        for payload in PAYLOADS:
            out.seek(0)
            out.truncate()
            try:
                raise ValueError(payload)
            except ValueError:
                logger.exception("cloud path %s", payload)
            self.assert_safe(out.getvalue())
            self.assertIn("Traceback", out.getvalue())
            self.assertIn("ValueError:", out.getvalue())


class AtomicWriteSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="guac-write-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "private" / "settings"

    def test_predictable_temp_links_cannot_clobber_other_files(self) -> None:
        self.target.parent.mkdir()
        victim = self.root / "unrelated"
        victim.write_text("preserve this")
        victim.chmod(0o644)
        planted = self.target.with_name(self.target.name + ".tmp")
        planted.symlink_to(victim)
        for write in (config.write_json_atomic, config.write_text_atomic):
            with self.subTest(write=write.__name__):
                write(self.target, "test content")
                self.assertEqual(victim.read_text(), "preserve this")
                self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
                self.assertTrue(planted.is_symlink())
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_failed_write_preserves_previous_file_and_cleans_temporary_data(
        self,
    ) -> None:
        config.write_json_atomic(self.target, {"old": "keep"})
        original = self.target.read_bytes()
        for failure in ("dump", "fsync", "replace"):
            with self.subTest(failure=failure):
                name = (
                    "guac.config.json.dump"
                    if failure == "dump"
                    else f"guac.config.os.{failure}"
                )
                with (
                    patch(name, side_effect=OSError("injected failure")),
                    self.assertRaises(OSError),
                ):
                    config.write_json_atomic(self.target, {"new": "sensitive data"})
                self.assertEqual(self.target.read_bytes(), original)
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_filters_symlink_still_updates_its_target_atomically(self) -> None:
        self.target.parent.mkdir()
        link = self.root / "filters.txt"
        link.symlink_to(self.target)
        config.write_text_atomic(link, "- *.tmp\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.target.read_text(), "- *.tmp\n")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.parent.stat().st_mode & 0o777, 0o700)

    def test_default_filters_do_not_create_a_dangling_symlink_target(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            link = paths.filters_file()
            link.parent.mkdir()
            link.symlink_to(self.root / "missing-target")
            config.ensure_filters()
            self.assertTrue(link.is_symlink())
            self.assertFalse((self.root / "missing-target").exists())


class BrowseContractSecurity(unittest.IsolatedAsyncioTestCase):
    async def test_browse_preserves_raw_paths_for_remote_and_mounted_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="guac-browse-") as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(root / "cfg"),
                    "XDG_STATE_HOME": str(root / "state"),
                },
            ):
                daemon = Daemon()
                self.addCleanup(daemon.rc_pool.shutdown, wait=True)
                self.addCleanup(daemon.fs_pool.shutdown, wait=True)
                daemon._need_engine = Mock()
                drive = SimpleNamespace(
                    name="Cloud", mount_state="off", mount_point=str(root / "mount")
                )
                daemon._drive = Mock(return_value=drive)
                daemon.rc = AsyncMock(
                    return_value={
                        "list": [{"Name": name, "IsDir": True} for name in PAYLOADS]
                    }
                )
                expected = {name: util.neutralize_controls(name) for name in PAYLOADS}
                remote = await daemon.cmd_browse({"remote": "Cloud", "path": ""})
                self.assertEqual(
                    {e["path"]: e["name"] for e in remote["dirs"]}, expected
                )
                # NUL is invalid on a filesystem; all other tested controls are legal names.
                mounted_names = [name for name in PAYLOADS if "\x00" not in name]
                for name in mounted_names:
                    (Path(drive.mount_point) / name).mkdir(parents=True)
                drive.mount_state = "mounted"
                mounted = await daemon.cmd_browse({"remote": "Cloud", "path": ""})
                self.assertEqual(
                    {e["path"]: e["name"] for e in mounted["dirs"]},
                    {name: expected[name] for name in mounted_names},
                )
                self.assertEqual(daemon.rc.await_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
