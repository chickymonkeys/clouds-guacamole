#!/usr/bin/env python3
"""Bisync recovery regressions: injected failures and real local rclone jobs, no FUSE.

python3 tests/test_recovery.py
"""

import asyncio
import os
import re
import shutil
import signal
import socket
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
from guac.daemon import Daemon, Folder, SyncWorker, UserError

REPO = Path(__file__).resolve().parents[1]
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")

BROKEN = (
    "ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings\n"
    "ERROR : Bisync aborted. Must run --resync to recover."
)
CONFLICT = "NOTICE: - Path1 Renaming Path1 copy - Cloud:docs/one.conflict1.md\n"
UNREACHABLE = (
    'Bisync critical error: couldn\'t list files: Propfind "http://cloud/": '
    "dial tcp 127.0.0.1:1: connect: connection refused"
)


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


class MockedJobs(RecoverySandbox):
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


class FailureTransitions(MockedJobs):
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
                self.d.online = True
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
        self.f.error = "dial tcp: connection refused"
        self.f.error_kind = "network"
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        self.assertFalse(self.f.error)
        self.assertFalse(self.f.error_kind)
        params = await self.start()
        self.assertTrue(params["resync"])
        self.assertNotIn("force", params)


class SchedulerJobs(MockedJobs):
    async def lose_network(self) -> None:
        await self.start()
        await self.finish(self.failed_job(UNREACHABLE))

    async def scheduler_pass(self) -> AsyncMock:
        """One round of the scheduler, recording what it starts instead of starting it."""
        started = AsyncMock(return_value=True)
        self.d._start_sync = started
        self.d.engine_state = "running"
        await self.d._start_due_folders()
        return started


class SyncHandling(SchedulerJobs):
    """What a folder does when syncing can't happen, and what it tells you meanwhile."""

    async def test_network_failure_waits_without_asking_for_attention(self) -> None:
        await self.lose_network()
        self.assertEqual(self.state()["state"], "waiting")
        summary = self.d.build_state()["summary"]
        self.assertEqual(summary["attention"], 0, "nothing here needs the user")
        self.assertEqual(summary["waitingNetwork"], 1)
        self.assertFalse(self.d._folder_due(self.f, time.time()))
        self.assertTrue(self.d._folder_due(self.f, self.f.retry_at + 1))
        # Still waiting, not "synced", between the retry time and the retry finishing
        self.f.retry_at = time.time() - 1
        self.assertEqual(self.state()["state"], "waiting")

    async def test_stale_lock_waits_without_asking_for_attention(self) -> None:
        await self.start()
        await self.finish(self.failed_job("prior lock file found"))
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.state()["errorKind"], "lock")
        summary = self.d.build_state()["summary"]
        self.assertEqual(summary["attention"], 0)
        self.assertEqual(summary["waitingNetwork"], 0, "the network is fine")

    async def test_failures_with_something_to_do_still_show(self) -> None:
        for error, kind in (
            ("googleapi: Error 403: Quota exceeded (rateLimitExceeded)", "rate"),
            ("Failed to copy: no space left on device", "other"),
        ):
            with self.subTest(kind=kind):
                await self.start()
                await self.finish(self.failed_job(error))
                self.assertEqual(self.state()["state"], "error")
                self.assertEqual(self.d.build_state()["summary"]["attention"], 1)

    async def test_wrong_route_check_cannot_stall_a_folder(self) -> None:
        # VPN and policy-routing setups have no default route in the main table: the route
        # stays "down" forever, so a network failure must keep being retried on its own
        self.d.online = False
        await self.lose_network()
        self.assertTrue(self.d._folder_due(self.f, self.f.retry_at + 1))
        self.f.retry_at = self.f.next_due = 0
        started = await self.scheduler_pass()
        started.assert_awaited_once_with(self.f)

    async def test_a_retry_happens_when_it_says_it_does(self) -> None:
        # Started by a local edit, so the timer is minutes away
        self.f.next_due = time.time() + 3600
        await self.lose_network()
        self.assertLess(self.f.next_due, time.time() + 60)
        self.assertTrue(self.d._folder_due(self.f, self.f.retry_at + 1))
        self.assertEqual(self.state()["nextSync"], int(self.f.retry_at))

    async def test_lost_connection_is_not_repaired_with_a_resync(self) -> None:
        # bisync sets the listings aside here, and --recover puts them back on the next run;
        # a resync would bring back whatever was deleted since the last sync
        for cause in (
            "unchunked simple update failed: dial tcp: connection refused",
            "googleapi: Error 403: Quota exceeded (rateLimitExceeded)",
        ):
            with self.subTest(cause=cause):
                await self.start()
                await self.finish(
                    self.failed_job(
                        f"Bisync critical error: {cause}\n"
                        "Bisync aborted. Must run --resync to recover."
                    )
                )
                self.assertEqual(self.f.resync_reason, "")
                self.assertNotIn("resync", await self.start())
                await self.finish({"success": True})

    async def test_missing_backups_still_repair_after_the_retry(self) -> None:
        await self.lose_network()
        await self.start()
        await self.finish(self.failed_job(BROKEN))
        self.assertEqual(self.f.resync_reason, "recover")
        self.assertFalse(self.f.attention)

    async def test_why_a_folder_waits_outlives_a_restart(self) -> None:
        await self.lose_network()
        restored = self.new_daemon()
        f = restored.folders["a"]
        self.assertEqual(restored._folder_state(f, time.time())["state"], "waiting")

    async def test_sync_now_retries_a_waiting_folder_at_once(self) -> None:
        self.d.online = False
        await self.lose_network()
        await self.d.cmd_sync_now({"id": "a"})
        self.assertFalse(self.f.error_kind)
        started = await self.scheduler_pass()
        started.assert_awaited_once_with(self.f)

    async def test_no_route_retries_rarely_until_it_comes_back(self) -> None:
        self.d.online = False
        await self.lose_network()
        self.assertEqual(self.f.backoff, 600)
        self.assertEqual(self.d._folder_due_at(self.f, 0.0), self.f.retry_at)
        with patch("guac.daemon.util.has_default_route", return_value=True):
            self.d.engine_state = "stopped"
            await self.d._tick()
        self.assertTrue(self.d.online)
        self.assertLessEqual(self.f.retry_at, time.time() + 10)
        self.assertEqual(self.f.backoff, 0)

    async def test_scheduler_sleeps_through_folders_that_cannot_run(self) -> None:
        self.f.next_due = 0
        del self.d.remotes["Cloud"]
        self.assertGreater(self.d._scheduler_sleep(time.time()), 1.0)

    async def test_failed_sync_keeps_changes_waiting(self) -> None:
        self.d._mark_folder_dirty(self.f, time.time())
        self.assertTrue(self.state()["localChanges"])
        await self.start()
        self.assertFalse(self.state()["localChanges"], "they are on their way")
        await self.finish(self.failed_job(UNREACHABLE))
        self.assertTrue(self.state()["localChanges"], "they did not get there")
        self.assertFalse(self.d._folder_due(self.f, time.time()), "backoff still rules")
        self.f.retry_at = 0
        await self.start()
        await self.finish({"success": True})
        self.assertFalse(self.state()["localChanges"])

    async def test_changes_seen_during_a_failed_sync_are_kept(self) -> None:
        await self.start()
        self.d._mark_folder_dirty(self.f, time.time())
        await self.finish(self.failed_job(UNREACHABLE))
        self.assertTrue(self.f.dirty_since)

    async def test_lost_worker_backs_off_and_says_so_when_it_repeats(self) -> None:
        delays = []
        for _ in range(4):
            await self.start()
            self.d.workers.pop("a", None)
            await self.d._poll_job(self.f)
            self.assertFalse(self.f.job_id)
            delays.append(self.f.backoff)
        self.assertEqual(delays, [5, 10, 20, 40])
        self.assertEqual(self.state()["state"], "error")
        self.assertIn("rclone", self.f.error)
        self.f.retry_at = 0
        await self.start()
        await self.finish({"success": True})
        self.assertEqual((self.f.backoff, self.f.error), (0, ""))

    async def test_one_lost_worker_is_not_worth_showing(self) -> None:
        await self.start()
        await self.d._poll_job(self.f)
        self.assertEqual(self.f.backoff, 5)
        self.assertEqual(self.f.error, "")

    async def test_lost_worker_keeps_changes_waiting(self) -> None:
        self.d._mark_folder_dirty(self.f, time.time())
        await self.start()
        await self.d._poll_job(self.f)
        self.assertTrue(self.state()["localChanges"])

    async def test_restored_local_folder_is_watched_again(self) -> None:
        watch = self.d._watch_folder = Mock()
        self.f.watching = False
        self.d._folder_attention(self.f, "missing_local", "Restore it")
        await self.d.cmd_resolve({"id": "a", "action": "resync"})
        await self.start()
        await self.finish({"success": True})
        watch.assert_called_once_with(self.f)

    async def test_watching_folder_is_left_alone(self) -> None:
        watch = self.d._watch_folder = Mock()
        self.f.watching = True
        await self.start()
        await self.finish({"success": True})
        watch.assert_not_called()

    async def test_sync_now_is_honest_about_a_folder_it_cannot_sync(self) -> None:
        self.d._folder_attention(self.f, "safety", "Check both copies")
        with self.assertRaisesRegex(UserError, "needs attention"):
            await self.d.cmd_sync_now({"id": "a"})
        reply = await self.d.cmd_sync_now({})
        self.assertIn("needs attention", reply["message"])
        self.assertNotIn("Syncing now", reply["message"])

    async def test_sync_all_counts_what_it_skipped(self) -> None:
        other = Folder("b", "Cloud", "other", str(self.root / "other"))
        self.d.folders[other.id] = other
        other.paused = True
        third = Folder("c", "Cloud", "third", str(self.root / "third"))
        self.d.folders[third.id] = third
        self.d._folder_attention(third, "safety", "Check both copies")
        reply = await self.d.cmd_sync_now({})
        self.assertEqual(reply["message"], "Syncing now (1 paused, 1 needs attention)")


class StoppingASync(SchedulerJobs):
    """Through a signal to the sync's own rclone, whatever the reason for stopping."""

    def running_worker(self) -> Mock:
        """The sync's own rclone, as far as stopping it goes."""
        proc = Mock(returncode=None, wait=AsyncMock(return_value=0))
        self.d.workers["a"] = SyncWorker(proc, Mock())
        return proc

    async def test_stopping_never_goes_through_job_stop(self) -> None:
        """It loses the listings: a resync follows, deleted files come back, and on a backend
        without atomic uploads a partial upload replaces the whole file on both sides."""
        self.running_worker()
        await self.start()
        await self.d._cancel_sync(self.f)
        calls = [c.args[0] for c in self.rc_mock.call_args_list]
        self.assertNotIn("job/stop", calls)
        self.assertFalse(self.f.job_id or self.f.stopping)

    async def test_a_sync_that_wont_wind_down_is_ended(self) -> None:
        async def hang() -> None:
            await asyncio.sleep(60)

        proc = self.running_worker()
        proc.wait = hang
        stop_worker = self.d._stop_worker = AsyncMock()
        await self.start()
        await self.d._cancel_sync(self.f, wait=0.01)
        stop_worker.assert_awaited_once_with("a")
        self.assertFalse(self.f.job_id or self.f.stopping)

    async def test_two_stops_at_once_signal_once(self) -> None:
        proc = self.running_worker()
        await self.start()
        await asyncio.gather(self.d._cancel_sync(self.f), self.d._cancel_sync(self.f))
        proc.send_signal.assert_called_once_with(signal.SIGINT)

    async def test_a_sync_winding_down_is_not_read_as_a_failure(self) -> None:
        await self.start()
        self.d._finish_sync = AsyncMock()

        async def stopped_meanwhile(*_args, **_kwargs) -> dict:
            self.f.stopping = True
            return {"finished": True, "success": False, "error": "context canceled"}

        self.running_worker()
        self.rc_mock.side_effect = stopped_meanwhile
        await self.d._poll_job(self.f)
        await self.d._poll_job(self.f)
        self.d._finish_sync.assert_not_awaited()


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

    async def test_connection_lost_during_a_transfer_keeps_deletions(self) -> None:
        (self.local / "0.md").unlink()
        (self.local / "1.md").write_text("edited offline\n")
        # What that failure leaves: the listings set aside, their backups next to them
        for listing in paths.bisync_dir("a").glob("*.lst"):
            shutil.copy2(listing, f"{listing}-old")
            listing.rename(f"{listing}-err")
        await self.d._finish_sync(
            self.f,
            self.failed_job(
                "Bisync critical error: Put: dial tcp: connection refused\n"
                "Bisync aborted. Must run --resync to recover."
            ),
        )
        self.assertEqual(self.state()["state"], "waiting")
        self.f.retry_at = 0
        await self.sync()
        self.assertEqual(self.state()["state"], "synced", self.f.error)
        self.assertFalse(self.f.run_resync, "an ordinary sync is all it takes")
        self.assertFalse((self.local / "0.md").exists(), "not brought back")
        self.assertFalse((self.cloud / "0.md").exists(), "and gone from the cloud")
        self.assertEqual((self.cloud / "1.md").read_text(), "edited offline\n")

    async def test_partial_upload_left_in_the_cloud_never_costs_the_file(self) -> None:
        # A backend without atomic uploads can keep what arrived before the connection went
        whole = "the whole file\n" * 2000
        (self.local / "big.bin").write_text(whole)
        (self.cloud / "big.bin").write_text(whole[:100])
        for listing in paths.bisync_dir("a").glob("*.lst"):
            shutil.copy2(listing, f"{listing}-old")
            listing.rename(f"{listing}-err")
        await self.sync()
        self.assertEqual(self.state()["state"], "synced", self.f.error)
        for directory in (self.local, self.cloud):
            versions = [p.read_text() for p in directory.glob("big*.bin")]
            self.assertIn(whole, versions, f"the whole file is in {directory}")
        self.assertEqual(self.state()["conflictCount"], 1, "and the user is told")

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


@unittest.skipUnless(shutil.which("rclone"), "real-job coverage needs rclone")
class RealNetworkLoss(RecoverySandbox):
    """A real HTTP remote, so a lost connection is a genuine one."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.server: subprocess.Popen | None = None
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        (self.root / "rclone.conf").write_text(
            "[Cloud]\ntype = webdav\nvendor = other\n"
            f"url = http://127.0.0.1:{self.port}\n"
        )
        self.d.remotes["Cloud"]["type"] = "webdav"
        self.f.initialized = False
        self.serve()
        for i in range(8):
            (self.cloud / f"{i}.md").write_text(f"original {i}\n")
        await self.sync()
        self.assertEqual(self.state()["state"], "synced", self.f.error)

    async def asyncTearDown(self) -> None:
        self.cut()
        await super().asyncTearDown()

    def serve(self) -> None:
        self.server = subprocess.Popen(
            ["rclone", "serve", "webdav", str(self.cloud)]
            + ["--addr", f"127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        self.fail("the test server did not start")

    def cut(self) -> None:
        if self.server is not None:
            self.server.kill()
            self.server.wait()
            self.server = None

    async def sync(self) -> None:
        self.assertTrue(await self.d._start_sync(self.f))
        deadline = time.monotonic() + 20
        while self.f.job_id and time.monotonic() < deadline:
            await self.d._poll_job(self.f)
            await asyncio.sleep(0.05)
        self.assertFalse(self.f.job_id, "rclone job timed out")

    async def test_waits_quietly_then_catches_up_without_a_resync(self) -> None:
        self.cut()
        (self.local / "0.md").write_text("edited with no network\n")
        (self.local / "1.md").unlink()
        self.d._mark_folder_dirty(self.f, time.time())
        await self.sync()
        state = self.state()
        self.assertEqual((state["state"], state["errorKind"]), ("waiting", "network"))
        self.assertTrue(state["localChanges"])
        summary = self.d.build_state()["summary"]
        self.assertEqual((summary["attention"], summary["waitingNetwork"]), (0, 1))

        self.serve()
        await self.d.cmd_sync_now({"id": "a"})
        await self.sync()
        self.assertEqual(self.state()["state"], "synced", self.f.error)
        self.assertFalse(self.state()["localChanges"])
        self.assertFalse(self.f.run_resync)
        self.assertEqual((self.cloud / "0.md").read_text(), "edited with no network\n")
        self.assertFalse((self.local / "1.md").exists(), "not brought back")
        self.assertFalse((self.cloud / "1.md").exists())

    async def start_moving(self) -> bytes:
        """A sync with a deletion and an edit to carry, stopped partway through a big upload."""
        data = os.urandom(150 * 1024 * 1024)
        (self.local / "big.bin").write_bytes(data)
        (self.local / "1.md").unlink()
        (self.local / "0.md").write_text("edited\n")
        self.d._mark_folder_dirty(self.f, time.time())
        self.assertTrue(await self.d._start_sync(self.f))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await self.d._poll_job(self.f)
            self.assertTrue(self.f.job_id, "the sync ended before it could be stopped")
            if self.f.progress.get("bytes", 0) > 1_000_000:
                return data
            await asyncio.sleep(0.02)
        self.fail("nothing moved")

    def assert_ordinary_sync_lost_nothing(self) -> None:
        self.assertEqual(self.state()["state"], "synced", self.f.error)
        self.assertFalse(self.f.run_resync, "no resync: the listings survived")
        self.assertFalse((self.local / "1.md").exists(), "deleted stays deleted")
        self.assertFalse((self.cloud / "1.md").exists())
        self.assertEqual((self.cloud / "0.md").read_text(), "edited\n")

    async def test_shutting_down_mid_sync_loses_nothing(self) -> None:
        """What runs at logout, reboot and guac stop."""
        data = await self.start_moving()
        await self.d._shutdown()
        self.assertFalse(self.f.job_id)
        await self.sync()
        self.assert_ordinary_sync_lost_nothing()
        self.assertEqual((self.cloud / "big.bin").read_bytes(), data)


class WidgetSource(unittest.TestCase):
    def test_theme_colors_never_use_the_bare_name(self) -> None:
        # QtQuick has its own Color singleton since Qt 6.12, which hides the shell's: under a
        # plain import every theme color is undefined, and the panel loses them silently
        bare = re.compile(r"(?<![\w.])Color\.[a-z]")
        files = [*(REPO / "ui").glob("*.qml"), REPO / "docs/screenshots/shots.qml"]
        for path in files:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(bare.search(line), f"{path.name}:{number}: {line}")


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
