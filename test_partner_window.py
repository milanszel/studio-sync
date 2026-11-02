import unittest
from unittest.mock import patch, Mock
from pathlib import Path
from web_app import Controller


class PartnerWindowTests(unittest.TestCase):
    def test_single_window_close_reopen_leaves_session(self):
        controller = Controller.__new__(Controller)
        import threading

        controller.lock = threading.RLock()
        controller.partner_window = None
        controller.app_url = "http://127.0.0.1:1234"
        controller.bridge = object()
        controller.sharing = object()
        windows = []

        class Event:
            def __init__(self):
                self.callbacks = []

            def __iadd__(self, fn):
                self.callbacks.append(fn)
                return self

        def create(*args, **kwargs):
            w = Mock()
            w.events.closed = Event()
            windows.append(w)
            return w

        module = Mock()
        module.create_window.side_effect = create
        with patch.dict("sys.modules", webview=module):
            controller.action("pop-out", {})
            module.create_window.assert_called_once()
            self.assertTrue(module.create_window.call_args.kwargs["on_top"])
            self.assertEqual(module.create_window.call_args.args[1], controller.app_url + "/partner")
            controller.action("pop-out", {})
            windows[0].show.assert_called_once()
            windows[0].events.closed.callbacks[0]()
            self.assertIsNone(controller.partner_window)
            self.assertIsNotNone(controller.bridge)
            self.assertIsNotNone(controller.sharing)
            controller.action("pop-out", {})
            self.assertEqual(len(windows), 2)

    def test_capture_is_window_only(self):
        source = (Path(__file__).parent / "screen_view.py").read_text()
        self.assertIn("initWithDesktopIndependentWindow_", source)
        self.assertIn("SCScreenshotManager", source)
        self.assertNotIn("CGWindowListCreateImage", source)
        self.assertIn("ImageGrab.grab(window=windows[0])", source)
        self.assertNotIn("ImageGrab.grab()", source)
