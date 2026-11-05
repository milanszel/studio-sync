"""Protocol shared by bridge and tests; SysEx data must remain 7-bit clean."""

import hashlib
import hmac
import json
import math

# Imported up front: importing it from several threads at once can fail half way.
from spake2 import SPAKE2_A, SPAKE2_B

PREFIX = b"\x7dSS2"


def encode(payload):
    return PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def decode(data):
    raw = bytes(data)
    if not raw.startswith(PREFIX):
        return None
    return json.loads(raw[len(PREFIX) :].decode("ascii"))


def valid(s):
    try:
        return (
            type(s["playing"]) is bool
            and s["mode"] in (0, 1)
            and math.isfinite(s["bpm"])
            and 10 <= s["bpm"] <= 522
            and math.isfinite(s["pos"])
            and 0 <= s["pos"] <= 86400
            and type(s.get("loop_beats", 0)) in (int, float)
            and math.isfinite(s.get("loop_beats", 0))
            and 0 <= s.get("loop_beats", 0) <= 65536
        )
    except (KeyError, TypeError, ValueError):
        return False


def position(s, now, offset=0):
    # `at` is on the host monotonic timeline; `offset` = host - local.
    age = max(0, now + offset - s["at"])
    pos = s["pos"] + (age if s["playing"] else 0)
    duration = loop_duration(s)
    return pos % duration if duration and s["playing"] else pos


def loop_duration(s):
    beats = s.get("loop_beats", 0)
    return beats * 60.0 / s["bpm"] if s["mode"] == 0 and beats > 0 else 0


def phase_distance(a, b, duration=0):
    distance = abs(a - b)
    if duration:
        distance = distance % duration
        distance = min(distance, duration - distance)
    return distance


def signed_phase(diff, duration=0):
    """A position difference as the shortest signed distance, around the loop if there is one."""
    if duration:
        diff = (diff + duration / 2) % duration - duration / 2
    return diff


class PlayheadFilter:
    """Where a playhead really is, from noisy (time, position) reports.

    FL reports its position in audio buffer sized steps, at uneven moments.
    One report can be off by 20 ms or more; the median of the last second of
    reports, each projected to the same moment, is accurate to a few ms.
    History restarts whenever `key` changes (new revision, tempo, loop, speed)
    or a report is far off the estimate (a seek)."""

    WINDOW = 1.0
    SEEK = 0.05

    def __init__(self):
        self.samples = []
        self.key = None

    def reset(self):
        self.samples = []
        self.key = None

    def add(self, key, t, pos, duration=0):
        if key != self.key:
            self.samples, self.key = [], key
        if self.samples:
            if any(abs(t - old) < 1e-6 for old, _ in self.samples):
                return  # The same report again.
            expected = self.estimate(t, duration)
            if abs(signed_phase(pos - expected, duration)) > self.SEEK:
                self.samples = []
        self.samples.append((t, pos))
        self.samples = [x for x in self.samples if t - x[0] <= self.WINDOW]

    def estimate(self, now, duration=0):
        if not self.samples:
            return None
        ref_t, ref_p = self.samples[-1]
        residuals = sorted(signed_phase((p - t) - (ref_p - ref_t), duration) for t, p in self.samples)
        middle = residuals[len(residuals) // 2]
        if len(residuals) % 2 == 0:
            middle = (middle + residuals[len(residuals) // 2 - 1]) / 2
        pos = ref_p + (now - ref_t) + middle
        return pos % duration if duration else pos

    def __len__(self):
        return len(self.samples)

    def step(self):
        """Size of the steps the position moves in (the audio buffer), or None.
        Reports are spread evenly over one step, so 80% of them span 0.8 steps."""
        if len(self.samples) < 15:
            return None
        ref_t, ref_p = self.samples[-1]
        r = sorted((p - t) - (ref_p - ref_t) for t, p in self.samples)
        return (r[int(len(r) * 0.9)] - r[int(len(r) * 0.1)]) / 0.8


class Authority:
    """Host serializes edits; telemetry from the current leader updates position."""

    def __init__(self):
        self.seen = {}
        self.state = None
        self.revision = 0

    def update(self, source, serial, seq, sample, at, baseline=False):
        """baseline: the sender just (re)joined, so its serial is a starting
        point. Edits it made while away must not override the room."""
        if not valid(sample) or type(serial) is not int or type(seq) is not int:
            return False
        previous = self.seen.get(source)
        if previous and seq <= previous[1]:
            return False
        self.seen[source] = (serial, seq)
        edit = previous is not None and serial > previous[0] and not baseline
        if self.state is None or edit:
            self.revision += 1
            self.state = dict(sample, source=source, revision=self.revision, at=at)
            return True
        if self.state["source"] == source:
            self.state = dict(sample, source=source, revision=self.revision, at=at)
            return True
        return False

    def hand_over(self, source, sample, at):
        """A new laptop leads the same revision, so followers don't seek."""
        if valid(sample) and self.state is not None:
            self.state = dict(sample, source=source, revision=self.revision, at=at)


# Wire protocol between the apps. Bump when packets change incompatibly.
APP_PROTOCOL = 4
# FL controller script protocol. 2 adds the timed 'fire' start, 3 adds timed seeks.
FL_PROTOCOL = 4
# Laptops in one room, host included.
MAX_MEMBERS = 8
# Packets older or newer than this (wall clock seconds) are rejected as replays.
REPLAY_WINDOW = 30

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
PAKE_IDS = dict(idA=b"studio-sync-join", idB=b"studio-sync-host")


def normalize_code(code):
    """Codes are case and separator insensitive: 'k7q-m2p' == 'K7QM2P'."""
    return "".join(c for c in str(code).upper() if c.isalnum())


def new_code():
    """A random room code like K7Q-M2P (about 30 bits).

    Short is safe because the code is only ever used in a SPAKE2 handshake
    (the method magic wormhole uses): each try needs a live exchange with the
    host, nothing captured from the network can be used to test guesses, and
    the host limits how fast anyone can try."""
    import secrets

    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return raw[:3] + "-" + raw[3:]


def check_code(code):
    if len(normalize_code(code)) < CODE_LENGTH:
        raise ValueError("Room code needs at least 6 letters or numbers.")


def room_secret():
    """The key the host's room actually runs on. Joiners receive it through the handshake."""
    import os

    return os.urandom(32)


def pake_join(code):
    """Joiner side: returns the handshake state and the message to send the host."""
    state = SPAKE2_A(normalize_code(code).encode("utf-8"), **PAKE_IDS)
    return state, state.start()


def pake_host(code, message):
    """Host side: answer a joiner's message. Returns (reply, shared key)."""
    state = SPAKE2_B(normalize_code(code).encode("utf-8"), **PAKE_IDS)
    reply = state.start()
    return reply, state.finish(message)


def handshake_proof(key, role, attempt):
    return hmac.new(key, ("studio-sync %s %s" % (role, attempt)).encode(), hashlib.sha256).hexdigest()


def wrap_secret(key, attempt, secret):
    """Encrypt the room secret for one joiner: XOR with a pad only that handshake's key gives."""
    pad = hmac.new(key, ("studio-sync wrap %s" % attempt).encode(), hashlib.sha256).digest()
    return bytes(a ^ b for a, b in zip(secret, pad))


def _canonical(payload):
    body = {k: v for k, v in payload.items() if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign(key, payload):
    """Return a copy of payload carrying an HMAC; the room code never travels."""
    signed = dict(payload)
    signed["mac"] = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    return signed


def verify(key, payload):
    mac = payload.get("mac") if isinstance(payload, dict) else None
    if not isinstance(mac, str):
        return False
    expected = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


class ReplayGuard:
    """Rejects packets that are too old, from the future, or already seen.

    Each sender stamps packets with wall clock time `ts` and a counter `n`
    that only goes up. Like IPsec, a sliding window accepts packets that the
    network delivered out of order, but never the same counter twice.
    """

    ORDER_WINDOW = 1024

    def __init__(self, window=REPLAY_WINDOW):
        self.window = window
        self.nodes = {}
        self.clock_error = None

    def check(self, node, n, ts, now_wall):
        if type(n) is not int or type(ts) not in (int, float) or not isinstance(node, str):
            return False
        skew = now_wall - ts
        if abs(skew) > self.window:
            self.clock_error = skew
            return False
        self.clock_error = None
        entry = self.nodes.get(node)
        if entry is None:
            entry = self.nodes[node] = dict(top=n, seen={n}, at=now_wall)
        else:
            if n in entry["seen"] or n <= entry["top"] - self.ORDER_WINDOW:
                return False
            entry["seen"].add(n)
            if n > entry["top"]:
                entry["top"] = n
                floor = n - self.ORDER_WINDOW
                if len(entry["seen"]) > 2 * self.ORDER_WINDOW:
                    entry["seen"] = {x for x in entry["seen"] if x > floor}
            entry["at"] = now_wall
        if len(self.nodes) > 64:
            self.nodes = {k: v for k, v in self.nodes.items() if now_wall - v["at"] < 2 * self.window}
        return True


def request_mac(key, method, path, body, ts, nonce):
    """HMAC for LAN file/frame requests between apps, bound to a time and a one time nonce."""
    digest = hashlib.sha256(body or b"").hexdigest()
    message = "\n".join((method.upper(), path, str(ts), nonce, digest)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def request_auth(key, method, path, body):
    import secrets
    import time

    ts, nonce = int(time.time()), secrets.token_hex(12)
    return "%d:%s:%s" % (ts, nonce, request_mac(key, method, path, body, ts, nonce))


class NonceGuard:
    def __init__(self, window=60):
        self.window = window
        self.seen = {}

    def check(self, key, method, path, body, header, now_wall):
        try:
            ts_text, nonce, mac = str(header).split(":", 2)
            ts = int(ts_text)
        except ValueError:
            return False
        if abs(now_wall - ts) > self.window or not 8 <= len(nonce) <= 64 or nonce in self.seen:
            return False
        if not hmac.compare_digest(mac, request_mac(key, method, path, body, ts, nonce)):
            return False
        self.seen = {k: v for k, v in self.seen.items() if now_wall - v < 2 * self.window}
        self.seen[nonce] = now_wall
        return True
