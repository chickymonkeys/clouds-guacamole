#!/usr/bin/env python3
"""Local installer lifecycle checks with a throwaway home and stubbed shell commands."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_ID = "chickymonkeys.guacamole"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gi-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.plugin = self.home / ".config/omarchy/plugins" / PLUGIN_ID
        self.shortcut = self.home / ".local/bin/guac"
        stubs = self.root / "stubs"
        stubs.mkdir()
        for name in ("omarchy", "omarchy-shell", "omarchy-restart-shell"):
            stub = stubs / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        runtime = self.root / "run"
        runtime.mkdir(mode=0o700)
        self.env = dict(os.environ)
        self.env.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_STATE_HOME=str(self.home / ".local/state"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_RUNTIME_DIR=str(runtime),
            RCLONE_CONFIG=str(self.home / ".config/rclone/rclone.conf"),
            PATH=f"{stubs}:{os.environ['PATH']}",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.sentinels = [
            self.home / ".config/guacamole/config.json",
            self.home / ".config/rclone/rclone.conf",
            self.home / "Cloud/Sync/notes.md",
            self.home / ".local/state/guacamole/backups/keep.md",
            self.home / ".cache/rclone/keep",
        ]
        for path in self.sentinels:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserve me\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def install(self, *args: str, success: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["bash", str(REPO / "install.sh"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def assert_data_preserved(self) -> None:
        for path in self.sentinels:
            self.assertEqual(path.read_text(), "preserve me\n", str(path))

    def test_copy_update_and_remove_shortcut(self) -> None:
        self.install("--no-enable")
        self.assertTrue((self.plugin / ".guacamole-install").is_file())
        self.assertEqual(self.shortcut.resolve(), self.plugin / "bin/guac")
        self.install("--no-enable")
        self.install("--uninstall")
        self.assertFalse(self.plugin.exists())
        self.assertFalse(self.shortcut.is_symlink(), "no dangling CLI shortcut")
        self.assert_data_preserved()

    def test_link_and_remove_preserve_checkout(self) -> None:
        self.install("--link", "--no-enable")
        self.assertTrue(self.plugin.is_symlink())
        self.assertEqual(self.plugin.resolve(), REPO)
        self.install("--uninstall")
        self.assertFalse(self.plugin.is_symlink())
        self.assertFalse(self.shortcut.is_symlink())
        self.assertTrue((REPO / "manifest.json").is_file())
        self.assert_data_preserved()

    def test_unmanaged_plugin_is_not_replaced_or_removed(self) -> None:
        self.plugin.mkdir(parents=True)
        foreign = self.plugin / "foreign.txt"
        foreign.write_text("not ours\n")
        self.install("--no-enable", success=False)
        self.install("--uninstall")
        self.assertEqual(foreign.read_text(), "not ours\n")

    def test_unrelated_shortcuts_are_preserved(self) -> None:
        self.shortcut.parent.mkdir(parents=True)
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                target = self.root / ("missing" if dangling else "other") / "bin/guac"
                if not dangling:
                    target.parent.mkdir(parents=True)
                    target.write_text("not ours\n")
                self.shortcut.symlink_to(target)
                self.install("--no-enable")
                self.assertEqual(self.shortcut.readlink(), target)
                self.install("--uninstall")
                self.assertEqual(self.shortcut.readlink(), target)
                self.shortcut.unlink()

    def test_existing_cli_file_is_preserved(self) -> None:
        self.shortcut.parent.mkdir(parents=True)
        self.shortcut.write_text("not ours\n")
        self.install("--no-enable")
        self.install("--uninstall")
        self.assertEqual(self.shortcut.read_text(), "not ours\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
