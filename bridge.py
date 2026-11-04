"""Network and FL bridge. One host serializes transport edits for up to MAX_MEMBERS laptops."""

import collections
import hmac
import json
import queue
import select
import socket
import sys
import threading
import time
import uuid
import mido
from protocol import (
    encode,
    decode,
    valid,
    position,
    loop_duration,
    signed_phase,
    PlayheadFilter,
    Authority,
    APP_PROTOCOL,
    FL_PROTOCOL,
    MAX_MEMBERS,
    ReplayGuard,
    room_secret,
    pake_join,
    pake_host,
    handshake_proof,
    wrap_secret,
    sign,
    verify,
)
from discovery import HostDiscovery

PORT = 42611
# Seconds without a packet before a laptop counts as gone.
PEER_TIMEOUT = 3
# A local edit not acknowledged by the host within this time is given up.
PENDING_TIMEOUT = 2
# Seconds of clock samples used to estimate the host clock.
CLOCK_WINDOW = 10
START_MODES = ("together", "instant")
# Phase errors (seconds). Below NUDGE_ERROR: in time. Up to SEEK_ERROR: speed FL
# up or down very slightly until it catches up (inaudible). Above: jump on a bar line.
NUDGE_ERROR = 0.008
SEEK_ERROR = 0.08
# Most FL is sped up or slowed down while nudging (0.8% is about 14 cents if
# anything were resampled; usually nothing is).
MAX_NUDGE = 0.008
# FL normally answers every ping within ~50 ms. Quieter than this means it is
# busy (exporting, a dialog, loading): stop sending it anything but slow pings.
FL_QUIET = 0.3


def fl_protocol(status):
    """Protocol of an FL script status. Scripts before 0.4.0 sent no number."""
    if not status:
        return 0
    value = status.get("protocol")
    return value if type(value) is int else 1


def short_name(value, fallback="Laptop"):
    text = str(value or "").strip()
    for suffix in (".local", ".lan", ".home"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
    return text[:40] or fallback


def clean_audio_key(a):
    if isinstance(a, dict) and type(a.get("listening")) is bool:
        key = {k: str(a.get(k, ""))[:100] for k in ("key", "alternative", "strength")}
        key["listening"] = a["listening"]
        return key
    return None


class ClockFilter:
    """Offset estimate (host minus local) from ping/pong pairs.

    Each pair gives a = t2 - t1 = offset + forward delay and b = t4 - t3 =
    return delay - offset. The smallest a and the smallest b over the last
    CLOCK_WINDOW seconds are each close to the true one way minimum, so
    (min a - min b) / 2 converges much faster than waiting for a single ping
    that was quick in both directions. WiFi power saving spikes never win a
    minimum, and small changes are slewed instead of jumped.
    """

    def __init__(self):
        self.samples = collections.deque()
        self.offset = None

    def add(self, t1, t2, t3, t4):
        self.samples.append((t2 - t1, t4 - t3, t4))
        while self.samples and t4 - self.samples[0][2] > CLOCK_WINDOW:
            self.samples.popleft()
        estimate = (min(a for a, _, _ in self.samples) - min(b for _, b, _ in self.samples)) / 2
        if self.offset is None or abs(estimate - self.offset) > 0.02:
            self.offset = estimate  # First estimate or a real clock step.
        else:
            self.offset += max(-0.0005, min(0.0005, estimate - self.offset))
        return self.offset

    def __len__(self):
        return len(self.samples)

    def delays(self):
        return [max(0.0, a + b) for a, b, _ in self.samples]

    def rtt(self):
        return min(self.delays()) if self.samples else None

    def jitter(self):
        if not self.samples:
            return None
        delays = self.delays()
        return max(delays) - min(delays)


class Bridge:
    def __init__(
        self,
        host,
        address,
        code,
        to_fl,
        from_fl,
        virtual=False,
        shared_ports=None,
        name=None,
        start_mode="together",
        host_port=PORT,
    ):
        self.setup_state(host, code, name, start_mode)
        self.shared_ports = shared_ports is not None
        try:
            self.host_addr = None if host else (socket.gethostbyname(address), host_port)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if sys.platform == "win32" and hasattr(socket, "SIO_UDP_CONNRESET"):
                # Windows otherwise raises on the next receive after a peer closes.
                self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            self.sock.bind(("0.0.0.0", PORT if host else 0))
            self.sock.setblocking(False)
            if host:
                self.discovery = HostDiscovery(self.name)
            if shared_ports:
                self.output, self.input = shared_ports
                self.input.callback = self.on_midi
            else:
                self.output = mido.open_output(to_fl, virtual=virtual)
                self.input = mido.open_input(from_fl, virtual=virtual, callback=self.on_midi)
        except Exception:
            self.cleanup()
            raise
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.fire_thread = threading.Thread(target=self.fire_loop, daemon=True)
        self.fire_thread.start()

    def setup_state(self, host, code, name=None, start_mode="together"):
        """Everything except sockets, MIDI ports and threads (also used by tests)."""
        self.host_addr = None
        self.host, self.code = host, code
        # The host makes a random room secret; a joiner gets it through a SPAKE2
        # handshake that proves it knows the room code (see protocol.new_code).
        self.key = room_secret() if host else None
        self.pake = None
        self.handshakes = {}
        self.hellos = []
        self.last_hello = 0
        self.wrong_code = False
        self.name = short_name(name or socket.gethostname())
        self.start_mode = start_mode if start_mode in START_MODES else "together"
        self.events = queue.Queue()
        self.local_messages = queue.Queue()
        self.done = threading.Event()
        self.node = uuid.uuid4().hex
        self.authority = Authority()
        self.guard = ReplayGuard()
        self.counter = 0
        self.state = None
        self.state_from = None
        self.local = None
        self.seq = 0
        self.share_port = None
        self.audio_key = None
        # Host: every joined laptop. Join: the host. Keyed by node id.
        self.members = {}
        # Join: other laptops in the room as the host reports them.
        self.roster = []
        self.latest = {}
        self.last_peer = 0
        self.last_fl = 0
        self.local_serial = None
        self.pending_serial = None
        self.pending_since = 0
        # The FL edit serial when we (re)joined. Anything up to it is a starting
        # point, not an edit, so changes made while away never override the room.
        self.baseline_serial = None
        self.offset = 0
        self.clock = ClockFilter()
        self.pings = {}
        self.applied_rev = -1
        self.last_apply = 0
        self.drift_since = None
        self.last_correction = 0
        self.corrections = 0
        self.nudges = 0
        self.nudge_ok = True
        self.nudge_until = 0
        self.nudge_epoch = 0
        self.phase_error = None
        self.step_ms = None
        self.local_filter = PlayheadFilter()
        self.remote_filter = PlayheadFilter()
        self.last_fl_ping = 0
        self.tempo_changed_at = 0
        self.last_state_bpm = None
        self.connected_at = time.monotonic()
        self.ever_heard = False
        self.full = False
        self.sock = None
        self.output = None
        self.input = None
        self.rx_count = 0
        self.tx_count = 0
        self.last_rx = "No MIDI received"
        self.last_error = ""
        self.problems = {}
        self.fl_problem = ("", 0)
        self.discovery = None
        self.audio_delay = 0
        self.start_wait = 0.4
        self.midi_lock = threading.Lock()
        self.fire_lock = threading.Condition()
        self.fire_at = None
        self.fire_id = None

    # ---- FL side ---------------------------------------------------------

    def on_midi(self, msg):
        # Stamp on arrival: queue and loop delays must not shift the playhead.
        arrived = time.monotonic()
        self.rx_count += 1
        self.last_rx = str(msg)[:200]
        if msg.type == "sysex":
            try:
                p = decode(msg.data)
                if isinstance(p, dict):
                    self.last_rx = repr(p)[:240]
                    self.local_messages.put((p, arrived))
                else:
                    self.last_error = "Received SysEx with an unrecognized header"
            except (ValueError, TypeError) as exc:
                self.last_error = "MIDI decode error: " + str(exc)

    def midi(self, payload):
        with self.midi_lock:
            self.output.send(mido.Message("sysex", data=encode(payload)))
            self.tx_count += 1

    def fl_live(self, now):
        return bool(self.local and now - self.last_fl < 2)

    def about_me(self, now):
        """What this laptop tells the others about its FL, for their screens."""
        message, at = self.fl_problem
        return dict(
            fl_ready=self.fl_live(now),
            version=self.local.get("version") if self.local else None,
            fl_protocol=fl_protocol(self.local),
            fl_api=(self.local or {}).get("api"),
            step_ms=round(self.step_ms, 1) if self.step_ms is not None else None,
            problem=message if message and now - at < 15 else "",
        )

    # ---- network ----------------------------------------------------------

    def note(self, message):
        """Remember a network problem; it shows for a few seconds after it last happened."""
        self.problems[message] = time.monotonic()
        if len(self.problems) > 20:
            oldest = min(self.problems, key=self.problems.get)
            del self.problems[oldest]

    @property
    def network_error(self):
        now = time.monotonic()
        recent = [(t, m) for m, t in self.problems.items() if now - t < 3]
        return max(recent)[1] if recent else ""

    def send_plain(self, addr, kind, **kw):
        """Handshake packets, sent before a joiner has the room secret."""
        try:
            self.sock.sendto(json.dumps(dict(kind=kind, protocol=APP_PROTOCOL, **kw)).encode(), addr)
        except OSError as exc:
            self.note("Network send failed: " + str(exc))

    def send_to(self, addr, kind, **kw):
        if not addr or self.key is None:
            return
        self.counter += 1
        packet = sign(
            self.key,
            dict(kind=kind, node=self.node, protocol=APP_PROTOCOL, ts=time.time(), n=self.counter, **kw),
        )
        try:
            self.sock.sendto(json.dumps(packet).encode(), addr)
        except OSError as exc:
            # WiFi drops and unreachable hosts are temporary; keep the session.
            self.note("Network send failed: " + str(exc))

    def send(self, kind, **kw):
        """Join: send to the host. Host: send to every member."""
        if self.host:
            for member in list(self.members.values()):
                self.send_to(member["addr"], kind, **kw)
        else:
            self.send_to(self.host_addr, kind, **kw)

    def live_members(self, now):
        return {n: m for n, m in self.members.items() if now - m["last"] < PEER_TIMEOUT}

    def remember(self, p, addr, now):
        """Record what a laptop reports about itself."""
        member = self.members.get(p["node"]) or dict(addr=addr, joined=now)
        port = p.get("share_port")
        fp = p.get("fl_protocol")
        member.update(
            addr=addr,
            last=now,
            name=short_name(p.get("name"), "Laptop " + str(len(self.members) + 1)),
            fl_ready=bool(p.get("fl_ready")),
            fl_protocol=fp if type(fp) is int else 0,
            version=p.get("version"),
            fl_api=p.get("fl_api") if type(p.get("fl_api")) is int else None,
            step_ms=p.get("step_ms") if type(p.get("step_ms")) in (int, float) else None,
            problem=str(p.get("problem") or "")[:160],
            share_port=port if type(port) is int and 1 <= port <= 65535 else None,
        )
        key = clean_audio_key(p.get("audio_key"))
        if key is not None:
            member["audio_key"], member["audio_at"] = key, now
        self.members[p["node"]] = member
        self.last_peer = now
        self.ever_heard = True
        return member

    def receive(self, p, addr, now):
        if not isinstance(p, dict):
            return
        kind = p.get("kind")
        if kind in ("hello", "welcome", "full"):
            if self.host and kind == "hello":
                self.receive_hello(p, addr, now)
            elif not self.host and addr == self.host_addr and kind in ("welcome", "full"):
                self.receive_welcome(p, kind, now)
            return
        if self.key is None:
            return
        if not verify(self.key, p):
            if "code" in p and "mac" not in p:
                self.note("Another laptop runs an older Studio Sync. Update every laptop.")
            return
        if p.get("node") == self.node:
            return
        if not self.guard.check(p.get("node"), p.get("n"), p.get("ts"), time.time()):
            if self.guard.clock_error is not None:
                self.note(
                    "Laptop clocks differ by %d seconds. Turn on automatic date and time on every laptop."
                    % round(abs(self.guard.clock_error))
                )
            return
        if self.host:
            self.receive_as_host(p, kind, addr, now)
        elif addr == self.host_addr:
            self.receive_as_join(p, kind, now)

    def receive_as_host(self, p, kind, addr, now):
        node = p["node"]
        if kind == "bye":
            self.members.pop(node, None)
            return
        known = node in self.members
        if not known and kind != "ping":
            return
        if not known and not self.has_seat(now):
            self.send_plain(addr, "full", limit=MAX_MEMBERS)
            return
        if kind == "ping":
            member = self.remember(p, addr, now)
            # Keep pongs under one network packet: the roster rides every 10th.
            member["pongs"] = member.get("pongs", 0) + 1
            roster = self.roster_for(node, now) if member["pongs"] % 10 == 1 else None
            self.send_to(
                addr,
                "pong",
                id=p["id"],
                stamp=now,
                sent=time.monotonic(),
                name=self.name,
                share_port=self.share_port,
                **self.about_me(now),
                audio_key=self.audio_key,
                state=self.authority.state,
                seen={node: self.authority.seen[node][0]} if node in self.authority.seen else {},
                start_mode=self.start_mode,
                roster=roster,
            )
        elif kind == "sample":
            member = self.members[node]
            if member["addr"] != addr or not valid(p["sample"]):
                return
            at = float(p["at"])
            if not now - 3 <= at <= now + 0.25:
                return
            member["last"] = now
            self.last_peer = now
            self.latest[node] = (p["sample"], at)
            self.accept_sample(
                node,
                p["serial"],
                p["seq"],
                p["sample"],
                at,
                now,
                p.get("request", False),
                baseline=bool(p.get("baseline")),
            )

    def has_seat(self, now):
        if len(self.members) < MAX_MEMBERS - 1:
            return True
        # Expired members don't hold seats.
        self.members = self.live_members(now)
        return len(self.members) < MAX_MEMBERS - 1

    # ---- joining: SPAKE2 handshake ------------------------------------------

    def receive_hello(self, p, addr, now):
        """Host: a laptop wants in. Answer its handshake; only a laptop that
        typed the right code can use the answer."""
        attempt, message, node = p.get("attempt"), p.get("pake"), p.get("node")
        if not (isinstance(attempt, str) and 16 <= len(attempt) <= 64 and isinstance(message, str)):
            return
        if not isinstance(node, str) or len(message) > 200:
            return
        cached = self.handshakes.get(attempt)
        if cached:
            self.send_plain(addr, "welcome", **cached["reply"])  # A repeated hello; same answer.
            return
        # Every handshake is one guess at the code, so limit how fast anyone can guess.
        self.hellos = [h for h in self.hellos if now - h[0] < 60]
        if sum(1 for h in self.hellos if h[1] == addr[0]) >= 10 or len(self.hellos) >= 30:
            self.note(
                "A laptop at %s keeps trying to join. If it isn't yours, change the room code." % addr[0]
            )
            return
        self.hellos.append((now, addr[0]))
        if node not in self.members and not self.has_seat(now):
            self.send_plain(addr, "full", limit=MAX_MEMBERS)
            return
        try:
            reply, key = pake_host(self.code, bytes.fromhex(message))
        except Exception:
            return
        fields = dict(
            attempt=attempt,
            pake=reply.hex(),
            proof=handshake_proof(key, "host", attempt),
            secret=wrap_secret(key, attempt, self.key).hex(),
        )
        self.handshakes = {a: h for a, h in self.handshakes.items() if now - h["at"] < 30}
        self.handshakes[attempt] = dict(reply=fields, at=now)
        self.send_plain(addr, "welcome", **fields)

    def send_hello(self, now):
        """Joiner: start (or repeat) the handshake with the host."""
        if self.pake is None or now - self.pake["at"] > 5:
            state, message = pake_join(self.code)
            self.pake = dict(state=state, message=message, attempt=uuid.uuid4().hex, at=now)
        self.last_hello = now
        self.send_plain(
            self.host_addr,
            "hello",
            attempt=self.pake["attempt"],
            pake=self.pake["message"].hex(),
            node=self.node,
        )

    def receive_welcome(self, p, kind, now):
        if kind == "full":
            self.full = True
            self.key = None  # Wait outside and ask again every few seconds.
            self.note("This room is full (%d laptops)." % MAX_MEMBERS)
            return
        pake = self.pake
        if not pake or p.get("attempt") != pake["attempt"]:
            return
        self.pake = None
        try:
            key = pake["state"].finish(bytes.fromhex(p["pake"]))
            wrapped = bytes.fromhex(p["secret"])
        except Exception:
            return
        if not hmac.compare_digest(str(p.get("proof")), handshake_proof(key, "host", pake["attempt"])):
            self.wrong_code = True
            self.note("Wrong room code. Check the code on the host screen and connect again.")
            return
        secret = wrap_secret(key, pake["attempt"], wrapped)
        if len(secret) == 32:
            self.key = secret
            self.full = False

    def roster_for(self, node, now):
        """Everyone else in the room, so joiners can show names and share directly."""
        people = []
        for other, m in self.live_members(now).items():
            if other == node:
                continue
            people.append(
                dict(
                    node=other,
                    name=m["name"],
                    ip=m["addr"][0],
                    share_port=m.get("share_port"),
                    fl_ready=m["fl_ready"],
                    fl_protocol=m["fl_protocol"],
                    fl_api=m.get("fl_api"),
                    step_ms=m.get("step_ms"),
                    problem=m.get("problem", ""),
                    audio_key=m.get("audio_key") if now - m.get("audio_at", 0) < 3 else None,
                )
            )
        return people

    def receive_as_join(self, p, kind, now):
        if kind == "bye":
            self.members.clear()
            self.last_peer = 0
            return
        if kind != "pong":
            return
        start = self.pings.pop(p["id"], None)
        if start is None:
            return
        self.full = False
        if not self.connected(now):
            # (Re)joined: our current serial is a starting point; drop offline edits.
            self.baseline_serial = self.local_serial
            self.pending_serial = None
        self.members = {p["node"]: self.members.get(p["node"], {})}
        self.remember(p, self.host_addr, now)
        self.offset = self.clock.add(start, float(p["stamp"]), float(p.get("sent", p["stamp"])), now)
        if p.get("start_mode") in START_MODES:
            self.start_mode = p["start_mode"]
        if p.get("roster") is None:
            self.receive_state(p)
            return
        roster = []
        for r in p.get("roster") or []:
            if isinstance(r, dict) and isinstance(r.get("node"), str):
                port = r.get("share_port")
                fp = r.get("fl_protocol")
                roster.append(
                    dict(
                        node=r["node"],
                        name=short_name(r.get("name")),
                        ip=str(r.get("ip", "")),
                        share_port=port if type(port) is int and 1 <= port <= 65535 else None,
                        fl_ready=bool(r.get("fl_ready")),
                        fl_protocol=fp if type(fp) is int else 0,
                        fl_api=r.get("fl_api") if type(r.get("fl_api")) is int else None,
                        step_ms=r.get("step_ms") if type(r.get("step_ms")) in (int, float) else None,
                        problem=str(r.get("problem") or "")[:160],
                        audio_key=clean_audio_key(r.get("audio_key")),
                    )
                )
        self.roster = roster[: MAX_MEMBERS - 2]
        self.receive_state(p)

    def receive_state(self, p):
        ack = (p.get("seen") or {}).get(self.node, (None,))
        acknowledged = ack if type(ack) is int else None
        if (
            self.pending_serial is not None
            and acknowledged is not None
            and acknowledged >= self.pending_serial
        ):
            self.pending_serial = None
        s = p.get("state")
        if isinstance(s, dict) and valid(s):
            if p["node"] != self.state_from:
                # A restarted host counts revisions from zero again.
                self.state_from, self.state, self.applied_rev = p["node"], None, -1
            if self.state is None or s["revision"] >= self.state["revision"]:
                self.state = s

    # ---- timeline ---------------------------------------------------------

    def timeline_sample(self, p):
        sample = {k: p[k] for k in ("playing", "bpm", "pos", "mode")}
        sample["loop_beats"] = p.get("loop_beats", 0)
        # The local playhead leads by output compensation. Publish nominal time.
        if sample["playing"] and not p.get("request", False):
            pos = sample["pos"] - self.audio_delay
            duration = loop_duration(sample)
            sample["pos"] = pos % duration if duration else max(0, pos)
        return sample

    def local_status(self, p, now):
        if p.get("kind") == "error":
            if str(p.get("message", "")).startswith("nudge"):
                self.nudge_ok = False  # This FL can't change playback speed: correct by jumping only.
            self.fl_problem = (str(p.get("message", "Unknown"))[:160], now)
            self.last_error = "FL script error: " + str(p.get("message", "Unknown"))
            self.events.put("FL script error: " + str(p.get("message", "Unknown")))
            return
        if p.get("kind") != "status" or not valid(p) or type(p.get("serial")) is not int:
            self.last_error = "FL response rejected: " + repr(p)[:240]
            return
        self.last_error = ""
        if self.baseline_serial is None:
            self.baseline_serial = p["serial"]
        if (
            self.local_serial is not None
            and p["serial"] > self.local_serial
            and not self.host
            and self.connected(now)
        ):
            self.pending_serial = p["serial"]
            self.pending_since = now
        self.local_serial = p["serial"]
        self.local, self.last_fl = p, now
        if p["playing"] and not p.get("request"):
            key = (p["bpm"], p["mode"], p.get("loop_beats", 0), self.nudge_epoch, now < self.nudge_until)
            self.local_filter.add(key, now, p["pos"], loop_duration(p))
            step = self.local_filter.step()
            if step is not None and not loop_duration(p):
                ms = step * 1000
                self.step_ms = ms if self.step_ms is None else self.step_ms * 0.95 + ms * 0.05
        else:
            self.local_filter.reset()
        self.seq += 1
        sample = self.timeline_sample(p)
        if self.host:
            self.accept_sample(self.node, p["serial"], self.seq, sample, now, now, p.get("request", False))
            self.state = self.authority.state
        elif self.clock_ready(now):
            self.send_sample(sample, now)

    def clock_ready(self, now):
        return len(self.clock) >= 3 and now - self.last_peer < PEER_TIMEOUT

    def send_sample(self, sample, at_local):
        self.send(
            "sample",
            serial=self.local["serial"],
            seq=self.seq,
            sample=sample,
            at=at_local + self.offset,
            request=self.local.get("request", False),
            # After (re)joining, our serial is a starting point, not an edit.
            baseline=self.local["serial"] == self.baseline_serial,
        )

    def accept_sample(self, node, serial, seq, sample, at, now, request=False, baseline=False):
        before = self.authority.revision
        old = self.authority.state
        # A prepared start must retain its future timestamp until it fires.
        scheduled = bool(old and old.get("scheduled") and old["source"] == node and now < old["at"])
        self.authority.update(node, serial, seq, sample, at, baseline=baseline)
        if scheduled and self.authority.revision == before:
            self.authority.state = old
        if request and self.authority.revision != before and sample["playing"]:
            self.authority.state.update(at=now + self.start_wait, scheduled=True)

    def forget_departed(self, now):
        """Host: drop laptops gone for a minute, and their bookkeeping."""
        gone = [n for n, m in self.members.items() if now - m["last"] > 60]
        for node in gone:
            del self.members[node]
        source = (self.authority.state or {}).get("source")
        keep = set(self.members) | {self.node, source}
        for table in (self.latest, self.authority.seen):
            for node in [n for n in table if n not in keep]:
                del table[node]

    def hand_over(self, now):
        """Host: if the laptop leading playback left, let someone still here lead,
        without a new revision, so nobody seeks."""
        s = self.authority.state
        if not s or s["source"] == self.node or (s.get("scheduled") and now < s["at"]):
            return
        source = self.members.get(s["source"])
        if source and now - source["last"] < PEER_TIMEOUT and now - s["at"] < 1:
            return
        if self.fl_live(now):
            self.authority.hand_over(self.node, self.timeline_sample(self.local), self.last_fl)
            return
        for node, member in self.live_members(now).items():
            sample, at = self.latest.get(node, (None, 0))
            if sample is not None and now - at < 1:
                self.authority.hand_over(node, sample, at)
                return

    # ---- start timing -----------------------------------------------------

    def schedule_fire(self, ident, due):
        with self.fire_lock:
            self.fire_id, self.fire_at = ident, due
            self.fire_lock.notify()

    def cancel_fire(self):
        with self.fire_lock:
            self.fire_id = self.fire_at = None
            self.fire_lock.notify()

    def fire_loop(self):
        """Send a start or seek at the due moment. FL acts on arrival in OnMidiIn,
        which is far tighter than its own OnIdle polling."""
        while not self.done.is_set():
            with self.fire_lock:
                while not self.done.is_set() and self.fire_at is None:
                    self.fire_lock.wait(0.25)
                if self.done.is_set():
                    return
                ident, due = self.fire_id, self.fire_at
                remaining = due - time.monotonic()
                if remaining > 0.003:
                    # Wake slightly early; a new schedule or cancel interrupts the wait.
                    self.fire_lock.wait(remaining - 0.002)
                    continue
                self.fire_id = self.fire_at = None
            while time.monotonic() < due:
                pass
            try:
                self.midi(dict(kind="fire", id=ident))
            except Exception as exc:
                self.last_error = "Start signal failed: " + str(exc)

    def connected(self, now):
        return bool(self.last_peer) and now - self.last_peer < PEER_TIMEOUT

    def bar_seconds(self, bpm):
        beats = (self.local or {}).get("bar_beats", 4)
        beats = beats if type(beats) in (int, float) and 1 <= beats <= 32 else 4
        return beats * 60.0 / bpm

    def apply_remote(self, now):
        if self.host:
            self.state = self.authority.state
        s = self.state
        if self.pending_serial is not None:
            if now - self.pending_since < PENDING_TIMEOUT:
                return  # Don't overwrite a local edit with a pre-acknowledgment snapshot.
            self.pending_serial = None  # The host never confirmed; follow the room again.
        if not s or not self.local or now - self.last_fl > FL_QUIET or not self.connected(now):
            self.phase_error = None
            return
        if self.last_state_bpm is not None and abs(self.last_state_bpm - s["bpm"]) > 0.005:
            self.tempo_changed_at = now
        self.last_state_bpm = s["bpm"]
        scheduled = bool(s.get("scheduled"))
        ident = str(s["revision"])
        confirmed = self.local.get("applied") == ident
        if s["source"] == self.node and (not scheduled or confirmed):
            self.applied_rev = s["revision"]
            self.phase_error = None  # Leading: everyone else follows this laptop.
            return
        offset = 0 if self.host else self.offset
        age = now + offset - s["at"]
        # Never use an old playing state after the source stopped reporting.
        if age > 2 or (age < -0.25 and not scheduled):
            return
        force = s["revision"] != self.applied_rev
        needs_state = (
            self.local["playing"] != s["playing"]
            or abs(self.local["bpm"] - s["bpm"]) > 0.01
            or self.local["mode"] != s["mode"]
        )
        cycle = loop_duration(s)
        delay = 0
        predicted = s["pos"]
        if s["playing"]:
            # How far past its start the room is, including our output compensation.
            lead = age + self.audio_delay
            if scheduled and not confirmed and lead < 0:
                delay = -lead
            else:
                predicted = s["pos"] + lead
                predicted = predicted % cycle if cycle else max(0, predicted)
        duration = cycle
        local_duration = loop_duration(self.local)
        if not local_duration or abs(local_duration - duration) > 0.001:
            duration = 0
        # Compare filtered playheads: one FL report can be off by a whole audio buffer.
        error = None
        aim, here = predicted, None
        if s["playing"] and self.local["playing"] and not (scheduled and age < 0):
            key = (s["source"], s["revision"], s["bpm"], s["mode"], s.get("loop_beats", 0))
            self.remote_filter.add(key, s["at"], s["pos"], cycle)
            room = self.remote_filter.estimate(now + offset, cycle)
            mine = self.local_filter.estimate(now, local_duration)
            if room is not None and mine is not None and len(self.local_filter) >= 4:
                error = signed_phase(room + self.audio_delay - mine, duration)  # Positive: we are behind.
                aim, here = mine + error, mine
        else:
            self.remote_filter.reset()
        self.phase_error = error
        local_pos = position(dict(self.local, at=self.last_fl), now)
        nudging = now < self.nudge_until
        settling = now - self.tempo_changed_at < 1 or needs_state or nudging or now - self.last_correction < 1
        big = error is not None and abs(error) > SEEK_ERROR
        small = error is not None and abs(error) > NUDGE_ERROR
        drift = not settling and (big or small)
        if drift:
            if self.drift_since is None:
                self.drift_since = now
        else:
            self.drift_since = None
        # Avoid chasing one late MIDI/network sample: errors must last before acting.
        sustained = drift and now - self.drift_since
        seek = big and sustained >= 0.4 and now - self.last_correction >= 2
        seek = seek or (small and not self.nudge_ok and sustained >= 0.6 and now - self.last_correction >= 2)
        nudge = small and not big and self.nudge_ok and sustained >= 0.6
        if now - self.last_apply < 0.05:
            return
        if force or not confirmed or needs_state:
            self.midi(
                dict(
                    kind="apply",
                    id=ident,
                    playing=s["playing"],
                    bpm=s["bpm"],
                    mode=s["mode"],
                    pos=predicted,
                    force=force,
                    delay_ms=delay * 1000,
                )
            )
            if delay > 0:
                self.schedule_fire(ident, now + delay)
        elif nudge:
            # Speed FL up (or slow it down) a fraction of a percent until it has caught up.
            rate = max(-MAX_NUDGE, min(MAX_NUDGE, error / 2.5))
            if abs(rate) < 0.001:
                rate = 0.001 if error > 0 else -0.001
            length = max(0.3, error / rate)
            self.midi(dict(kind="nudge", id=ident, speed=1 + rate, ms=length * 1000))
            self.nudge_until = now + length + 0.25
            self.nudge_epoch += 1
            self.nudges += 1
            self.last_apply = now
            self.drift_since = None
            return
        elif seek:
            # Jump back into place on the next bar line, where it is least audible.
            unit = self.bar_seconds(s["bpm"])
            if unit > 3.9:
                unit = 60.0 / s["bpm"]  # Long bars at slow tempos: use the next beat (FL allows 4 s).
            at = here if here is not None else local_pos
            wait = unit - (at % unit)
            if wait < 0.05:
                wait += unit
            target = aim + wait
            target = target % cycle if cycle else target
            self.midi(
                dict(
                    kind="apply",
                    id=ident,
                    playing=True,
                    bpm=s["bpm"],
                    mode=s["mode"],
                    pos=target,
                    force=True,
                    seek=True,
                    delay_ms=wait * 1000,
                )
            )
            self.schedule_fire(ident, now + wait)
            self.corrections += 1
        else:
            return
        self.applied_rev = s["revision"]
        self.last_apply = now
        self.last_correction = now
        self.drift_since = None

    def pause_local(self):
        self.cancel_fire()
        if self.local:
            self.midi(
                dict(
                    kind="apply",
                    id="disconnect",
                    playing=False,
                    bpm=self.local["bpm"],
                    mode=self.local["mode"],
                    pos=position(dict(self.local, at=self.last_fl), time.monotonic()),
                    force=True,
                )
            )

    def coordinated(self, now):
        """Hold a pressed Play for the shared count in only when every laptop can do it."""
        if self.start_mode != "together" or not self.connected(now):
            return False
        if self.host:
            people = self.live_members(now).values()
        else:
            people = list(self.live_members(now).values()) + self.roster
        return all(m.get("fl_protocol", 0) >= FL_PROTOCOL for m in people if m.get("fl_ready"))

    # ---- loop -------------------------------------------------------------

    def read_network(self):
        for _ in range(64):
            try:
                data, addr = self.sock.recvfrom(16384)
            except BlockingIOError:
                return
            except OSError as exc:
                # Includes Windows connection resets; never fatal for UDP.
                self.note("Network receive failed: " + str(exc))
                return
            arrived = time.monotonic()
            try:
                self.receive(json.loads(data), addr, arrived)
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                continue

    def people(self, now):
        """Everyone else in the room, for the interface."""
        result = []
        for node, m in self.live_members(now).items():
            result.append(
                dict(
                    node=node,
                    name=m.get("name", "Laptop"),
                    fl=bool(m.get("fl_ready")),
                    host=not self.host,
                    outdated=m.get("fl_ready") and m.get("fl_protocol", 0) < FL_PROTOCOL,
                    fl_api=m.get("fl_api"),
                    step_ms=m.get("step_ms"),
                    problem=m.get("problem", ""),
                    audio_key=m.get("audio_key") if now - m.get("audio_at", 0) < 3 else None,
                )
            )
        if not self.host and self.connected(now):
            for r in self.roster:
                result.append(
                    dict(
                        node=r["node"],
                        name=r["name"],
                        fl=r["fl_ready"],
                        host=False,
                        outdated=r["fl_ready"] and r["fl_protocol"] < FL_PROTOCOL,
                        fl_api=r.get("fl_api"),
                        step_ms=r.get("step_ms"),
                        problem=r.get("problem", ""),
                        audio_key=r.get("audio_key"),
                    )
                )
        return result

    def share_targets(self, now):
        """(ip, port, name) of every laptop that can receive files and frames."""
        targets = []
        for m in self.live_members(now).values():
            if m.get("share_port"):
                targets.append((m["addr"][0], m["share_port"], m.get("name", "Laptop")))
        if not self.host and self.connected(now):
            for r in self.roster:
                if r.get("share_port") and r.get("ip"):
                    targets.append((r["ip"], r["share_port"], r["name"]))
        return targets

    def hint(self, now):
        if self.wrong_code:
            return "Wrong room code. Check the code on the host screen, then connect again."
        if self.full and not self.connected(now):
            return "This room is full (%d laptops)." % MAX_MEMBERS
        if self.network_error:
            return self.network_error
        if not self.host and not self.ever_heard and now - self.connected_at > 4:
            return (
                "No reply from the host at %s. Check the address and room code, and allow Python "
                "through the host's firewall." % (self.host_addr[0] if self.host_addr else "?")
            )
        return ""

    def run(self):
        heartbeat, report = 0, 0
        was_live = False
        try:
            while not self.done.is_set():
                self.read_network()
                for _ in range(64):
                    try:
                        p, arrived = self.local_messages.get_nowait()
                    except queue.Empty:
                        break
                    self.local_status(p, arrived)
                now = time.monotonic()
                if now - heartbeat >= 0.05:
                    heartbeat = now
                    # When FL goes quiet (an export, a dialog) send it only a slow
                    # ping: a queue of messages can stall or crash it.
                    if now - self.last_fl < FL_QUIET or now - self.last_fl_ping >= 1:
                        self.last_fl_ping = now
                        self.midi({"kind": "ping", "coordinated": self.coordinated(now)})
                    if (
                        not self.host
                        and self.key is not None
                        and self.ever_heard
                        and now - self.last_peer > 6
                    ):
                        # The host may have restarted with a new room secret: join again.
                        self.key = None
                    if not self.host and self.key is None:
                        # A full room is retried slowly; a wrong code waits for the user.
                        if not self.wrong_code and now - self.last_hello >= (5 if self.full else 1):
                            try:
                                self.send_hello(now)
                            except Exception as exc:
                                self.last_hello = now
                                self.note("Couldn't start joining: %s" % exc)
                    elif not self.host:
                        key = uuid.uuid4().hex
                        self.pings[key] = time.monotonic()
                        self.pings = {k: v for k, v in self.pings.items() if now - v < 3}
                        self.send(
                            "ping",
                            id=key,
                            name=self.name,
                            share_port=self.share_port,
                            audio_key=self.audio_key,
                            **self.about_me(now),
                        )
                        # Latest telemetry repeats, so a dropped edit is retried.
                        if self.local and self.clock_ready(now):
                            self.send_sample(self.timeline_sample(self.local), self.last_fl)
                    else:
                        self.hand_over(now)
                        self.forget_departed(now)
                live = self.connected(now)
                if was_live and not live:
                    # Keep playing: a WiFi blip must not stop a take. Resync on return.
                    self.cancel_fire()
                    self.events.put(
                        "Lost the other laptops. FL keeps playing and will resync when they return."
                    )
                was_live = live
                self.apply_remote(now)
                if now - report >= 0.4:
                    report = now
                    self.report(now, live)
                # Wake as soon as a packet arrives; otherwise poll MIDI every 5 ms.
                try:
                    select.select([self.sock], [], [], 0.005)
                except (OSError, ValueError):
                    self.done.wait(0.005)
        except Exception as exc:
            self.events.put("Bridge stopped: " + str(exc))
        finally:
            try:
                self.send("bye")
            except Exception:
                pass
            try:
                self.pause_local()
            except Exception:
                pass
            self.cleanup()

    def report(self, now, live):
        people = self.people(now)
        outdated = [p["name"] for p in people if p["outdated"]]
        keys = [dict(name=p["name"], **p["audio_key"]) for p in people if p.get("audio_key")]
        clock = not self.host and live
        self.events.put(
            dict(
                fl=self.fl_live(now),
                peer=live and any(p["fl"] for p in people),
                people=people,
                local=self.local,
                state=self.state,
                peer_audio_key=keys[0] if keys else None,
                peer_audio_keys=keys,
                rx=self.rx_count,
                tx=self.tx_count,
                last_rx=self.last_rx,
                error=self.last_error or self.hint(now),
                fl_protocol=fl_protocol(self.local),
                outdated=outdated,
                start_mode=self.start_mode,
                corrections=self.corrections,
                nudges=self.nudges,
                phase_ms=self.phase_error * 1000 if self.phase_error is not None else None,
                following=bool(self.state and self.state.get("source") != self.node and live),
                audio_delay_ms=self.audio_delay * 1000,
                step_ms=self.step_ms,
                fl_age_ms=(now - self.last_fl) * 1000 if self.last_fl else None,
                peer_age_ms=(now - self.last_peer) * 1000 if self.last_peer else None,
                clock_offset_ms=self.offset * 1000 if clock and len(self.clock) else None,
                clock_samples=len(self.clock) if clock else 0,
                jitter_ms=self.clock.jitter() * 1000 if clock and len(self.clock) else None,
                rtt=self.clock.rtt() * 1000 if clock and len(self.clock) else None,
            )
        )

    def cleanup(self):
        if self.discovery:
            self.discovery.close()
            self.discovery = None
        if self.shared_ports and self.input:
            self.input.callback = None
        for resource in ((self.sock,) if self.shared_ports else (self.input, self.output, self.sock)):
            if resource:
                resource.close()

    def close(self):
        self.done.set()
        with self.fire_lock:
            self.fire_lock.notify_all()
        self.thread.join(timeout=2)
        self.fire_thread.join(timeout=1)
