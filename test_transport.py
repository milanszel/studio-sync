import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch
import mido
from protocol import Authority, encode, decode, position
from bridge import Bridge


def bare_bridge(host=False, code="TEST-ROOM-CODE"):
    """A Bridge without sockets or threads, for unit tests of its logic."""
    b = Bridge.__new__(Bridge)
    b.setup_state(host, code)
    if not host:
        b.host_addr = ("127.0.0.1", 42611)
    return b


def following_bridge():
    """A guest that follows another laptop, without sockets or threads."""
    b = bare_bridge()
    b.node = "follower"
    b.applied_rev = 1
    b.last_correction = 0
    return b


def step(b, now, local, room, bpm=120, mode=1, loop_beats=0, bar_beats=4):
    """One tick: the leader reported `room` and our FL reported `local`, both just now."""
    b.state = dict(
        playing=True, bpm=bpm, mode=mode, loop_beats=loop_beats, pos=room, at=now, source="leader", revision=1
    )
    b.local_status(
        dict(
            kind="status",
            playing=True,
            bpm=bpm,
            mode=mode,
            loop_beats=loop_beats,
            bar_beats=bar_beats,
            pos=local,
            serial=0,
            applied="1",
            protocol=4,
        ),
        now,
    )
    b.last_peer = now
    b.apply_remote(now)


class FakeFL:
    def __init__(self, name):
        self.name = name
        self.playing = False
        # Like FL, the playhead is a count of beats; its time in ms depends on
        # the current tempo.
        self.beats = 0.0
        self._bpm = 120.0
        self.mode = 1
        self.at = time.monotonic()
        self.callback = None
        self.toggles = 0
        self.starts = []
        self.target = []
        # Real FL behaviour, off by default: the playhead is reported in audio
        # buffer steps, playback starts a little after Play, and every sound
        # card clock runs very slightly fast or slow.
        self.buffer = 0.0
        self.start_lag = 0.0
        self.clock = 1.0
        self.busy = False

        def emit(raw):
            if self.callback:
                self.callback(mido.Message("sysex", data=raw[1:-1]))

        modules = {
            "device": types.SimpleNamespace(isAssigned=lambda: True, midiOutSysex=emit),
            "general": types.SimpleNamespace(
                getRecPPB=lambda: 384,
                getRecPPQ=lambda: 96,
                getVersion=lambda: 36,
                processRECEvent=self.rec_event,
            ),
            "patterns": types.SimpleNamespace(
                patternNumber=lambda: 1, getPatternLength=lambda index: getattr(self, "loop_beats", 0)
            ),
            "midi": types.SimpleNamespace(
                SONGLENGTH_S=1,
                SONGLENGTH_MS=0,
                FPT_Play=10,
                PME_System=2,
                REC_Tempo=93,
                REC_Control=1,
                REC_UpdateControl=2,
            ),
            "mixer": types.SimpleNamespace(
                getCurrentTempo=lambda asInt=0: self.bpm if asInt else self.bpm * 1000,
                setCurrentTempo=self.tempo,
            ),
            "transport": types.SimpleNamespace(
                isPlaying=lambda: self.playing,
                getSongPos=lambda mode: self.reported() * 1000 if mode == 0 else int(self.reported()),
                getLoopMode=lambda: self.mode,
                setLoopMode=self.mode_toggle,
                setSongPos=self.seek,
                globalTransport=lambda *args: self.toggle(),
                stop=self.stop,
            ),
        }
        spec = importlib.util.spec_from_file_location(
            "script_" + name, Path(__file__).parent / "FL-Script" / "Studio-Sync" / "device_studio_sync.py"
        )
        self.script = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.script)
        self.script.OnInit()

    def advance(self):
        now = time.monotonic()
        if self.playing and now > self.at:
            self.beats += (now - self.at) * self._bpm / 60.0 * self.clock
        self.at = max(self.at, now)

    @property
    def bpm(self):
        return self._bpm

    @bpm.setter
    def bpm(self, value):
        self.advance()
        self._bpm = value

    def song_bpm(self):
        # The project tempo, ignoring a nudge in progress.
        nudge = self.script._nudge
        return nudge["base"] if nudge else self._bpm

    @property
    def pos(self):
        self.advance()
        return self.beats * 60.0 / self.song_bpm()

    @pos.setter
    def pos(self, seconds):
        self.advance()
        self.beats = seconds * self.song_bpm() / 60.0

    def position(self):
        return self.pos

    def reported(self):
        # What FL reports: ms at the tempo it is playing right now.
        self.advance()
        pos = self.beats * 60.0 / self._bpm
        if self.buffer and self.playing:
            pos = (pos // self.buffer) * self.buffer
        return pos

    def rec_event(self, index, value, flags):
        if index == 93:
            self.bpm = value / 1000

    def tempo(self, bpm, asInt=0):
        self.bpm = bpm if asInt else bpm / 1000

    def mode_toggle(self):
        self.mode = 1 - self.mode

    def seek(self, pos, mode):
        self.advance()
        seconds = pos / 1000 if mode == 0 else pos
        self.beats = seconds * self._bpm / 60.0
        self.target.append(self.pos)

    def toggle(self):
        self.position()
        self.playing = not self.playing
        self.toggles += 1
        if self.playing:
            self.at = time.monotonic() + self.start_lag
            self.starts.append((self.at, self.pos))

    def stop(self):
        self.position()
        self.playing = False
        self.pos = 0

    def user_toggle(self):
        self.toggle()
        self.script.OnRefresh(0)

    def receive(self, msg):
        event = types.SimpleNamespace(sysex=bytes(msg.bytes()), handled=False, pmeflags=2)
        self.script.OnMidiIn(event)
        if not event.handled:
            self.script.OnSysEx(event)

    def idle(self):
        if not self.busy:  # FL doesn't call OnIdle while exporting.
            self.script.OnIdle()


class Resource:
    def __init__(self, send=lambda msg: None):
        self.send = send

    def close(self):
        pass


class Tests(unittest.TestCase):
    def test_host_start_wait_is_configurable(self):
        for delay in (0.1, 0.2, 0.4, 1):
            b = bare_bridge()
            b.authority = Authority()
            b.start_wait = delay
            stopped = dict(playing=False, bpm=140, mode=1, pos=0)
            b.accept_sample("source", 0, 1, stopped, 10, 10)
            b.accept_sample("source", 1, 2, dict(stopped, playing=True), 11, 11, True)
            self.assertAlmostEqual(b.authority.state["at"], 11 + delay)
            b.accept_sample("source", 1, 3, dict(stopped, playing=True), 11.01, 11.01, True)
            self.assertAlmostEqual(b.authority.state["at"], 11 + delay)

    def test_host_discovery_finds_ip_without_code(self):
        from discovery import HostDiscovery, find_hosts

        server = HostDiscovery()
        try:
            found = find_hosts(0.25)
            self.assertTrue(found)
            self.assertTrue(all("code" not in h for h in found))
        finally:
            server.close()

    def test_packets_are_signed_and_never_carry_the_code(self):
        import os
        from protocol import sign, verify

        key = os.urandom(32)
        packet = sign(key, dict(kind="ping", node="a", id="1", stamp=1.25))
        wire = json.dumps(packet)
        self.assertNotIn("secretroom", wire)
        self.assertTrue(verify(key, json.loads(wire)))
        tampered = json.loads(wire)
        tampered["stamp"] = 9
        self.assertFalse(verify(key, tampered))
        self.assertFalse(verify(os.urandom(32), json.loads(wire)))

    def test_replay_guard_allows_reordering_but_not_repeats(self):
        from protocol import ReplayGuard

        g = ReplayGuard()
        now = 1000.0
        self.assertTrue(g.check("a", 5, now, now))
        self.assertTrue(g.check("a", 7, now, now))
        self.assertTrue(g.check("a", 6, now, now))  # Arrived late, still new.
        self.assertFalse(g.check("a", 6, now, now))  # Same packet again.
        self.assertFalse(g.check("a", 8, now - 60, now))  # Too old.
        self.assertIsNotNone(g.clock_error)
        self.assertTrue(g.check("a", 5000, now, now))
        self.assertFalse(g.check("a", 7, now, now))  # Far behind the window.

    def test_fl_bar_seek_moves_without_stopping(self):
        f = FakeFL("seek")
        f.callback = lambda msg: None
        f.receive(mido.Message("sysex", data=encode({"kind": "ping"})))
        f.playing = True
        f.pos = 10.0
        toggles = f.toggles
        f.receive(
            mido.Message(
                "sysex",
                data=encode(
                    dict(
                        kind="apply",
                        id="4",
                        playing=True,
                        bpm=120,
                        mode=1,
                        pos=12.0,
                        seek=True,
                        force=True,
                        delay_ms=900,
                    )
                ),
            )
        )
        self.assertTrue(f.playing)
        self.assertTrue(f.script._pending["seek"])
        f.receive(mido.Message("sysex", data=encode(dict(kind="fire", id="4"))))
        self.assertTrue(f.playing)
        self.assertEqual(f.toggles, toggles)  # Never paused.
        self.assertAlmostEqual(f.pos, 12.0, delta=0.01)
        self.assertEqual(f.script.snapshot()["bar_beats"], 4.0)

    def test_fl_keeps_playing_when_app_disappears(self):
        f = FakeFL("orphan")
        f.callback = lambda msg: None
        f.receive(mido.Message("sysex", data=encode({"kind": "ping"})))
        f.playing = True
        f.pos = 30.0
        f.script._last_ping -= 10
        f.idle()
        self.assertTrue(f.playing)
        self.assertGreater(f.position(), 29)
        self.assertFalse(f.script._enabled)

    def test_drift_is_corrected_on_the_next_bar_line(self):
        b = following_bridge()
        commands = []
        b.midi = commands.append
        fired = []
        b.schedule_fire = lambda ident, due: fired.append(due)
        sent_at = None
        for i in range(30):
            now = 10 + i * 0.05
            before = len(commands)
            step(b, now, local=5.3 + i * 0.05, room=5.0 + i * 0.05, bpm=120, bar_beats=4)
            if len(commands) > before and sent_at is None:
                sent_at = now
        self.assertEqual(len(commands), 1)
        c = commands[0]
        self.assertTrue(c["seek"])
        # 120 BPM in 4/4: bars are 2 s. Jump on the next bar line of our own playhead.
        here = 5.3 + (sent_at - 10)
        wait = 2 - here % 2
        self.assertAlmostEqual(c["delay_ms"], wait * 1000, delta=2)
        self.assertAlmostEqual(c["pos"], 5.0 + (sent_at - 10) + wait, delta=0.002)
        self.assertEqual(b.corrections, 1)
        self.assertEqual(len(fired), 1)

    def test_small_drift_is_nudged_not_jumped(self):
        b = following_bridge()
        commands = []
        b.midi = commands.append
        for i in range(40):
            now = 10 + i * 0.05
            step(b, now, local=5.0 + i * 0.05, room=5.03 + i * 0.05, bpm=120)
        self.assertEqual(len(commands), 1)
        c = commands[0]
        self.assertEqual(c["kind"], "nudge")
        self.assertGreater(c["speed"], 1)  # Behind, so play slightly faster.
        self.assertLessEqual(c["speed"], 1.008)
        # Speed times length makes up the 30 ms.
        self.assertAlmostEqual((c["speed"] - 1) * c["ms"] / 1000, 0.03, delta=0.004)
        self.assertEqual(b.nudges, 1)

    def test_in_time_does_nothing(self):
        b = following_bridge()
        commands = []
        b.midi = commands.append
        import random

        rnd = random.Random(3)
        buffer = 1024 / 44100
        for i in range(200):
            now = 10 + i * 0.05 + rnd.uniform(0, 0.02)
            # Both FLs report in buffer steps; the leader is truly 4 ms ahead.
            mine = 5.0 + (now - 10)
            theirs = 5.004 + (now - 10)
            step(b, now, local=(mine // buffer) * buffer, room=(theirs // buffer) * buffer, bpm=120)
        self.assertEqual(commands, [])
        self.assertLess(abs(b.phase_error), 0.02)

    def test_late_scheduled_start_catches_up_by_audio_offset(self):
        b = bare_bridge()
        b.audio_delay = 0.25
        b.local = dict(playing=False, bpm=120, mode=1, pos=0, applied="0")
        b.state = dict(
            playing=True, bpm=120, mode=1, pos=0, source="other", revision=1, at=100.1, scheduled=True
        )
        b.last_fl = b.last_peer = 100
        commands = []
        b.midi = commands.append
        b.apply_remote(100)
        # Starting 0.25 s early would need a start in the past: start now, 0.15 s in.
        self.assertEqual(commands[-1]["delay_ms"], 0)
        self.assertAlmostEqual(commands[-1]["pos"], 0.15)

    def test_unacknowledged_edit_stops_blocking(self):
        b = bare_bridge()
        b.pending_serial = 3
        b.pending_since = 0
        b.local = dict(playing=False, bpm=120, mode=1, pos=4, applied="")
        b.state = dict(playing=False, bpm=120, mode=1, pos=8, source="other", revision=2, at=2.9)
        b.last_fl = b.last_peer = 3
        commands = []
        b.midi = commands.append
        b.apply_remote(1)
        self.assertEqual(commands, [])
        b.apply_remote(3)
        self.assertIsNone(b.pending_serial)
        self.assertEqual(commands[-1]["pos"], 8)

    def nudge_fl(self, name):
        f, out = self.tempo_fl(name)
        f.bpm = 140.0
        f.toggle()
        f.idle()
        f.script.OnIdle()
        return f, out

    def send_nudge(self, f, speed, ms):
        f.receive(mido.Message("sysex", data=encode(dict(kind="nudge", id="n", speed=speed, ms=ms))))

    def test_nudge_moves_on_the_bar_without_touching_tempo(self):
        # Earlier/Later and drift fixes used to change FL's BPM for everyone.
        f, out = self.nudge_fl("nudge1")
        f.script.OnRefresh(0)
        serial, before = f.script._serial, len(out)
        f.pos = 0.5  # Bar is 4 beats at 140 BPM: 1.714 s.
        self.send_nudge(f, 1.008, 2500)  # Ask for 20 ms ahead.
        self.assertEqual(f.bpm, 140.0)
        start, began = f.pos, time.monotonic()
        while time.monotonic() - began < 1.5:
            time.sleep(0.005)
            f.script.OnIdle()
            f.script.OnRefresh(0)
            self.assertEqual(f.bpm, 140.0)
        moved = f.pos - start - (time.monotonic() - began)
        self.assertAlmostEqual(moved, 0.020, delta=0.004)
        self.assertGreater(f.target[-1], 1.714)  # The move happened on the bar line.
        self.assertEqual(f.script._serial, serial, "a nudge must not count as an edit")
        self.assertTrue(all(o.get("bpm") == 140.0 for o in out[before:] if o.get("kind") == "status"))

    def test_nudge_backwards(self):
        f, out = self.nudge_fl("nudge2")
        f.pos = 1.70
        self.send_nudge(f, 0.992, 2500)  # 20 ms back; too close to this bar, so the next one.
        start, began = f.pos, time.monotonic()
        while time.monotonic() - began < 2.0:
            time.sleep(0.005)
            f.script.OnIdle()
        moved = f.pos - start - (time.monotonic() - began)
        self.assertAlmostEqual(moved, -0.020, delta=0.004)

    def tempo_fl(self, name):
        f = FakeFL(name)
        out = []
        f.callback = lambda msg: out.append(decode(msg.data))
        f.receive(mido.Message("sysex", data=encode({"kind": "ping"})))
        return f, out

    def apply_tempo(self, f, bpm, ident="t1"):
        f.receive(
            mido.Message(
                "sysex", data=encode(dict(kind="apply", id=ident, playing=False, bpm=bpm, mode=1, pos=0))
            )
        )

    def test_tempo_falls_back_when_fl_has_no_set_tempo(self):
        f, out = self.tempo_fl("oldfl")

        def missing(*a):
            raise AttributeError("module 'mixer' has no attribute 'setCurrentTempo'")

        f.script.mixer.setCurrentTempo = missing
        self.apply_tempo(f, 150)
        self.assertEqual(f.bpm, 150)
        serial = f.script._serial
        f.script._tempo_target = (150, time.monotonic() - 1)
        f.idle()
        self.assertEqual(f.script._serial, serial)
        self.assertFalse(any(p.get("kind") == "error" for p in out))

    def test_tempo_refused_by_fl_is_reported(self):
        f, out = self.tempo_fl("stubborn")
        f.script.mixer.setCurrentTempo = lambda *a: None
        f.script.general.processRECEvent = lambda *a: None
        self.apply_tempo(f, 150)
        self.assertEqual(f.bpm, 120)
        f.script._tempo_target = (150, time.monotonic() - 1)
        f.idle()
        errors = [p for p in out if p.get("kind") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("did not accept tempo 150", errors[0]["message"])
        f.idle()
        self.assertEqual(len([p for p in out if p.get("kind") == "error"]), 1)

    def test_late_tempo_update_is_not_a_local_edit(self):
        f, out = self.tempo_fl("lazy")
        later = []
        f.script.mixer.setCurrentTempo = lambda bpm, flag=0: later.append(bpm)
        f.script.general.processRECEvent = lambda *a: None
        self.apply_tempo(f, 150)
        serial = f.script._serial
        f.bpm = later[-1]  # FL applies it on its next cycle.
        f.idle()
        self.assertEqual(f.script._serial, serial, "FL applying the app's tempo must not count as an edit")
        f.script.observe(True)
        self.assertEqual(out[-1]["bpm"], 150)
        self.assertEqual(out[-1]["api"], 36)

    def test_old_app_packets_show_update_hint(self):
        import os

        b = bare_bridge()
        b.key = os.urandom(32)
        b.receive(dict(kind="ping", code="room", node="x", id="1"), ("127.0.0.1", 1), 0)
        self.assertIn("older Studio Sync", b.network_error)

    def test_network_errors_do_not_stop_the_bridge(self):
        output = Resource()
        input_port = Resource()
        b = Bridge(False, "127.0.0.1", "netroom", "to", "from", shared_ports=(output, input_port))
        try:

            class Broken:
                def __init__(self, real):
                    self.real = real

                def recvfrom(self, n):
                    raise ConnectionResetError(10054, "reset by peer")

                def sendto(self, data, addr):
                    raise OSError(51, "Network is unreachable")

                def fileno(self):
                    return self.real.fileno()

                def close(self):
                    self.real.close()

            b.sock = Broken(b.sock)
            time.sleep(0.3)
            self.assertTrue(b.thread.is_alive())
            self.assertIn("Network", b.network_error)
        finally:
            b.close()

    def test_fire_is_sent_at_due_time(self):
        output = Resource()
        input_port = Resource()
        b = Bridge(False, "127.0.0.1", "fireroom", "to", "from", shared_ports=(output, input_port))
        sent = []
        b.midi = lambda payload: sent.append((time.monotonic(), payload))
        try:
            due = time.monotonic() + 0.15
            b.schedule_fire("7", due)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not any(p["kind"] == "fire" for _, p in sent):
                time.sleep(0.005)
            fires = [(t, p) for t, p in sent if p["kind"] == "fire"]
            self.assertEqual(len(fires), 1)
            self.assertEqual(fires[0][1]["id"], "7")
            self.assertGreaterEqual(fires[0][0], due)
            self.assertLess(fires[0][0] - due, 0.01)
        finally:
            b.close()

    def test_fl_fire_starts_pending_immediately(self):
        f = FakeFL("fire")
        f.callback = lambda msg: None
        f.receive(mido.Message("sysex", data=encode({"kind": "ping"})))
        f.receive(
            mido.Message(
                "sysex",
                data=encode(
                    dict(
                        kind="apply", id="9", playing=True, bpm=120, pos=1, mode=1, force=True, delay_ms=1500
                    )
                ),
            )
        )
        self.assertIsNotNone(f.script._pending)
        self.assertFalse(f.playing)
        f.receive(mido.Message("sysex", data=encode(dict(kind="fire", id="old"))))
        self.assertFalse(f.playing)
        f.receive(mido.Message("sysex", data=encode(dict(kind="fire", id="9"))))
        self.assertTrue(f.playing)
        self.assertIsNone(f.script._pending)

    def test_shared_start_from_either_fl_keeps_opening(self):
        host_fl, peer_fl = FakeFL("shared_host"), FakeFL("shared_join")
        mapping = {"host to": host_fl, "host from": host_fl, "peer to": peer_fl, "peer from": peer_fl}

        def open_output(name, **kwargs):
            return Resource(mapping[name].receive)

        def open_input(name, callback, **kwargs):
            mapping[name].callback = callback
            return Resource()

        with patch.object(mido, "open_output", open_output), patch.object(mido, "open_input", open_input):
            host = Bridge(True, "", "testroom", "host to", "host from")
            peer = Bridge(False, "127.0.0.1", "testroom", "peer to", "peer from")
            fls = [host_fl, peer_fl]
            try:
                self.wait(lambda: host_fl.script._coordinated and peer_fl.script._coordinated, fls)
                self.wait(lambda: peer.node in host.authority.seen, fls)
                for initiator in (host_fl, peer_fl):
                    initiator.user_toggle()
                    self.assertFalse(initiator.playing, "Source should wait for the shared start")
                    self.wait(lambda: host_fl.playing and peer_fl.playing, fls)
                    self.assertLess(abs(host_fl.starts[-1][0] - peer_fl.starts[-1][0]), 0.06)
                    self.assertLess(host_fl.starts[-1][1], 0.06)
                    self.assertLess(peer_fl.starts[-1][1], 0.06)
                    host_fl.stop()
                    host_fl.idle()
                    self.wait(lambda: not peer_fl.playing and peer_fl.position() < 0.01, fls)
                    self.wait(
                        lambda: host.local
                        and not host.local["playing"]
                        and peer.local
                        and not peer.local["playing"],
                        fls,
                    )
            finally:
                peer.close()
                host.close()

    def test_prepared_start_is_cancelled_on_disconnect(self):
        f = FakeFL("cancel")
        f.callback = lambda msg: None
        p = dict(kind="apply", id="start", playing=True, bpm=140, pos=0, mode=1, force=True, delay_ms=400)
        f.receive(mido.Message("sysex", data=encode(p)))
        self.assertIsNotNone(f.script._pending)
        f.receive(mido.Message("sysex", data=encode(dict(p, id="disconnect", playing=False, delay_ms=0))))
        self.assertIsNone(f.script._pending)
        self.assertFalse(f.playing)

    def test_position_preserves_subsecond_precision(self):
        f = FakeFL("milliseconds")
        f.pos = 3.456
        self.assertAlmostEqual(f.script.snapshot()["pos"], 3.456)

    def test_drift_requires_persistence_before_seeking(self):
        b = following_bridge()
        commands = []
        b.midi = commands.append
        times = []
        for i in range(30):
            now = 10 + i * 0.05
            before = len(commands)
            step(b, now, local=5 + i * 0.05, room=5.3 + i * 0.05, bpm=140)
            if len(commands) > before:
                times.append(now)
        # Nothing for the first 0.4 s of a clear error, then one jump, then no chasing.
        self.assertEqual(len(times), 1)
        self.assertGreaterEqual(times[0], 10.5)
        self.assertTrue(commands[0]["seek"])

    def test_play_pause_without_event_pmeflags(self):
        f = FakeFL("no_flags")
        responses = []
        f.callback = lambda msg: responses.append(decode(msg.data))
        for ident, playing in [("play", True), ("pause", False)]:
            packet = dict(kind="apply", id=ident, playing=playing, bpm=140, mode=1, pos=5, force=True)
            event = types.SimpleNamespace(
                sysex=bytes(mido.Message("sysex", data=encode(packet)).bytes()), handled=False
            )
            f.script.OnMidiIn(event)
            self.assertEqual(f.playing, playing)
            self.assertEqual(responses[-1]["kind"], "status")
            self.assertEqual(responses[-1]["applied"], ident)
        self.assertAlmostEqual(f.pos, 5)
        self.assertEqual(f.script._serial, 0)

    def test_fl_explicit_tempo_mode_is_valid_bpm(self):
        f = FakeFL("scaled")
        f.bpm = 140
        self.assertEqual(f.script.mixer.getCurrentTempo(), 140000)
        self.assertEqual(f.script.snapshot()["bpm"], 140.0)
        f.bpm = 142.3
        self.assertEqual(f.script.snapshot()["bpm"], 142.3)

    def test_shared_midi_ports_survive_disconnect(self):
        output = Resource()
        input_port = Resource()
        with patch.object(output, "close") as output_close, patch.object(input_port, "close") as input_close:
            b = Bridge(False, "127.0.0.1", "diagnostic", "to", "from", shared_ports=(output, input_port))
            self.assertEqual(input_port.callback, b.on_midi)
            b.close()
            self.assertIsNone(input_port.callback)
            input_close.assert_not_called()
            output_close.assert_not_called()
            second = Bridge(False, "127.0.0.1", "diagnostic", "to", "from", shared_ports=(output, input_port))
            self.assertEqual(input_port.callback, second.on_midi)
            second.close()
            input_close.assert_not_called()
            output_close.assert_not_called()

    def test_received_invalid_status_is_visible(self):
        b = bare_bridge()
        b.rx_count = 0
        b.last_rx = ""
        b.last_error = ""
        import queue

        b.local_messages = queue.Queue()
        b.on_midi(mido.Message("sysex", data=encode({"kind": "status", "bpm": 999})))
        self.assertEqual(b.rx_count, 1)
        p, arrived = b.local_messages.get_nowait()
        b.local_status(p, arrived)
        self.assertIn("response rejected", b.last_error)

    def wait(self, predicate, fls, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for fl in fls:
                fl.idle()
            if predicate():
                return
            time.sleep(0.015)
        self.assertTrue(predicate(), "Timed out waiting for transport sync")

    def test_sysex_and_position(self):
        p = {"kind": "status", "playing": False, "pos": 3.5, "bpm": 120, "mode": 1}
        wire = encode(p)
        self.assertTrue(all(b < 128 for b in wire))
        self.assertEqual(decode(wire), p)
        self.assertIsNone(decode(b"other"))
        self.assertEqual(position(dict(p, at=10), 20), 3.5)
        self.assertEqual(position(dict(p, playing=True, at=110), 11, 100), 4.5)

    def test_authority_orders_edits_and_ignores_old_packets(self):
        a = Authority()
        s = dict(playing=False, pos=0, bpm=120, mode=1)
        a.update("host", 0, 1, s, 0)
        a.update("peer", 0, 1, s, 0)
        self.assertEqual(a.state["source"], "host")
        a.update("peer", 1, 2, dict(s, playing=True, pos=5), 1)
        rev = a.revision
        self.assertEqual(a.state["source"], "peer")
        a.update("peer", 0, 1, s, 0)
        self.assertEqual(a.revision, rev)
        self.assertTrue(a.state["playing"])
        a.update("host", 1, 2, s, 2)
        self.assertEqual(a.state["source"], "host")

    def test_pause_resume_echo_and_foreign_sysex(self):
        f = FakeFL("single")
        outgoing = []
        f.callback = lambda msg: outgoing.append(decode(msg.data))
        ping = mido.Message("sysex", data=encode({"kind": "ping"}))
        f.receive(ping)
        f.user_toggle()
        f.idle()
        self.assertEqual(f.script._serial, 1)
        f.seek(5, 1)
        payload = {
            "kind": "apply",
            "id": "1",
            "playing": False,
            "bpm": 135,
            "mode": 1,
            "pos": 5,
            "force": True,
        }
        f.receive(mido.Message("sysex", data=encode(payload)))
        self.assertFalse(f.playing)
        self.assertAlmostEqual(f.position(), 5)
        self.assertEqual(f.bpm, 135)
        self.assertEqual(f.script._serial, 1)  # remote action never becomes a local edit
        toggles = f.toggles
        f.receive(mido.Message("sysex", data=encode(payload)))
        self.assertEqual(f.toggles, toggles)  # duplicate pause must not resume
        f.user_toggle()
        self.assertEqual(f.script._serial, 2)
        f.receive(mido.Message("sysex", data=[1, 2, 3]))
        self.assertEqual(f.script._serial, 2)

    def test_tempo_edit_is_reported_before_stale_apply(self):
        f = FakeFL("tempo")
        outgoing = []
        f.callback = lambda msg: outgoing.append(decode(msg.data))
        f.receive(mido.Message("sysex", data=encode({"kind": "ping"})))
        f.bpm = 155
        f.receive(
            mido.Message(
                "sysex", data=encode(dict(kind="apply", id="old", playing=False, bpm=120, mode=1, pos=0))
            )
        )
        self.assertEqual(f.bpm, 155)
        self.assertEqual(f.script._serial, 1)
        self.assertEqual(outgoing[-1]["bpm"], 155)

    def test_empty_sysex_and_raw_callback(self):
        f = FakeFL("empty")
        outgoing = []
        f.callback = lambda msg: outgoing.append(decode(msg.data))
        for payload in (None, b"", [], 123):
            event = types.SimpleNamespace(sysex=payload, handled=False, pmeflags=2)
            f.script.OnSysEx(event)
            f.script.OnMidiIn(event)
            self.assertFalse(event.handled)
        event = types.SimpleNamespace(
            sysex=bytes(mido.Message("sysex", data=encode({"kind": "ping"})).bytes()),
            handled=False,
            pmeflags=2,
        )
        f.script.OnMidiIn(event)
        self.assertTrue(event.handled)
        self.assertEqual(len(outgoing), 1)
        event.sysex = None  # reproduce FL's later callback without raw payload
        f.script.OnSysEx(event)
        self.assertEqual(len(outgoing), 1)

    def test_real_lan_with_simulated_fl_both_directions(self):
        host_fl, peer_fl = FakeFL("host"), FakeFL("peer")
        mapping = {"host to": host_fl, "host from": host_fl, "peer to": peer_fl, "peer from": peer_fl}

        def open_output(name, **kwargs):
            return Resource(mapping[name].receive)

        def open_input(name, callback, **kwargs):
            mapping[name].callback = callback
            return Resource()

        with patch.object(mido, "open_output", open_output), patch.object(mido, "open_input", open_input):
            host = Bridge(True, "", "testroom", "host to", "host from")
            peer = Bridge(False, "127.0.0.1", "testroom", "peer to", "peer from")
            fls = [host_fl, peer_fl]
            try:
                self.wait(
                    lambda: host.members
                    and peer.members
                    and len(peer.clock) >= 3
                    and host.coordinated(time.monotonic()),
                    fls,
                )
                # Establish peer initial serial before its first edit.
                self.wait(lambda: peer.node in host.authority.seen, fls)
                host_fl.user_toggle()
                self.wait(lambda: peer_fl.playing, fls)
                self.wait(lambda: abs(host_fl.position() - peer_fl.position()) < 0.16, fls)
                for _ in range(25):
                    for fl in fls:
                        fl.idle()
                    time.sleep(0.015)
                paused = peer_fl.position()
                peer_fl.user_toggle()  # pause inside follower FL, not in app
                self.wait(lambda: not host_fl.playing, fls)
                self.assertFalse(peer_fl.playing)
                self.assertAlmostEqual(host_fl.position(), paused, delta=0.2)
                # Echoes must not increment remote script's edit serial.
                self.assertEqual(host_fl.script._serial, 1)
                self.assertEqual(peer_fl.script._serial, 1)
                peer_fl.bpm = 142.3
                peer_fl.idle()
                self.wait(lambda: host_fl.bpm == 142.3, fls)
                host_fl.bpm = 155.5
                self.wait(lambda: peer_fl.bpm == 155.5, fls)
                self.assertEqual(host_fl.bpm, 155.5)
                peer_fl.user_toggle()
                self.wait(lambda: host_fl.playing, fls)
                self.assertGreater(host_fl.position(), paused - 0.1)
                host_fl.stop()
                host_fl.idle()
                self.wait(lambda: not peer_fl.playing and peer_fl.position() < 0.01, fls)
                # A laptop leaving pauses only its own FL; the rest keep playing.
                host_fl.user_toggle()
                self.wait(lambda: peer_fl.playing, fls)
                peer.close()
                self.assertFalse(peer_fl.playing)
                for _ in range(40):
                    host_fl.idle()
                    time.sleep(0.05)
                self.assertTrue(host_fl.playing)
            finally:
                peer.close()
                host.close()

    def test_pending_local_edit_blocks_old_snapshot(self):
        b = bare_bridge()
        b.host = False
        b.pending_serial = 1
        b.state = {"source": "other"}
        # The guard must return without even reading other bridge state.
        b.apply_remote(time.monotonic())


if __name__ == "__main__":
    unittest.main()
