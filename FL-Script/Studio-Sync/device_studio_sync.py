# name=Studio Sync Transport
"""FL MIDI controller bridge. Uses documented FL APIs; no network in FL."""

import json
import time
import math
import device
import general
import midi
import mixer
import transport
import patterns

PREFIX = b"\x7dSS2"
_last = None
_sent = 0.0
_serial = 0
_applied = ""
_applying = False
_enabled = False
_last_ping = 0.0
_error = None
_diagnostic_events = 0
_diagnostic_output = False
_diagnostic_ping = False
_coordinated = False
_waiting = None
_pending = None
_tempo_edit_at = 0.0
# Tempo the app last asked for, and when: (bpm, time). Used to verify it took
# effect and to stop FL's own update of it counting as a local edit.
_tempo_target = None
_tempo_reported = False
VERSION = "0.5.1"
PROTOCOL = 3
# Seconds without an app ping before the script stops following the app.
PING_TIMEOUT = 5


def tempo_bpm():
    # Explicit BPM mode: confirmed 140000 by default, 140.0 with flag 1.
    return float(mixer.getCurrentTempo(1))


def pattern_beats():
    if transport.getLoopMode() != 0:
        return 0.0
    try:
        beats = float(patterns.getPatternLength(patterns.patternNumber()))
        return beats if math.isfinite(beats) and 0 < beats <= 65536 else 0.0
    except Exception:
        return 0.0


def position_distance(a, b, s):
    distance = abs(a - b)
    if s["mode"] == 0 and s.get("loop_beats", 0) > 0:
        duration = s["loop_beats"] * 60.0 / s["bpm"]
        distance %= duration
        distance = min(distance, duration - distance)
    return distance


def bar_beats():
    # Beats per bar from the project time signature, for bar aligned corrections.
    try:
        beats = float(general.getRecPPB()) / float(general.getRecPPQ())
        return beats if math.isfinite(beats) and 1 <= beats <= 32 else 4.0
    except Exception:
        return 4.0


def snapshot():
    return {
        "playing": bool(transport.isPlaying()),
        "bpm": tempo_bpm(),
        "pos": float(transport.getSongPos(midi.SONGLENGTH_MS)) / 1000.0,
        "mode": int(transport.getLoopMode()),
        "loop_beats": pattern_beats(),
        "bar_beats": bar_beats(),
    }


def output(payload):
    if device.isAssigned():
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        device.midiOutSysex(b"\xf0" + PREFIX + body + b"\xf7")


def changed(old, new):
    return (
        old["playing"] != new["playing"]
        or abs(old["bpm"] - new["bpm"]) > 0.005
        or old["mode"] != new["mode"]
        or abs(old.get("loop_beats", 0) - new.get("loop_beats", 0)) > 0.005
        or (not new["playing"] and abs(old["pos"] - new["pos"]) > 0.005)
    )


def api_version():
    try:
        return int(general.getVersion())
    except Exception:
        return 0


def status(s):
    return dict(
        s,
        kind="status",
        serial=_serial,
        applied=_applied,
        version=VERSION,
        protocol=PROTOCOL,
        api=api_version(),
        request=_waiting is not None,
    )


def set_tempo(bpm):
    """Set FL's tempo from the app. Newer FL versions have mixer.setCurrentTempo;
    every version accepts the tempo as a recorded control event. Try both and
    check later (in check_tempo) that it actually took effect."""
    global _tempo_target, _tempo_reported
    # The app repeats an unconfirmed command; keep the first request time so
    # a refusal is still reported.
    if _tempo_target is None or abs(_tempo_target[0] - bpm) > 0.005:
        _tempo_target, _tempo_reported = (bpm, time.monotonic()), False
    try:
        mixer.setCurrentTempo(bpm, 1)
    except Exception:
        pass
    if abs(tempo_bpm() - bpm) <= 0.005:
        return
    try:
        general.processRECEvent(
            midi.REC_Tempo, int(round(bpm * 1000)), midi.REC_Control | midi.REC_UpdateControl
        )
    except Exception:
        pass


def check_tempo():
    """Report once if FL ignored a tempo change from the app."""
    global _tempo_target, _tempo_reported
    if _tempo_target is None or _tempo_reported:
        return
    bpm, at = _tempo_target
    if time.monotonic() - at < 0.5:
        return
    if abs(tempo_bpm() - bpm) > 0.005:
        _tempo_reported = True
        message = "FL did not accept tempo %.3f from Studio Sync (FL API %d)." % (bpm, api_version())
        print("Studio Sync: " + message)
        output({"kind": "error", "message": message})
    else:
        _tempo_target = None


def observe(force=False):
    global _last, _sent, _serial, _error, _enabled, _waiting, _pending, _applying, _tempo_edit_at
    if _applying or not _enabled:
        return
    now = time.monotonic()
    if now - _last_ping > PING_TIMEOUT:
        # The app is gone. Keep playing where we are (stopping would lose the
        # take and jump to the start); just stop following until it returns.
        _waiting = _pending = None
        _enabled = False
        print("Studio Sync: app disconnected; FL keeps playing on its own.")
        return
    try:
        s = snapshot()
        if (
            _coordinated
            and _waiting is None
            and _pending is None
            and _last is not None
            and not _last["playing"]
            and s["playing"]
        ):
            # The UI has already started FL; hold and rewind before the common start.
            _applying = True
            try:
                transport.globalTransport(midi.FPT_Play, 1, midi.PME_System)
                transport.setSongPos(_last["pos"] * 1000.0, midi.SONGLENGTH_MS)
            finally:
                _applying = False
            _waiting = dict(s, pos=_last["pos"], requested_at=now)
        if _waiting is not None:
            if now - _waiting["requested_at"] > 2:
                _waiting = None
                s = snapshot()
            else:
                s = {k: _waiting[k] for k in ("playing", "bpm", "pos", "mode")}
                s["loop_beats"] = pattern_beats()
                s["bar_beats"] = bar_beats()
        elif _pending is not None and not _pending.get("seek"):
            s = {k: _pending[k] for k in ("playing", "bpm", "pos", "mode")}
            s["loop_beats"] = pattern_beats()
            s["bar_beats"] = bar_beats()
        edit = _last is not None and changed(_last, s)
        if (
            edit
            and _tempo_target is not None
            and abs(s["bpm"] - _tempo_target[0]) <= 0.005
            and now - _tempo_target[1] < 1.5
            and not changed(dict(_last, bpm=s["bpm"]), s)
        ):
            # FL finished applying the app's tempo; that is not a local edit.
            edit = False
        if edit:
            _serial += 1
            if abs(_last["bpm"] - s["bpm"]) > 0.005:
                _tempo_edit_at = now
        wrapped = bool(
            _last
            and s["playing"]
            and _last["playing"]
            and s["mode"] == 0
            and s.get("loop_beats", 0) > 0
            and _last["pos"] - s["pos"] > s["loop_beats"] * 30.0 / s["bpm"]
        )
        _last = s
        if edit or wrapped or force or now - _sent >= 0.25:
            output(status(s))
            _sent = now
    except Exception as exc:
        if str(exc) != _error:
            _error = str(exc)
            print("Studio Sync error: " + _error)
            output({"kind": "error", "message": _error})


def OnInit():
    global _last, _serial, _enabled, _applied, _diagnostic_events, _diagnostic_output, _diagnostic_ping, _waiting, _pending, _coordinated
    _last, _serial, _enabled, _applied = None, 0, False, ""
    _diagnostic_events, _diagnostic_output, _diagnostic_ping = 0, False, False
    _waiting = _pending = None
    _coordinated = False
    print("Studio Sync " + VERSION + " loaded. Timed starts enabled.")


def OnIdle():
    global _diagnostic_output
    if not _diagnostic_output:
        _diagnostic_output = True
        print("Studio Sync: linked MIDI output = " + str(device.isAssigned()))
    # Fallback only: the app normally sends 'fire' at the due moment, which
    # FL handles on arrival instead of waiting for the next idle callback.
    if _pending is not None and time.monotonic() >= _pending["due"]:
        start_pending()
    check_tempo()
    observe()


def start_pending():
    global _pending, _last, _applying
    command = _pending
    _pending = None
    if command is None or not _enabled or time.monotonic() - _last_ping >= PING_TIMEOUT:
        return
    _applying = True
    try:
        if command.get("seek"):
            # Bar aligned correction: move the playhead without stopping.
            if transport.isPlaying():
                transport.setSongPos(command["pos"] * 1000.0, midi.SONGLENGTH_MS)
            _last = snapshot()
            return
        transport.setSongPos(command["pos"] * 1000.0, midi.SONGLENGTH_MS)
        if not transport.isPlaying():
            transport.globalTransport(midi.FPT_Play, 1, midi.PME_System)
        _last = snapshot()
    except Exception as exc:
        print("Studio Sync scheduled start error: " + str(exc))
        output({"kind": "error", "message": str(exc)})
    finally:
        _applying = False


def OnRefresh(flags):
    observe()


def OnMidiIn(event):
    # FL documents raw SysEx data on this callback. Consume our packets here
    # before higher-level dispatch can omit the raw payload.
    _diagnose(event, "OnMidiIn")
    _receive(event)


def OnSysEx(event):
    # Some FL events have sysex=None. Fallback only for unhandled payloads.
    if not getattr(event, "handled", False):
        _diagnose(event, "OnSysEx")
        _receive(event)


def _diagnose(event, callback):
    global _diagnostic_events
    if _diagnostic_events >= 8:
        return
    _diagnostic_events += 1
    payload = getattr(event, "sysex", None)
    print(
        "Studio Sync: "
        + callback
        + " status="
        + str(getattr(event, "status", None))
        + " sysex="
        + repr(payload)[:180]
    )


def _receive(event):
    global _enabled, _last_ping, _last, _sent, _applied, _applying, _diagnostic_ping, _coordinated, _waiting, _pending
    payload = getattr(event, "sysex", None)
    if payload is None:
        return
    try:
        raw = bytes(payload)
    except (TypeError, ValueError):
        return
    if not raw.startswith(b"\xf0" + PREFIX) or not raw.endswith(b"\xf7"):
        return
    event.handled = True
    try:
        p = json.loads(raw[1 + len(PREFIX) : -1].decode("ascii"))
        if not isinstance(p, dict):
            return
        if p.get("kind") == "fire":
            if _pending is not None and str(p.get("id")) == _applied:
                start_pending()
                observe(True)
            return
        if p.get("kind") == "ping":
            _coordinated = bool(p.get("coordinated", False))
            if not _coordinated:
                _waiting = None
                if _pending is not None and not _pending.get("seek"):
                    _pending = None
            if not _diagnostic_ping:
                _diagnostic_ping = True
                print("Studio Sync: app ping received; linked output = " + str(device.isAssigned()))
            _last_ping = time.monotonic()
            if not _enabled:
                _last = snapshot()
                _enabled = True
            observe(True)
            return
        if p.get("kind") != "apply":
            return
        bpm, pos = float(p["bpm"]), float(p["pos"])
        playing, mode = p["playing"], p["mode"]
        if not 30 <= bpm <= 300 or not math.isfinite(pos) or pos < 0:
            return
        if type(playing) is not bool or mode not in (0, 1):
            return
        # Publish edits made since the last idle callback before applying a snapshot.
        observe()
        if time.monotonic() - _tempo_edit_at < 0.2:
            output(status(_last))
            return
        delay = float(p.get("delay_ms", 0))
        if not math.isfinite(delay) or not 0 <= delay <= 4000:
            return
        if p.get("seek"):
            # Schedule a seek for the 'fire' at the next bar line; never pause.
            if playing and delay > 0 and transport.isPlaying():
                _pending = dict(seek=True, pos=pos, due=time.monotonic() + delay / 1000.0)
                _applied = str(p["id"])
            return
        if _pending is not None and not _pending.get("seek") and str(p["id"]) == _applied and playing:
            output(status(_last))
            return
        _applying = True
        _waiting = _pending = None
        if transport.getLoopMode() != mode:
            transport.setLoopMode()
        if abs(tempo_bpm() - bpm) > 0.005:
            set_tempo(bpm)
        current = snapshot()
        if playing and delay > 0:
            if current["playing"]:
                transport.globalTransport(midi.FPT_Play, 1, getattr(event, "pmeflags", midi.PME_System))
            transport.setSongPos(pos * 1000.0, midi.SONGLENGTH_MS)
            _pending = dict(playing=True, bpm=bpm, mode=mode, pos=pos, due=time.monotonic() + delay / 1000.0)
            _last = dict(playing=True, bpm=bpm, mode=mode, pos=pos, loop_beats=pattern_beats())
            _applied = str(p["id"])
            _enabled = True
            _last_ping = time.monotonic()
            output(status(_last))
            return
        force = bool(p.get("force", False))
        toggle = current["playing"] != playing
        if force or toggle or position_distance(current["pos"], pos, current) > 0.15:
            # Using an absolute time avoids project-length dependent normalization.
            transport.setSongPos(pos * 1000.0, midi.SONGLENGTH_MS)
        if toggle:
            # FPT_Play is the documented play/pause command, preserving pause.
            flags = getattr(event, "pmeflags", midi.PME_System)
            transport.globalTransport(midi.FPT_Play, 1, flags)
            transport.setSongPos(pos * 1000.0, midi.SONGLENGTH_MS)
        _applied = str(p["id"])
        _last = snapshot()  # establish echo-free baseline after applying
        _last_ping = time.monotonic()
        _enabled = True
        output(status(_last))
        _sent = time.monotonic()
    except Exception as exc:
        print("Studio Sync apply error: " + str(exc))
        output({"kind": "error", "message": str(exc)})
    finally:
        _applying = False
