from test_transport import bare_bridge
import unittest
from protocol import Authority
import test_loop_sync as loops


class CompensationTests(unittest.TestCase):
    def test_publish_nominal_time_and_wrap(self):
        b = bare_bridge()
        b.audio_delay = 0.1
        p = dict(playing=True, bpm=120, mode=0, loop_beats=32, pos=0.04)
        self.assertAlmostEqual(b.timeline_sample(p)["pos"], 15.94)
        self.assertEqual(b.timeline_sample(dict(p, request=True))["pos"], 0.04)
        self.assertEqual(b.timeline_sample(dict(p, playing=False))["pos"], 0.04)
        self.assertAlmostEqual(b.timeline_sample(dict(p, mode=1, pos=5))["pos"], 4.9)

    def test_host_and_guest_followers_keep_compensated_phase(self):
        for host in (True, False):
            b = loops.LoopTests().bridge()
            b.host = host
            b.audio_delay = 0.1
            b.offset = 0
            b.authority = Authority()
            for i in range(200):
                now = 100 + i * 0.1
                state = dict(
                    playing=True,
                    bpm=120,
                    mode=0,
                    loop_beats=32,
                    pos=((now - 0.2) - 100) % 16,
                    at=now - 0.2,
                    source="other",
                    revision=1,
                )
                b.state = state
                b.authority.state = state
                b.local = dict(state, pos=((now - 0.1) - 100 + 0.1) % 16, applied="1")
                b.last_fl = now - 0.1
                b.last_peer = now
                b.apply_remote(now)
            self.assertEqual(b.commands, [])

    def test_scheduled_start_delay_symmetric(self):
        for host in (True, False):
            b = loops.LoopTests().bridge()
            b.host = host
            b.audio_delay = 0.08
            b.offset = 0
            b.authority = Authority()
            b.applied_rev = 0
            b.state = dict(
                playing=True,
                bpm=120,
                mode=0,
                loop_beats=32,
                pos=0,
                at=100.4,
                source="other",
                revision=1,
                scheduled=True,
            )
            b.authority.state = b.state
            b.local = dict(b.state, playing=False, applied="0")
            b.last_fl = 100
            b.last_peer = 100
            b.apply_remote(100)
            self.assertAlmostEqual(b.commands[-1]["delay_ms"], 320)
            self.assertEqual(b.commands[-1]["pos"], 0)
