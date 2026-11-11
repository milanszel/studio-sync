import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater
from updater import Updater, parse_version, pick_asset

INSTALLER = b"pretend installer" * 1000


def latest(version="9.9.9", data=INSTALLER, sha=None):
    entry = lambda name: dict(
        name=name,
        url="https://example.invalid/" + name,
        size=len(data),
        sha256=sha or hashlib.sha256(data).hexdigest(),
    )
    return dict(
        version=version,
        notes="Fixes",
        files={
            "windows": entry("Studio-Sync-Setup-%s.exe" % version),
            "mac-x86_64": entry("Studio-Sync-%s-Mac-x86_64.dmg" % version),
        },
    )


class FakeWeb:
    def __init__(self, release, data=INSTALLER):
        self.release, self.data, self.urls = release, data, []

    def __call__(self, req):
        self.urls.append(req.full_url)
        if "latest.json" in req.full_url:
            return io.BytesIO(json.dumps(self.release).encode())
        return io.BytesIO(self.data)


class UpdaterTests(unittest.TestCase):
    def test_versions_compare_as_numbers(self):
        self.assertGreater(parse_version("0.12.0"), parse_version("0.11.10"))
        self.assertGreater(parse_version("0.11.10"), parse_version("0.11.9"))
        self.assertEqual(parse_version("v1.2"), (1, 2, 0))

    def test_picks_the_right_installer(self):
        files = latest()["files"]
        self.assertIn("Setup", pick_asset(files, "win32")["name"])
        self.assertIn("x86_64", pick_asset(files, "darwin", "x86_64")["name"])
        # Apple silicon falls back to the Intel build when there's no arm64 one.
        self.assertIn("x86_64", pick_asset(files, "darwin", "arm64")["name"])
        files["mac-arm64"] = dict(name="Studio-Sync-9.9.9-Mac-arm64.dmg")
        self.assertIn("arm64", pick_asset(files, "darwin", "arm64")["name"])
        self.assertIsNone(pick_asset(files, "linux"))

    def test_newer_version_is_offered(self):
        u = Updater("0.11.1", True, opener=FakeWeb(latest("0.12.0")))
        with patch.object(updater, "pick_asset", lambda files: files["windows"]):
            u.check()
        state = u.state()
        self.assertEqual(state["status"], "available")
        self.assertEqual(state["latest"], "0.12.0")
        self.assertTrue(state["can_install"])

    def test_same_or_older_version_is_not_offered(self):
        for version in ("0.11.1", "0.10.0"):
            u = Updater("0.11.1", True, opener=FakeWeb(latest(version)))
            with patch.object(updater, "pick_asset", lambda files: files["windows"]):
                u.check()
            self.assertEqual(u.state()["status"], "current")

    def test_offline_check_is_quiet(self):
        def offline(req):
            raise OSError("no network")

        u = Updater("0.11.1", True, opener=offline)
        u.check()
        self.assertEqual(u.state()["status"], "idle")

    def test_download_is_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            good = latest()["files"]["windows"]
            u = Updater("0.11.1", True, folder=folder, opener=FakeWeb(latest()))
            path = u.download(good)
            self.assertEqual(Path(path).read_bytes(), INSTALLER)
            bad = latest(sha="0" * 64)["files"]["windows"]
            with self.assertRaisesRegex(ValueError, "corrupted"):
                u.download(bad)
            self.assertFalse(any(Path(folder).glob("*.exe")))

    def test_truncated_download_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = latest()["files"]["windows"]
            u = Updater("0.11.1", True, folder=folder, opener=FakeWeb(latest(), data=INSTALLER[:100]))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                u.download(asset)

    def test_running_from_source_cannot_install(self):
        u = Updater("0.11.1", False, opener=FakeWeb(latest()))
        with patch.object(updater, "pick_asset", lambda files: files["windows"]):
            u.check()
        self.assertFalse(u.state()["can_install"])
        with self.assertRaisesRegex(ValueError, "source"):
            u.install()


class FLScriptSyncTests(unittest.TestCase):
    def test_installed_script_is_updated_with_the_app(self):
        import web_app

        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / "Documents" / "Image-Line" / "FL Studio"
            target = folder / "Settings" / "Hardware" / "Studio-Sync" / "device_studio_sync.py"
            target.parent.mkdir(parents=True)
            target.write_text('VERSION = "0.5.0"\n')
            with patch.object(Path, "home", lambda: Path(home)):
                c = web_app.Controller(ports=("in", "out"))
                c.sync_fl_script()
            bundled = (web_app.ROOT / "FL-Script" / "Studio-Sync" / "device_studio_sync.py").read_bytes()
            self.assertEqual(target.read_bytes(), bundled)
            self.assertEqual(target.with_suffix(".py.backup").read_text(), 'VERSION = "0.5.0"\n')
            self.assertTrue(c.fl_script_updated)

    def test_nothing_is_created_where_fl_has_no_script(self):
        import web_app

        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", lambda: Path(home)):
                c = web_app.Controller(ports=("in", "out"))
                c.sync_fl_script()
            self.assertFalse(any(Path(home).rglob("device_studio_sync.py")))
            self.assertIsNone(c.fl_script_updated)


if __name__ == "__main__":
    unittest.main()
