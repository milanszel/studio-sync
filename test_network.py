"""Several laptops over a simulated bad network: lag, jitter, lost packets."""

import heapq
import os
import random
import socket
import threading
import time
import unittest
from unittest.mock import patch
import mido
import bridge as bridge_module
from bridge import Bridge
from protocol import MAX_MEMBERS
from test_transport import FakeFL, Resource

CODE = "TEST-ROOM-NETW"


class LossyLink:
    """UDP relay between one joiner and the host with delay, jitter and loss each way."""

    def __init__(self, host_port, delay=0.02, jitter=0.015, loss=0.05, seed=1):
        self.host = ("127.0.0.1", host_port)
        self.delay, self.jitter, self.loss = delay, jitter, loss
        self.random = random.Random(seed)
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.front.bind(("127.0.0.1", 0))
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.back.bind(("127.0.0.1", 0))
        for s in (self.front, self.back):
            s.settimeout(0.002)
        self.port = self.front.getsockname()[1]
        self.client = None
        self.queue = []
        self.lock = threading.Lock()
        self.cut = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def schedule(self, sock, data, addr):
        if self.cut or self.random.random() < self.loss:
            return
        due = time.monotonic() + max(0, self.delay + self.random.uniform(-self.jitter, self.jitter))
        with self.lock:
            heapq.heappush(self.queue, (due, id(data), sock, data, addr))

    def run(self):
        while not self.done.is_set():
            for sock, forward in ((self.front, True), (self.back, False)):
                try:
                    data, addr = sock.recvfrom(65535)
                except (socket.timeout, OSError):
                    continue
                if forward:
                    self.client = addr
                    self.schedule(self.back, data, self.host)
                elif self.client:
                    self.schedule(self.front, data, self.client)
            now = time.monotonic()
            with self.lock:
                while self.queue and self.queue[0][0] <= now:
                    _, _, sock, data, addr = heapq.heappop(self.queue)
                    try:
                        sock.sendto(data, addr)
                    except OSError:
                        pass

    def close(self):
        self.done.set()
        self.thread.join(1)
        self.front.close()
        self.back.close()


class Room:
    """A host plus joiners, each with a simulated FL, joined through lossy links."""

    def __init__(self, joiners, link=None):
        self.fls, self.bridges, self.links = [], [], []
        self.mapping = {}
        for i in range(joiners + 1):
            self.add(link)

    def add(self, link=None):
        mapping = self.mapping

        def open_output(name, **kwargs):
            return Resource(mapping[name].receive)

        def open_input(name, callback, **kwargs):
            mapping[name].callback = callback
            return Resource()

        i = len(self.bridges)
        with patch.object(mido, "open_output", open_output), patch.object(mido, "open_input", open_input):
            fl = FakeFL("net%d_%d" % (id(self), i))
            mapping["to%d" % i] = mapping["from%d" % i] = fl
            self.fls.append(fl)
            if i == 0:
                b = Bridge(True, "", CODE, "to0", "from0", name="Host")
            else:
                port = bridge_module.PORT
                if link is not None:
                    l = LossyLink(bridge_module.PORT, seed=i, **link)
                    self.links.append(l)
                    port = l.port
                b = Bridge(
                    False, "127.0.0.1", CODE, "to%d" % i, "from%d" % i, name="Guest %d" % i, host_port=port
                )
            self.bridges.append(b)
        return fl

    def idle(self):
        for fl in self.fls:
            fl.idle()

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.idle()
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def ready(self):
        host = self.bridges[0]
        now = time.monotonic()
        return len(host.live_members(now)) == len(self.bridges) - 1 and all(
            len(b.clock) >= 3 and b.coordinated(now) for b in self.bridges[1:]
        )

    def close(self):
        for b in reversed(self.bridges):
            b.close()
        for l in self.links:
            l.close()


class NetworkTests(unittest.TestCase):
    def assert_together(self, room, fls, tolerance):
        positions = [fl.position() for fl in fls]
        self.assertLess(max(positions) - min(positions), tolerance, positions)

    @unittest.skipIf(os.environ.get("CI"), "millisecond timing is unreliable on shared build servers")
    def test_four_laptops_start_together_on_a_bad_network(self):
        room = Room(3, link=dict(delay=0.02, jitter=0.015, loss=0.05))
        try:
            self.assertTrue(room.wait(room.ready, 10), "laptops never all connected")
            # Let the clock filter collect a few seconds of samples, as in a real session.
            room.wait(lambda: False, 5)
            room.fls[2].user_toggle()  # A guest presses Play.
            self.assertTrue(room.wait(lambda: all(fl.playing for fl in room.fls), 4))
            starts = [fl.starts[-1][0] for fl in room.fls]
            gap = max(starts) - min(starts)
            print("\nstart gap with 20 ms lag, 15 ms jitter, 5%% loss: %.2f ms" % (gap * 1000))
            self.assertLess(gap, 0.005, "start gap too large")
            room.wait(lambda: False, 1)
            self.assert_together(room, room.fls, 0.03)
        finally:
            room.close()

    def test_leader_leaving_and_late_joiner(self):
        room = Room(2)
        try:
            self.assertTrue(room.wait(room.ready, 8))
            room.fls[1].user_toggle()  # Guest 1 starts and leads.
            self.assertTrue(room.wait(lambda: all(fl.playing for fl in room.fls), 4))
            room.bridges[1].close()  # The leader leaves.
            self.assertFalse(room.fls[1].playing)
            room.wait(lambda: False, 4)
            # Everyone else keeps playing, still together.
            self.assertTrue(room.fls[0].playing and room.fls[2].playing)
            self.assert_together(room, [room.fls[0], room.fls[2]], 0.05)
            # A host tempo change still reaches the remaining guest.
            room.fls[0].bpm = 133
            self.assertTrue(room.wait(lambda: room.fls[2].bpm == 133, 4))
        finally:
            room.close()

    def test_late_joiner_comes_in_at_the_right_place(self):
        room = Room(1)
        try:
            self.assertTrue(room.wait(room.ready, 8))
            room.fls[0].user_toggle()
            self.assertTrue(room.wait(lambda: all(fl.playing for fl in room.fls), 4))
            room.wait(lambda: False, 1.5)
            late = room.add()
            self.assertTrue(room.wait(lambda: late.playing, 6), "late joiner never started")
            room.wait(lambda: False, 1)
            self.assert_together(room, room.fls, 0.03)
        finally:
            room.close()

    def test_wifi_drop_keeps_playing_and_resyncs(self):
        room = Room(1, link=dict(delay=0.005, jitter=0.002, loss=0))
        try:
            self.assertTrue(room.wait(room.ready, 8))
            room.fls[0].user_toggle()
            self.assertTrue(room.wait(lambda: all(fl.playing for fl in room.fls), 4))
            room.links[0].cut = True
            room.wait(lambda: False, 4)
            self.assertTrue(all(fl.playing for fl in room.fls), "a dropout must not stop playback")
            # While cut off, the guest's own edit must not override the room on return.
            serial = room.fls[1].script._serial
            room.fls[1].bpm = 90
            room.wait(lambda: room.fls[1].script._serial > serial, 2)
            room.links[0].cut = False
            self.assertTrue(
                room.wait(lambda: room.fls[1].bpm == 120, 6), "guest did not follow the room again"
            )
            self.assertEqual(room.fls[0].bpm, 120)
        finally:
            room.close()

    def test_host_tempo_reaches_guests_and_refusals_are_reported(self):
        room = Room(2)
        try:
            self.assertTrue(room.wait(room.ready, 8))
            room.fls[0].bpm = 150.0
            self.assertTrue(room.wait(lambda: all(fl.bpm == 150.0 for fl in room.fls), 4))
            # Guest 2's FL ignores tempo commands: everyone is told who and why.
            stubborn = room.fls[2].script
            stubborn.mixer.setCurrentTempo = lambda *a: None
            stubborn.general.processRECEvent = lambda *a: None
            room.fls[0].bpm = 97.0
            self.assertTrue(room.wait(lambda: room.fls[1].bpm == 97.0, 4))

            def reported():
                people = room.bridges[0].people(time.monotonic())
                return any(
                    p["name"] == "Guest 2" and "did not accept tempo 97" in p["problem"] for p in people
                )

            self.assertTrue(room.wait(reported, 4))
            self.assertEqual(room.fls[2].bpm, 150.0)
        finally:
            room.close()

    def test_fast_and_slow_tempos_sync(self):
        # FL allows 10 to 522 BPM; a 407 BPM project used to be rejected.
        room = Room(1)
        try:
            self.assertTrue(room.wait(room.ready, 8))
            for bpm in (407.0, 522.0, 10.0):
                room.fls[0].bpm = bpm
                self.assertTrue(room.wait(lambda: room.fls[1].bpm == bpm, 4), bpm)
                self.assertFalse(room.bridges[0].last_error, room.bridges[0].last_error)
        finally:
            room.close()

    def test_wrong_code_is_refused_and_guessing_is_limited(self):
        room = Room(0)
        try:
            host = room.bridges[0]
            with patch.object(mido, "open_output", lambda name, **k: Resource()), patch.object(
                mido, "open_input", lambda name, callback, **k: Resource()
            ):
                wrong = Bridge(False, "127.0.0.1", "ZZZ-999", "a", "b", name="Stranger")
            try:
                self.assertTrue(room.wait(lambda: wrong.wrong_code, 4))
                self.assertIsNone(wrong.key)
                self.assertFalse(host.live_members(time.monotonic()))
                self.assertIn("Wrong room code", wrong.hint(time.monotonic()))
            finally:
                wrong.close()
            # Someone hammering the host with guesses is cut off after 10 a minute.
            from protocol import pake_join

            attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            import json
            import uuid

            for _ in range(15):
                _, message = pake_join("AAA-AAA")
                hello = dict(kind="hello", attempt=uuid.uuid4().hex, pake=message.hex(), node="x")
                attacker.sendto(json.dumps(hello).encode(), ("127.0.0.1", bridge_module.PORT))
            attacker.close()
            room.wait(lambda: "keeps trying to join" in host.network_error, 2)
            self.assertIn("keeps trying to join", host.network_error)
            self.assertLessEqual(sum(1 for h in host.hellos if h[1] == "127.0.0.1"), 11)
        finally:
            room.close()

    @unittest.skipIf(os.environ.get("CI"), "millisecond timing is unreliable on shared build servers")
    def test_real_world_drift_stays_tight(self):
        """Different buffer sizes, a late start and sound card clocks 200 ppm apart
        (worse than real laptops): the true playheads must stay within ~10 ms."""
        room = Room(1, link=dict(delay=0.004, jitter=0.003, loss=0.01))
        try:
            host_fl, guest_fl = room.fls
            host_fl.buffer, guest_fl.buffer = 256 / 44100, 1024 / 44100
            guest_fl.start_lag, guest_fl.clock = 0.03, 1.0002
            self.assertTrue(room.wait(room.ready, 8))
            room.wait(lambda: False, 3)
            host_fl.user_toggle()
            self.assertTrue(room.wait(lambda: host_fl.playing and guest_fl.playing, 4))
            room.wait(lambda: False, 6)  # Let it settle.
            gaps = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                room.wait(lambda: False, 0.25)
                gaps.append(abs(host_fl.position() - guest_fl.position()))
            guest = room.bridges[1]
            print(
                "\nworst gap %.1f ms, typical %.1f ms, nudges %d, jumps %d"
                % (max(gaps) * 1000, sorted(gaps)[len(gaps) // 2] * 1000, guest.nudges, guest.corrections)
            )
            # What's left is constant: FL reports the playhead in buffer sized steps, so
            # laptops with different buffer sizes sit a few ms apart (Earlier/Later fixes it).
            self.assertLess(sorted(gaps)[len(gaps) // 2], 0.012)
            self.assertLess(max(gaps) - min(gaps), 0.006, "the gap wanders")
            self.assertLessEqual(guest.corrections, 1)
        finally:
            room.close()

    def test_export_is_left_alone(self):
        """While FL exports it stops calling OnIdle. The script must not touch the
        transport and the app must stop flooding it with messages."""
        room = Room(1)
        try:
            host_fl, guest_fl = room.fls
            self.assertTrue(room.wait(room.ready, 8))
            host_fl.user_toggle()
            self.assertTrue(room.wait(lambda: host_fl.playing and guest_fl.playing, 4))
            sent = []
            original = room.bridges[1].output.send
            room.bridges[1].output.send = lambda msg: (sent.append(msg), original(msg))
            guest_fl.busy = True  # Export starts on the guest.
            room.wait(lambda: False, 0.5)
            toggles, targets = guest_fl.toggles, len(guest_fl.target)
            host_fl.bpm = 133.0  # Meanwhile the room changes tempo and jumps.
            host_fl.seek(40000, 0)
            room.wait(lambda: False, 3)
            self.assertEqual(guest_fl.toggles, toggles, "transport touched during export")
            self.assertEqual(len(guest_fl.target), targets, "playhead moved during export")
            self.assertEqual(guest_fl.bpm, 120.0)
            later = len(sent)
            room.wait(lambda: False, 2)
            self.assertLessEqual(len(sent) - later, 3, "app kept flooding a busy FL")
            guest_fl.busy = False  # Export finished: it catches up with the room.
            self.assertTrue(room.wait(lambda: guest_fl.bpm == 133.0, 4))
            self.assertTrue(room.wait(lambda: abs(guest_fl.position() - host_fl.position()) < 0.05, 6))
        finally:
            room.close()

    def test_room_full(self):
        with patch.object(bridge_module, "MAX_MEMBERS", 2):
            room = Room(2)
            try:
                room.wait(lambda: any(b.full for b in room.bridges[1:]), 4)
                self.assertEqual(len(room.bridges[0].members), 1)
                self.assertTrue(any("full" in b.network_error for b in room.bridges[1:]))
            finally:
                room.close()
        self.assertEqual(MAX_MEMBERS, 8)

    def test_replayed_packets_are_ignored(self):
        room = Room(1)
        try:
            self.assertTrue(room.wait(room.ready, 8))
            host, guest = room.bridges
            captured = []
            original = guest.send_to

            def spy(addr, kind, **kw):
                original(addr, kind, **kw)
                captured.append(guest.last_packet)

            guest.last_packet = None
            real_sign = bridge_module.sign

            def capture_sign(key, payload):
                signed = real_sign(key, payload)
                guest.last_packet = signed
                return signed

            with patch.object(bridge_module, "sign", capture_sign):
                guest.send_to = spy
                room.wait(lambda: len(captured) > 5, 2)
            guest.send_to = original
            old = captured[0]
            # Replay from another address after the guest goes quiet.
            guest.close()
            room.wait(lambda: not host.live_members(time.monotonic()), 5)
            attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            import json

            attacker.sendto(json.dumps(old).encode(), ("127.0.0.1", bridge_module.PORT))
            attacker.close()
            room.wait(lambda: False, 0.3)
            self.assertFalse(host.live_members(time.monotonic()))
        finally:
            room.close()


if __name__ == "__main__":
    unittest.main()
