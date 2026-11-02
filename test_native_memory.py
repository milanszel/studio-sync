"""Regression coverage for background native object lifetime boundaries."""

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from native_memory import native_pool


class NativeMemoryTests(unittest.TestCase):
    def test_repeated_calls_drain_every_pool(self):
        state = dict(active=0, drained=0, pending=[])

        class Pool:
            def __enter__(self):
                state["active"] += 1

            def __exit__(self, *error):
                state["pending"].clear()
                state["active"] -= 1
                state["drained"] += 1

        @native_pool
        def operation(fail=False):
            self.assertEqual(state["active"], 1)
            state["pending"].append(bytearray(4096))
            if fail:
                raise ValueError("recognition failed")
            return b"copied result"

        with patch("native_memory.sys.platform", "darwin"), patch.dict(
            sys.modules, objc=types.SimpleNamespace(autorelease_pool=Pool)
        ):
            for i in range(10000):
                if i % 10 == 0:
                    with self.assertRaises(ValueError):
                        operation(True)
                else:
                    self.assertEqual(operation(), b"copied result")
                self.assertEqual(state["pending"], [])
                self.assertEqual(state["active"], 0)
        self.assertEqual(state["drained"], 10000)

    def test_other_platform_has_no_native_dependency(self):
        @native_pool
        def operation():
            return 42

        with patch("native_memory.sys.platform", "win32"), patch.dict(sys.modules, objc=None):
            self.assertEqual(operation(), 42)

    def test_all_repeating_native_entry_points_are_pooled(self):
        expected = {
            "screen_view.py": {"capture_fl", "_capture_mac"},
            "mac_capture.py": {"start", "content_ready", "stop", "stopped"},
        }
        for filename, names in expected.items():
            tree = ast.parse(Path(__file__).with_name(filename).read_text())
            pooled = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and any(isinstance(d, ast.Name) and d.id == "native_pool" for d in node.decorator_list)
            }
            self.assertTrue(names <= pooled, filename)
            if filename == "mac_capture.py":
                callback = next(
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "stream_didOutputSampleBuffer_ofType_"
                )
                self.assertIsInstance(callback.body[0], ast.With)
                self.assertEqual(callback.body[0].items[0].context_expr.func.attr, "autorelease_pool")


if __name__ == "__main__":
    unittest.main()
