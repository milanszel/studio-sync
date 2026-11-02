from test_transport import bare_bridge
import unittest
import numpy as np
from audio_key import chroma, estimate, AudioKey, NAMES


class AudioTests(unittest.TestCase):
    def test_silence(self):
        self.assertIsNone(chroma(np.zeros(24000), 48000))

    def test_pitch(self):
        t = np.arange(24000) / 48000
        for midi in (48, 57, 60, 69, 76):
            x = 0.1 * np.sin(2 * np.pi * 440 * 2 ** ((midi - 69) / 12) * t)
            self.assertEqual(int(np.argmax(chroma(x, 48000))), midi % 12)

    def test_single_note(self):
        self.assertEqual(estimate([1] + [0] * 11)["strength"], "Uncertain")

    def test_transposition(self):
        p = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
        for root in range(12):
            result = estimate(np.roll(p, root))
            self.assertIn(NAMES[root] + " major", result["key"] + " " + result["alternative"])

    def test_stop(self):
        a = AudioKey()
        a.stop()
        a.stop()
        self.assertFalse(a.state()["listening"])


class NativeFormatTests(unittest.TestCase):
    def test_tuple_format(self):
        from mac_capture import audio_format

        self.assertEqual(
            audio_format((48000.0, 1819304813, 41, 4, 1, 4, 2, 32, 0)), (48000, 1819304813, 41, 2, 32)
        )

    def test_named_format(self):
        from mac_capture import audio_format
        from types import SimpleNamespace

        self.assertEqual(
            audio_format(
                SimpleNamespace(
                    mSampleRate=44100.0,
                    mFormatID=1819304813,
                    mFormatFlags=9,
                    mChannelsPerFrame=2,
                    mBitsPerChannel=32,
                )
            ),
            (44100, 1819304813, 9, 2, 32),
        )


class MusicalTests(unittest.TestCase):
    def test_d_minor_melody_with_harmonics(self):
        t = np.arange(24000) / 48000
        votes = []
        for midi in [62, 65, 69, 70, 69, 67, 65, 64, 62, 57, 62, 65, 69, 62]:
            f = 440 * 2 ** ((midi - 69) / 12)
            x = sum(0.1 / h * np.sin(2 * np.pi * f * h * t) for h in range(1, 6))
            votes.append(chroma(x, 48000))
        result = estimate(np.mean(votes, axis=0))
        self.assertIn("D minor", result["key"] + " " + result["alternative"])
        self.assertNotEqual(result["key"], "A major")

    def test_shared_summary_host(self):
        import time
        import os
        from protocol import sign

        b = bare_bridge(host=True, code="ROOM-CODE-TEST")
        sent = []
        b.send_to = lambda addr, kind, **kw: sent.append((addr, kind, kw))
        ping = dict(
            kind="ping",
            node="guest",
            id="ping",
            name="Studio B",
            ts=time.time(),
            n=1,
            audio_key=dict(listening=True, key="D minor", alternative="F major", strength="Tentative"),
        )
        b.receive(sign(b.key, ping), ("127.0.0.1", 5), 10)
        self.assertEqual(b.members["guest"]["audio_key"]["key"], "D minor")
        self.assertEqual(sent[-1][1], "pong")
        people = b.people(10.5)
        self.assertEqual(people[0]["name"], "Studio B")
        self.assertEqual(people[0]["audio_key"]["key"], "D minor")
        forged = dict(ping, node="x", id="p2", n=2, audio_key=dict(listening=True, key="A major"))
        b.receive(sign(os.urandom(32), forged), ("127.0.0.1", 5), 11)
        self.assertNotIn("x", b.members)
        self.assertEqual(b.members["guest"]["audio_key"]["key"], "D minor")


class RecordedDistributionTests(unittest.TestCase):
    def test_user_d_minor_clip_distribution(self):
        c = [0.016, 0.034, 0.226, 0.032, 0.298, 0.093, 0.049, 0.045, 0.031, 0.136, 0.026, 0.014]
        self.assertEqual(estimate(np.array(c) / sum(c))["key"], "D minor")


class WindowsCaptureTests(unittest.TestCase):
    def test_loopback_and_cleanup(self):
        import sys, threading
        from unittest.mock import patch
        from types import SimpleNamespace
        from windows_capture import WindowsCapture

        seen = []
        errors = []
        closed = []
        ready = threading.Event()

        class Stream:
            def read(self, *a, **kw):
                return np.ones((1024, 2), dtype=np.float32).tobytes()

            def stop_stream(self):
                closed.append("stop")

            def close(self):
                closed.append("close")

        class Audio:
            def get_default_wasapi_loopback(self):
                return dict(defaultSampleRate=48000, maxInputChannels=2, isLoopbackDevice=True, index=7)

            def open(self, **kw):
                seen.append(kw)
                return Stream()

            def terminate(self):
                closed.append("terminate")

        capture = WindowsCapture(lambda data, rate: (capture.done.set(), ready.set()), errors.append)
        with patch.dict(sys.modules, pyaudiowpatch=SimpleNamespace(PyAudio=Audio, paFloat32=1)):
            capture.start()
            self.assertTrue(ready.wait(2))
            capture.stop()
        self.assertEqual(errors, [])
        self.assertEqual(seen[0]["input_device_index"], 7)
        self.assertEqual(closed, ["stop", "close", "terminate"])

    def test_missing_output_reports_error(self):
        import sys, threading
        from unittest.mock import patch
        from types import SimpleNamespace
        from windows_capture import WindowsCapture

        errors = []
        ready = threading.Event()

        class Audio:
            def get_default_wasapi_loopback(self):
                raise RuntimeError("No output")

            def terminate(self):
                pass

        capture = WindowsCapture(lambda *args: None, lambda msg: (errors.append(msg), ready.set()))
        with patch.dict(sys.modules, pyaudiowpatch=SimpleNamespace(PyAudio=Audio, paFloat32=1)):
            capture.start()
            self.assertTrue(ready.wait(2))
            capture.stop()
        self.assertIn("No output", errors[0])
