import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import web_app


class FakeBridge:
    def __init__(self, host, address, code, *args, **kwargs):
        self.host = host
        self.code = code
        self.share_targets = lambda now: []
        self.peer = None
        self.last_peer = 0
        self.events = queue.Queue()
        self.local = None
        self.thread = type("Thread", (), {"is_alive": lambda self: True})()

    def close(self):
        pass


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = patch.object(Path, "home", return_value=Path(self.temp.name))
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.temp.cleanup()

    def test_saved_ports_and_report(self):
        with patch.object(web_app, "Bridge", FakeBridge), patch.object(
            web_app.mido, "get_output_names", return_value=["out"]
        ), patch.object(web_app.mido, "get_input_names", return_value=["in"]):
            c = web_app.Controller()
            c.action(
                "connect",
                dict(role="Join", address="192.168.1.2", code="PRIV-ATEC-ODE9", to_fl="out", from_fl="in"),
            )
            self.assertEqual(web_app.Controller().settings["to_fl"], "out")
            c.bridge.events.put(dict(fl=True, peer=False, rx=4, tx=5))
            self.assertIn("FL is responding", c.state()["hint"])
            c.action("export", {})
            report = json.loads(next((Path(self.temp.name) / "Downloads").glob("*.json")).read_text())
            self.assertNotIn("PRIV-ATEC-ODE9", json.dumps(report))
            self.assertNotIn("192.168.1.2", json.dumps(report))
            self.assertEqual(report["status"]["rx"], 4)
            c.bridge.thread.is_alive = lambda: False
            self.assertIn("Connection stopped", c.state()["hint"])

    def test_missing_port_rejected(self):
        with patch.object(web_app.mido, "get_output_names", return_value=[]), patch.object(
            web_app.mido, "get_input_names", return_value=[]
        ):
            c = web_app.Controller()
            with self.assertRaisesRegex(ValueError, "unavailable"):
                c.action("connect", dict(code="ABCD-EFGH-JKMN"))
            self.assertFalse(c.state()["connected"])

    def test_local_preview_without_session_or_midi(self):
        import threading

        capture_started = threading.Event()

        def capture(*args):
            capture_started.set()
            raise ValueError("Allow Screen Recording for Terminal/Python")

        with patch("screen_view.capture_fl", side_effect=capture), patch.object(
            web_app.mido, "get_output_names", side_effect=AssertionError("MIDI not needed")
        ):
            c = web_app.Controller()
            c.action("start-view", {})
            self.assertTrue(capture_started.wait(1))
            c.sharing.view_thread.join(1)
            state = c.state()
            self.assertFalse(state["connected"])
            self.assertIn("Screen Recording", state["sharing"]["view_error"])
            self.assertFalse(state["sharing"]["viewing"])
            self.assertIsNone(c.sharing.server)
            c.disconnect()

    def test_listening_is_opt_in_and_remembered(self):
        c = web_app.Controller()
        self.assertFalse(c.settings.get("listen", False))
        with patch.object(c.audio_key, "start"), patch.object(c.audio_key, "stop"):
            c.action("listen", {})
            self.assertTrue(web_app.Controller().settings["listen"])
            c.action("stop-listen", {})
            self.assertFalse(web_app.Controller().settings["listen"])
            c.close_audio()

    def test_settings_write_is_atomic(self):
        c = web_app.Controller()
        c.settings["start_wait"] = 500
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                c.save_settings()
        folder = Path(self.temp.name) / ".studio-sync"
        self.assertFalse(list(folder.glob("*.tmp")))
        c.save_settings()
        self.assertEqual(json.loads((folder / "settings.json").read_text())["start_wait"], 500)

    def test_audio_file_supports_byte_ranges(self):
        import threading, urllib.request
        from http.server import ThreadingHTTPServer
        from types import SimpleNamespace

        source = Path(self.temp.name) / "take.wav"
        source.write_bytes(bytes(range(256)) * 4)
        sharing = SimpleNamespace(received=lambda ident: dict(name="take.wav", path=str(source)))
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), web_app.make_handler(SimpleNamespace(sharing=sharing), "tok")
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/file?token=tok&id=x"
            req = urllib.request.Request(
                url, headers={"Host": f"127.0.0.1:{server.server_port}", "Range": "bytes=10-19"}
            )
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(r.headers["Content-Range"], "bytes 10-19/1024")
                self.assertEqual(r.read(), bytes(range(10, 20)))
            req = urllib.request.Request(url, headers={"Range": "bytes=-4"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.read(), bytes(range(252, 256)))
            with urllib.request.urlopen(url) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(len(r.read()), 1024)
                self.assertEqual(r.headers["Accept-Ranges"], "bytes")
        finally:
            server.shutdown()
            server.server_close()

    def test_auto_audio_respects_stop_and_recovers(self):
        c = web_app.Controller()
        with patch.object(c.audio_key, "start") as start, patch.object(c.audio_key, "stop"):
            c.auto_listen = True
            c.ensure_audio_listening()
            self.assertEqual(start.call_count, 1)
            c.audio_key.result["listening"] = True
            c.ensure_audio_listening()
            self.assertEqual(start.call_count, 1)
            with patch.object(c, "save_settings"):
                c.action("stop-listen", {})
            c.audio_key.result["listening"] = False
            c.ensure_audio_listening()
            self.assertEqual(start.call_count, 1)
            with patch.object(c, "save_settings"):
                c.action("listen", {})
            self.assertEqual(start.call_count, 2)
            c.ensure_audio_listening()
            self.assertEqual(start.call_count, 3)
            c.close_audio()
            c.ensure_audio_listening()
            self.assertEqual(start.call_count, 3)

    def test_history_bounded(self):
        c = web_app.Controller()
        for i in range(600):
            c.record(str(i))
        self.assertEqual(len(c.history), 500)

    def test_save_folder_persists_and_native_save_as(self):
        from types import SimpleNamespace
        import sys

        folder = Path(self.temp.name) / "chosen"
        folder.mkdir()
        c = web_app.Controller()
        responses = iter([(str(folder),), (str(folder / "saved.zip"),)])
        c.window = SimpleNamespace(create_file_dialog=lambda *a, **k: next(responses))
        module = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=1, SAVE=2))
        source = Path(self.temp.name) / "received.zip"
        source.write_bytes(b"PKtest")
        c.sharing = SimpleNamespace(
            folder=None, received=lambda ident: dict(name="received.zip", path=str(source))
        )
        with patch.dict(sys.modules, webview=module):
            c.action("choose-save-folder", {})
            self.assertEqual(web_app.Controller().settings["save_folder"], str(folder))
            self.assertEqual(c.sharing.folder, folder)
            c.action("save-file-as", dict(id="test"))
        self.assertEqual((folder / "saved.zip").read_bytes(), b"PKtest")
        self.assertFalse(list(folder.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
