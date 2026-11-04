import math
import unittest
from protocol import position, phase_distance, valid
from test_transport import FakeFL, bare_bridge
import mido
from protocol import encode


class LoopTests(unittest.TestCase):
    def state(self, pos=0, at=0):
        return dict(playing=True, bpm=120, mode=0, loop_beats=32, pos=pos, at=at, source="leader", revision=1)

    def bridge(self):
        b = bare_bridge()
        b.host = False
        b.node = "follower"
        b.pending_serial = None
        b.peer_fl_ready = True
        b.offset = 0
        b.applied_rev = 1
        b.last_apply = 0
        b.last_correction = 0
        b.drift_since = None
        b.commands = []
        b.midi = b.commands.append
        return b

    def test_eight_bar_projection_wraps_and_song_does_not(self):
        s = self.state(15.95, 10)
        self.assertAlmostEqual(position(s, 10.1), 0.05)
        self.assertAlmostEqual(position(s, 10.1 + 16 * 20), 0.05)
        self.assertAlmostEqual(position(dict(s, mode=1), 10.1), 16.05)
        self.assertAlmostEqual(phase_distance(15.99, 0.01, 16), 0.02)
        self.assertTrue(valid(s))
        for beats in [-1, math.inf, math.nan, "32", True]:
            self.assertFalse(valid(dict(s, loop_beats=beats)))

    def test_twenty_eight_bar_loops_never_cause_false_seeks(self):
        b = self.bridge()
        for i in range(3213):
            now = 100 + i * 0.1
            b.state = self.state(((now - 0.2) - 100) % 16, now - 0.2)
            b.local = dict(self.state(((now - 0.1) - 100 - 0.02) % 16, now - 0.1), applied="1")
            b.last_fl = now - 0.1
            b.last_peer = now
            b.apply_remote(now)
        self.assertEqual(b.commands, [])

    def test_accumulating_drift_corrects_across_repeats(self):
        """An 8 bar loop where this laptop keeps slipping 35 ms behind every 16 s.
        Small slips are nudged back smoothly across the loop point, never jumped."""
        from test_transport import following_bridge, step

        b = following_bridge()
        commands = []
        b.midi = commands.append
        behind, worst = 0.02, 0
        nudge_end, nudge_rate = 0, 0
        for i in range(1600):
            now = 100 + i * 0.05
            if i and i % 320 == 0:
                behind += 0.035
            if now < nudge_end:
                behind -= nudge_rate * 0.05  # The nudge plays faster or slower meanwhile.
            room = (now - 100) % 16
            before = len(commands)
            step(b, now, local=(room - behind) % 16, room=room, mode=0, loop_beats=32)
            for c in commands[before:]:
                self.assertEqual(c["kind"], "nudge")
                nudge_rate, nudge_end = c["speed"] - 1, now + c["ms"] / 1000
            if i > 320:
                worst = max(worst, abs(behind))
        self.assertTrue(commands)
        self.assertLess(abs(behind), 0.012)
        self.assertLess(worst, 0.06)

    def test_fl_reports_wrap_immediately_without_new_edit_serial(self):
        f = FakeFL("loop")
        f.mode = 0
        f.loop_beats = 32
        out = []
        f.callback = lambda msg: out.append(msg)
        f.receive(mido.Message("sysex", data=encode(dict(kind="ping"))))
        f.playing = True
        f.pos = 15.99
        f.script._last = dict(f.script.snapshot())
        f.pos = 0.01
        f.script._sent = __import__("time").monotonic()
        f.idle()
        self.assertEqual(f.script._serial, 0)
        self.assertEqual(f.script._last["loop_beats"], 32)
        self.assertEqual(len(out), 2)
