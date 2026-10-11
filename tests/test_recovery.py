#!/usr/bin/env python3
"""Bisync recovery regressions: injected failures and real local rclone jobs, no FUSE.

python3 tests/test_recovery.py
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from guac import config, filters, paths, providers
from guac.daemon import Daemon, Folder, UserError

REPO = Path(__file__).resolve().parents[1]
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")

BROKEN = (
    "ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings\n"
    "ERROR : Bisync aborted. Must run --resync to recover."
)
CONFLICT = "NOTICE: - Path1 Renaming Path1 copy - Cloud:docs/one.conflict1.md\n"


class RecoverySandbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gr-")
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "c"),
                "XDG_STATE_HOME": str(self.root / "s"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "r"),
                "RCLONE_CONFIG": str(self.root / "rclone.conf"),
            },
        )
        self.env.start()
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        self.local.mkdir()
        self.cloud.mkdir()
        (self.root / "rclone.conf").write_text(
            f"[Cloud]\ntype = alias\nremote = {self.cloud}\n"
        )
        cfg = config.load()
        cfg["notifications"] = False
        cfg["remotes"] = {
            "Cloud": config.normalize_remote(
                {
                    "stream": False,
                    "folders": [{"id": "a", "path": "", "local": str(self.local)}],
                }
            )
        }
        config.save(cfg)
        config.ensure_filters()
        paths.ensure_private_dir(paths.state_dir())
        paths.ensure_private_dir(paths.runtime_dir())
        self.daemons: list[Daemon] = []
        self.d = self.new_daemon()
        self.f = self.d.folders["a"]
        self.f.initialized = True
        self.f.filters_md5 = filters.digest(self.d._global_filters)

    def new_daemon(self) -> Daemon:
        d = Daemon()
        self.daemons.append(d)
        d._watch_folder = Mock()
        d.notify = Mock()
        d._read_global_filters()
        d.remotes = {
            "Cloud": {
                "type": "alias",
                "provider": providers.detect({"type": "alias"}),
                "warnings": [],
                "custom_client": False,
            }
        }
        d._rebuild_folders()
        return d

    async def asyncTearDown(self) -> None:
        for d in self.daemons:
            for fid in list(d.workers):
                await d._stop_worker(fid)
            d.rc_pool.shutdown(wait=True)
            d.fs_pool.shutdown(wait=True)
        self.env.stop()
        self.tmp.cleanup()

    def state(self) -> dict:
        return self.d._folder_state(self.f, time.time())

    def failed_job(self, error: str, output: str = "") -> dict:
        return {"success": False, "error": error, "output": {"output": output}}


class FailureTransitions(RecoverySandbox):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rc_mock = AsyncMock(return_value={"jobid": 1})
        self.d.rc = self.rc_mock
        self.d._start_worker = AsyncMock(return_value=SimpleNamespace(rcc=None))
        self.d._stop_worker = AsyncMock()

    async def start(self) -> dict:
        self.assertTrue(await self.d._start_sync(self.f))
        return self.rc_mock.call_args.args[1]

    async def finish(self, result: dict) -> None:
        await self.d._finish_sync(self.f, result)

    async def test_safety_stop_resync_and_force_are_distinct(self) -> None:
        for message, code in (
            ("Safety abort: too many deletes. Run with --force", "safety"),
            ("Cannot sync to an empty directory", "empty"),
        ):
            for action in ("resync", "force"):
                with self.subTest(code=code, action=action):
                    self.f.resync_reason = ""
                    self.f.initialized = True
                    await self.start()
                    await self.finish(self.failed_job(message, BROKEN))
                    self.assertEqual(self.state()["attentionCode"], code)
                    self.assertFalse(self.d._folder_due(self.f, time.time() + 3600))
                    self.d._persist_all_folders()
                    restored = self.new_daemon().folders["a"]
                    self.assertEqual(restored.attention_code, code)
                    await self.d.cmd_resolve({"id": "a", "action": action})
                    params = await self.start()
                    self.assertEqual(bool(params.get("resync")), action == "resync")
                    self.assertEqual(bool(params.get("force")), action == "force")
                    if action == "resync":
                        self.assertEqual(params["resyncMode"], "newer")
                    await self.finish({"success": True})
                    self.assertEqual(self.state()["state"], "synced")
                    params = await self.start()
                    self.assertNotIn("force", params, "force applies to one job only")
                    await self.finish({"success": True})

    async def test_one_automatic_repair_then_actionable_failure(self) -> None:
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        self.assertEqual(self.state()["state"], "pending")
        self.assertTrue(self.state()["resync"])
        self.assertGreater(self.f.retry_at, time.time())
        self.assertFalse(self.d._folder_due(self.f, time.time()))
        self.assertEqual(self.f.resync_reason, "recover")
        restored = self.new_daemon().folders["a"]
        self.assertEqual(restored.resync_reason, "recover")
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        self.assertEqual(self.state()["attentionCode"], "resync_failed")
        self.assertIn("Resync", self.f.attention)
        self.assertFalse(self.d._folder_due(self.f, time.time() + 3600))
        restored = self.new_daemon().folders["a"]
        self.assertEqual(restored.attention_code, "resync_failed")
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        params = await self.start()
        self.assertTrue(params["resync"])
        self.assertNotIn("force", params)
        await self.finish({"success": True})
        self.assertEqual(self.state()["state"], "synced")

    async def test_repair_success_followed_by_broken_listings_does_not_loop(
        self,
    ) -> None:
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        await self.start()
        await self.finish({"success": True})
        self.assertTrue(self.f.recovery_attempted)
        # A restart must not buy another automatic repair before normal sync succeeds.
        self.d._persist_all_folders()
        restored = self.new_daemon()
        self.assertTrue(restored.folders["a"].recovery_attempted)
        await restored._finish_sync(restored.folders["a"], self.failed_job(BROKEN))
        self.assertEqual(restored.folders["a"].attention_code, "resync_failed")

    async def test_ordinary_success_rearms_automatic_recovery(self) -> None:
        self.f.recovery_attempted = True
        await self.start()
        await self.finish({"success": True})
        self.assertFalse(self.f.recovery_attempted)
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        self.assertEqual(self.f.resync_reason, "recover")
        self.assertFalse(self.f.attention)

    async def test_missing_listings_override_resilient_retryable_footer(self) -> None:
        await self.start()
        await self.finish(
            self.failed_job(
                "bisync aborted",
                "Bisync critical error: cannot find prior Path1 or Path2 listings\n"
                "Bisync aborted. Error is retryable without --resync due to --resilient mode.",
            )
        )
        self.assertEqual(self.f.resync_reason, "recover")
        self.assertFalse(self.f.attention)

    async def test_repair_cause_is_preserved_in_long_output(self) -> None:
        await self.start()
        await self.finish(
            self.failed_job(
                "bisync aborted",
                "Bisync critical error: cannot find prior Path1 or Path2 listings\n"
                + "listing diagnostic\n" * 500
                + "Bisync aborted. Error is retryable without --resync due to --resilient mode.",
            )
        )
        self.assertEqual(self.f.resync_reason, "recover")
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        self.assertEqual(self.f.attention_code, "resync_failed")
        self.assertIn("cannot find prior", self.f.attention)

    async def test_network_error_outside_critical_line_during_repair_is_retried(
        self,
    ) -> None:
        self.f.resync_reason = "recover"
        await self.start()
        await self.finish(
            self.failed_job(
                "dial tcp: connection reset by peer",
                "Bisync critical error: failed to sync directories\nMust run --resync to recover.",
            )
        )
        self.assertFalse(self.f.attention)
        self.assertEqual(self.f.error_kind, "network")
        self.assertGreater(self.f.retry_at, time.time())

    async def test_permanent_error_during_resync_stops_retries(self) -> None:
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        await self.start()
        await self.finish(self.failed_job("Failed to copy: permission denied"))
        self.assertEqual(self.state()["attentionCode"], "resync_failed")
        self.assertIn("permission denied", self.f.attention)
        self.assertFalse(self.d._folder_due(self.f, time.time() + 3600))

    async def test_filter_edit_during_normal_job_does_not_count_as_failed_repair(
        self,
    ) -> None:
        await self.start()
        self.f.excludes = ["*.tmp"]
        self.d._check_filters(self.f)
        self.assertEqual(self.f.resync_reason, "filters")
        self.assertFalse(self.state()["resync"], "report the running job's actual mode")
        await self.finish(self.failed_job(BROKEN))
        self.assertFalse(self.f.attention)
        self.assertEqual(self.f.resync_reason, "recover")
        self.assertTrue((await self.start())["resync"])

    async def test_transient_repair_failures_back_off_without_attention(self) -> None:
        for cause, kind, delay in (
            ("dial tcp: lookup example.invalid: no such host", "network", 30),
            ("googleapi: Error 403: Quota exceeded (rateLimitExceeded)", "rate", 120),
        ):
            with self.subTest(kind=kind):
                self.f.resync_reason = "recover"
                self.f.backoff = 0
                await self.start()
                message = (
                    "Bisync critical error: "
                    + cause
                    + "\nMust run --resync to recover."
                )
                await self.finish(self.failed_job(message))
                self.assertFalse(self.f.attention)
                self.assertEqual(self.f.error_kind, kind)
                self.assertEqual(self.f.backoff, delay)
                self.assertFalse(self.d._folder_due(self.f, time.time()))
                self.assertTrue((await self.start())["resync"])
                await self.finish(self.failed_job(message))
                self.assertEqual(self.f.backoff, delay * 2)
                self.assertFalse(self.f.attention)

    async def test_auth_and_lock_errors_are_not_hidden_by_resync_footer(self) -> None:
        await self.start()
        await self.finish(
            self.failed_job('oauth2: "invalid_grant"\nMust run --resync to recover.')
        )
        self.assertEqual(self.state()["attentionCode"], "auth")
        self.assertEqual(self.f.resync_reason, "recover")
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        await self.start()
        await self.finish(
            self.failed_job("Prior lock file found\nMust run --resync to recover.")
        )
        self.assertFalse(self.f.attention)
        self.assertEqual(self.f.error_kind, "lock")

    async def test_conflicts_survive_failed_sync_and_clear_keeps_files(self) -> None:
        copy = self.local / "one.conflict1.md"
        copy.write_text("other version")
        self.f.conflicts = ["older.conflict1.md"]
        other = Folder("b", "Cloud", "other", str(self.root / "other"))
        self.d.folders[other.id] = other
        await self.start()
        await self.finish(self.failed_job(BROKEN, CONFLICT + CONFLICT))
        self.assertEqual(self.state()["conflicts"], [copy.name, "older.conflict1.md"])
        self.assertEqual(self.state()["conflictCount"], 2)
        self.assertEqual(other.conflicts, [])
        self.assertEqual(self.d.build_state()["summary"]["attention"], 1)
        self.assertEqual(self.new_daemon().folders["a"].conflicts, self.f.conflicts)
        await self.start()
        await self.finish({"success": True})
        self.assertEqual(self.d.build_state()["summary"]["attention"], 1)
        await self.d.cmd_clear_conflicts({"id": "a"})
        self.assertTrue(copy.exists())
        self.assertEqual(self.new_daemon().folders["a"].conflicts, [])
        self.assertEqual(self.d.build_state()["summary"]["attention"], 0)

    async def test_paused_attention_remains_in_widget_summary(self) -> None:
        self.d._folder_attention(self.f, "safety", "Check both copies")
        self.f.paused = True
        self.assertEqual(self.state()["state"], "paused")
        self.assertTrue(self.state()["attention"])
        self.assertEqual(self.d.build_state()["summary"]["attention"], 1)

    async def test_force_cannot_silently_turn_into_a_pending_resync(self) -> None:
        self.f.resync_reason = "recover"
        self.d._folder_attention(self.f, "safety", "Check both copies")
        with self.assertRaisesRegex(UserError, "Resync first"):
            await self.d.cmd_resolve({"id": "a", "action": "force"})
        self.assertFalse(self.f.force_next)
        self.assertEqual(self.f.attention_code, "safety")
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        params = await self.start()
        self.assertTrue(params["resync"])
        self.assertNotIn("force", params)

    async def test_resync_clears_unconsumed_force_and_network_wait(self) -> None:
        self.f.force_next = True
        self.f.waiting_network = True
        self.f.error_kind = "network"
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        self.assertFalse(self.f.waiting_network)
        self.assertFalse(self.f.error_kind)
        params = await self.start()
        self.assertTrue(params["resync"])
        self.assertNotIn("force", params)


@unittest.skipUnless(shutil.which("rclone"), "real-job coverage needs rclone")
class RealBisyncRecovery(RecoverySandbox):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.f.initialized = False
        for i in range(4):
            (self.cloud / f"{i}.md").write_text(f"original {i}\n")
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")

    async def sync(self) -> None:
        self.assertTrue(await self.d._start_sync(self.f))
        deadline = time.monotonic() + 20
        while self.f.job_id and time.monotonic() < deadline:
            await self.d._poll_job(self.f)
            await asyncio.sleep(0.05)
        self.assertFalse(self.f.job_id, "local rclone job timed out")

    async def test_mass_delete_restores_via_widget_resync(self) -> None:
        for file in self.local.glob("*.md"):
            file.unlink()
        await self.sync()
        self.assertIn(self.state()["attentionCode"], ("safety", "empty"))
        self.assertEqual(len(list(self.cloud.glob("*.md"))), 4)
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")
        for file in self.cloud.glob("*.md"):
            self.assertEqual((self.local / file.name).read_text(), file.read_text())

    async def test_mass_delete_applies_once_via_widget_force(self) -> None:
        for i in range(3):
            (self.local / f"{i}.md").unlink()
        await self.sync()
        self.assertEqual(self.state()["attentionCode"], "safety")
        self.assertEqual(len(list(self.cloud.glob("*.md"))), 4)
        await self.d.cmd_resolve({"id": "a", "action": "force"})
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")
        self.assertEqual([p.name for p in self.cloud.glob("*.md")], ["3.md"])

    async def test_missing_listings_repair_without_manual_state_cleanup(self) -> None:
        for listing in paths.bisync_dir("a").glob("*.lst"):
            listing.unlink()
        await self.sync()
        self.assertEqual(
            self.f.resync_reason, "recover", paths.rclone_log().read_text()
        )
        self.assertFalse(self.f.attention)
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")
        self.assertTrue(self.f.recovery_attempted)
        await self.sync()
        self.assertFalse(self.f.recovery_attempted)

    async def test_conflict_copies_are_actionable_and_preserved(self) -> None:
        (self.cloud / "0.md").write_text("cloud version\n")
        (self.local / "0.md").write_text("local version wins\n")
        now = time.time()
        os.utime(self.cloud / "0.md", (now - 5, now - 5))
        os.utime(self.local / "0.md", (now, now))
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")
        self.assertEqual(self.state()["conflicts"], ["0.conflict1.md"])
        self.assertEqual(self.d.build_state()["summary"]["attention"], 1)
        for directory in (self.cloud, self.local):
            self.assertEqual((directory / "0.md").read_text(), "local version wins\n")
            self.assertEqual(
                (directory / "0.conflict1.md").read_text(), "cloud version\n"
            )
        await self.d.cmd_clear_conflicts({"id": "a"})
        self.assertEqual(self.state()["conflictCount"], 0)
        self.assertTrue((self.local / "0.conflict1.md").exists())

    async def test_repeated_missing_listings_stop_and_offer_resync(self) -> None:
        for attempt in range(2):
            listings = list(paths.bisync_dir("a").glob("*.lst"))
            self.assertEqual(len(listings), 2)
            for listing in listings:
                listing.unlink()
            await self.sync()
            if attempt == 0:
                self.assertEqual(self.f.resync_reason, "recover")
                await self.sync()
                self.assertTrue(self.f.recovery_attempted)
            else:
                self.assertEqual(self.f.attention_code, "resync_failed")
                self.assertFalse(self.d._folder_due(self.f, time.time() + 3600))
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        await self.sync()
        self.assertEqual(self.state()["state"], "synced")
        self.assertEqual(len(list(self.local.glob("*.md"))), 4)


@unittest.skipUnless(
    shutil.which("qs") and OMARCHY_SHELL.is_dir(),
    "widget coverage needs Quickshell and the Omarchy shell",
)
class WidgetRecovery(unittest.TestCase):
    def test_existing_controls_and_visibility(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gw-") as directory:
            root = Path(directory)
            shell = root / "shell"
            shell.mkdir()
            shutil.copy(REPO / "tests" / "recovery_widget.qml", shell / "shell.qml")
            for name in ("Commons", "Ui"):
                (shell / name).symlink_to(OMARCHY_SHELL / name)
            (shell / "gui").symlink_to(REPO / "ui")
            runtime = root / "run"
            runtime.mkdir(mode=0o700)
            env = dict(os.environ)
            original_runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            # Quickshell requires a visible Wayland socket even with Qt's offscreen platform.
            env["WAYLAND_DISPLAY"] = str(
                Path(original_runtime) / env.get("WAYLAND_DISPLAY", "wayland-1")
            )
            env.update(
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_CONFIG_HOME": str(root / "cfg"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "QT_QPA_PLATFORM": "offscreen",
                }
            )
            result = subprocess.run(
                ["qs", "-p", str(shell / "shell.qml")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("RECOVERY_WIDGET_PASS", output, output)
            self.assertNotIn("RECOVERY_WIDGET_FAIL", output, output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
