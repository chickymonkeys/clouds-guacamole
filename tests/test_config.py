#!/usr/bin/env python3
"""Unit tests for settings, provider guides and path rules (no rclone needed)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = dict(os.environ)
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "cfg")
        os.environ["XDG_STATE_HOME"] = str(Path(self.tmp.name) / "state")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_defaults(self) -> None:
        from guac import config
        cfg = config.load()
        self.assertEqual(cfg["mount_root"], "~/Cloud/Stream")
        self.assertEqual(cfg["local_root"], "~/Cloud/Sync")
        self.assertEqual(cfg["remotes"], {})
        self.assertTrue((Path(os.environ["XDG_CONFIG_HOME"]) / "guacamole" / "config.json").exists(), "first load saves")
        cfg["mount_root"] = "~/Elsewhere"
        config.save(cfg)
        self.assertEqual(config.load()["mount_root"], "~/Elsewhere")

    def test_hidden_hints_survive(self) -> None:
        from guac import config
        entry = config.normalize_remote({"stream": True, "hidden_hints": ["shared_app", "shared_app", 3]})
        self.assertEqual(entry["hidden_hints"], ["3", "shared_app"])
        self.assertEqual(config.normalize_remote({})["hidden_hints"], [])

    def test_config_is_private(self) -> None:
        from guac import config
        config.save(config.load())
        mode = (Path(os.environ["XDG_CONFIG_HOME"]) / "guacamole" / "config.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_coerce(self) -> None:
        from guac import config
        self.assertIs(config.coerce("notifications", "off"), False)
        self.assertEqual(config.coerce("sync_interval_min", "7"), 7)
        with self.assertRaises(ValueError):
            config.coerce("sync_interval_min", "0")
        with self.assertRaises(ValueError):
            config.coerce("mount_root", "  ")


class ProviderTests(unittest.TestCase):
    def test_guides_follow_rclone_docs(self) -> None:
        from guac import providers
        self.assertEqual(set(providers.CLIENT_GUIDES), providers.OAUTH_TYPES, "every OAuth backend gets a guide")
        self.assertTrue(providers.CLIENT_GUIDES["drive"]["required"])
        for rtype, guide in providers.CLIENT_GUIDES.items():
            self.assertTrue(guide["steps"] and guide["docs"].startswith("https://rclone.org/" + rtype), rtype)
            for step in guide["steps"]:
                self.assertTrue(step["title"] and step["text"], rtype)
                if "link" in step:
                    self.assertTrue(step["link"]["url"].startswith("https://"), rtype)
        # Redirect URIs that rclone authorize actually uses for each backend
        copies = {r: [c["value"] for s in g["steps"] for c in s.get("copy", [])] for r, g in providers.CLIENT_GUIDES.items()}
        self.assertIn("http://localhost:53682/", copies["dropbox"])
        self.assertIn("http://localhost:53682/", copies["onedrive"])
        self.assertIn("http://127.0.0.1:53682/", copies["box"])
        self.assertIn("http://localhost:53682/", copies["pcloud"])
        self.assertIsNone(providers.client_guide("webdav"))

    def test_client_checks(self) -> None:
        from guac.providers import check_client
        # Obviously fake values in each provider's format: never paste real credentials here
        gid = "0-fakeclient.apps.googleusercontent.com"
        self.assertEqual(check_client("drive", gid, "fake-secret-for-tests"), "")
        self.assertIn("googleusercontent", check_client("drive", "fake-secret-for-tests", gid))
        self.assertEqual(check_client("dropbox", "fakeappkey", "fakeappsecret"), "")
        self.assertIn("same", check_client("dropbox", "fakeappkey", "fakeappkey"))
        guid = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(check_client("onedrive", guid, "fake-secret-value-for-tests"), "")
        self.assertIn("Secret ID", check_client("onedrive", guid, "00000000-0000-0000-0000-000000000001"))
        self.assertEqual(check_client("box", "a" * 32, "b" * 32), "")
        self.assertEqual(check_client("pcloud", "fakeclientid", "fakeclientsecret"), "")
        self.assertIn("needed", check_client("box", "a" * 32, ""))

    def test_warnings(self) -> None:
        from guac.providers import remote_warnings, visible_warnings
        drive = remote_warnings({"type": "drive"})
        self.assertEqual([w["code"] for w in drive], ["shared_client_id"])
        self.assertEqual(remote_warnings({"type": "drive", "client_id": "x"}), [])
        hint = remote_warnings({"type": "dropbox"})
        self.assertEqual([w["code"] for w in hint], ["shared_app"])
        self.assertEqual(visible_warnings(hint, ["shared_app"]), [])
        self.assertEqual(visible_warnings(drive, ["shared_client_id"]), drive, "the Google warning can't be hidden")
        for rtype in ("onedrive", "box", "pcloud"):
            self.assertEqual([w["code"] for w in remote_warnings({"type": rtype})], ["shared_app"], rtype)
        self.assertEqual(remote_warnings({"type": "webdav"}), [])


class PathTests(unittest.TestCase):
    def test_overlaps(self) -> None:
        from guac.paths import overlaps
        self.assertTrue(overlaps(Path("/a/b"), Path("/a/b/c")))
        self.assertTrue(overlaps(Path("/a/b/c"), Path("/a/b")))
        self.assertFalse(overlaps(Path("/a/b"), Path("/a/bc")))

    def test_remote_path_cleaning(self) -> None:
        from guac.daemon import UserError, clean_remote_path
        self.assertEqual(clean_remote_path("/Documents//Notes/"), "Documents/Notes")
        with self.assertRaises(UserError):
            clean_remote_path("Documents/../../etc")

    def test_conflict_parsing(self) -> None:
        from guac.daemon import _parse_conflicts
        out = ("2026/10/03 20:08:02 NOTICE: - Path1             Renaming Path1 copy                         "
               "- /x/src/notes.conflict1.md\n2026/10/03 INFO  : a.txt: Copied (new)\n")
        self.assertEqual(_parse_conflicts(out), ["notes.conflict1.md"])

    def test_error_classification(self) -> None:
        from guac.util import classify_error
        self.assertEqual(classify_error("dial tcp: lookup www.googleapis.com: no such host"), "network")
        self.assertEqual(classify_error("googleapi: Error 403: Rate Limit Exceeded, rateLimitExceeded"), "rate")
        self.assertEqual(classify_error('oauth2: "invalid_grant" "Token has been expired or revoked."'), "auth")

    def test_bisync_failures(self) -> None:
        from guac.daemon import _transient_failure
        throttled = ("ERROR : Bisync critical error: couldn't list directory: googleapi: Error 403: Quota exceeded "
                     "for quota metric 'Queries' (rateLimitExceeded)\nERROR : Bisync aborted. Must run --resync to recover.")
        self.assertEqual(_transient_failure(throttled), "rate", "a throttled listing is retried, not a broken state")
        offline = "ERROR : Bisync critical error: dial tcp: lookup www.googleapis.com: no such host\n"
        self.assertEqual(_transient_failure(offline), "network")
        broken = ("ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings\n"
                  "ERROR : Bisync aborted. Must run --resync to recover.")
        self.assertEqual(_transient_failure(broken), "")

    def test_sync_speed(self) -> None:
        from guac.daemon import _stats_speed
        # rclone 1.75 leaves speedAvg empty for files copied by sync and bisync
        self.assertEqual(_stats_speed({"speed": 2461695.8, "transferring": [{"name": "a.pdf", "speedAvg": None}]}), 2461695)
        self.assertEqual(_stats_speed({"speed": 9.0, "transferring": [{"speedAvg": 100}, {"speedAvg": 50}]}), 150)
        self.assertEqual(_stats_speed({"speed": 2461695.8, "transferring": []}), 0, "idle shows no speed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
