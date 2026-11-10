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
VERSION = "0.6.4"
PROTOCOL = 5
# Seconds without an app ping before the script stops following the app.
PING_TIMEOUT = 5
# FL calls OnIdle many times a second. A longer gap means FL is busy (exporting,
# a dialog, loading); the script then leaves FL completely alone. Anything it
# skips the app sends again once FL is responsive.
BUSY_GAP = 0.3
_last_idle = 0.0
# When the current speed nudge ends (monotonic seconds), or None.
_nudge = None


def fl_tempo():
    # Explicit BPM mode: confirmed 140000 by default, 140.0 with flag 1.
    return float(mixer.getCurrentTempo(1))


def tempo_bpm():
    """The project tempo. During a nudge FL runs a fraction of a percent off
    it; that is not a tempo change, so report the tempo underneath."""
    global _nudge
    current = fl_tempo()
    if _nudge is None:
        return current
    base, ended = _nudge["base"], _nudge["ended"]
    at_base, at_nudge = abs(current - base) <= 0.005, abs(current - _nudge["tempo"]) <= 0.005
    if not (at_base or at_nudge):
        _nudge = None  # Someone changed the tempo during the nudge; theirs wins.
        return current
    if ended is not None:
        # FL can apply a tempo on its next cycle; wait for the return to settle.
        waited = time.monotonic() - ended
        if (at_base and waited > 0.5) or waited > 1.5:
            _nudge = None
            return current
    return base


def nudge_factor():
    # Song position in ms is ticks at the current tempo; convert to and from
    # the project tempo while a nudge is running.
    if _nudge is None:
        return 1.0
    return fl_tempo() / _nudge["base"]


def song_pos():
    return float(transport.getSongPos(midi.SONGLENGTH_MS)) / 1000.0 * nudge_factor()


def set_song_pos(seconds):
    transport.setSongPos(seconds * 1000.0 / nudge_factor(), midi.SONGLENGTH_MS)


def write_tempo(bpm):
    try:
        mixer.setCurrentTempo(bpm, 1)
    except Exception:
        pass
    if abs(fl_tempo() - bpm) <= 0.005:
        return True
    try:
        general.processRECEvent(
            midi.REC_Tempo, int(round(bpm * 1000)), midi.REC_Control | midi.REC_UpdateControl
        )
    except Exception:
        pass
    return abs(fl_tempo() - bpm) <= 0.005


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


def busy():
    return time.monotonic() - _last_idle > BUSY_GAP


def end_nudge():
    """Back to the project tempo after a nudge (or if anything interrupts one).
    Nudges change the tempo by a fraction of a percent instead of using
    transport.setPlaybackSpeed, which some FL versions scale wrongly (a 0.8%
    nudge became 4x speed and a 407 BPM project)."""
    if _nudge is None or _nudge["ended"] is not None:
        return
    _nudge["ended"] = time.monotonic()
    if abs(fl_tempo() - _nudge["base"]) > 0.005:
        write_tempo(_nudge["base"])


def snapshot():
    return {
        "playing": bool(transport.isPlaying()),
        "bpm": tempo_bpm(),
        "pos": song_pos(),
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
    global _nudge
    if _tempo_target is None or abs(_tempo_target[0] - bpm) > 0.005:
        _tempo_target, _tempo_reported = (bpm, time.monotonic()), False
    _nudge = None  # A new room tempo replaces any nudge.
    write_tempo(bpm)


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
        end_nudge()
        print("Studio Sync: app disconnected; FL keeps playing on its own.")
        return
    try:
        s = snapshot()
        if not s["playing"]:
            end_nudge()
        if (
            _coordinated
            and not busy()
            and now - _last_idle < 0.25
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
                set_song_pos(_last["pos"])
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
    global _last_idle, _nudge
    _last_idle, _nudge = time.monotonic(), None
    print("Studio Sync " + VERSION + " loaded. Timed starts and smooth corrections enabled.")


def OnIdle():
    global _diagnostic_output, _last_idle, _pending, _waiting
    now = time.monotonic()
    if _last_idle and now - _last_idle > BUSY_GAP:
        # Back from being busy (an export, a dialog). Anything queued meanwhile is stale.
        print("Studio Sync: FL was busy for %.1f s; resuming." % (now - _last_idle))
        _pending = _waiting = None
        end_nudge()
    _last_idle = now
    if _nudge is not None and _nudge["ended"] is None and now >= _nudge["until"]:
        end_nudge()
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
                if "shift" in command:
                    set_song_pos(max(0.0, song_pos() + command["shift"]))
                else:
                    set_song_pos(command["pos"])
            _last = snapshot()
            return
        set_song_pos(command["pos"])
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


def nudge(p):
    """Move a few ms toward the room. FL has no reliable way to play a little
    faster: transport.setPlaybackSpeed jumped to 4x in some versions, and
    nudging the tempo shows as a BPM change on screen. So the shift the app asks
    for, (speed - 1) x length, is made as one tiny move on the next bar line,
    relative to wherever the playhead is then, so timing jitter can't add error."""
    global _pending
    try:
        speed, length = float(p.get("speed", 1)), float(p.get("ms", 0))
    except (TypeError, ValueError):
        return
    if not (0.97 <= speed <= 1.03 and 0 < length <= 30000) or not transport.isPlaying():
        return
    if _pending is not None or _waiting is not None or _tempo_target is not None:
        return
    shift = (speed - 1.0) * length / 1000.0
    if abs(shift) < 0.002:
        return
    bpm = tempo_bpm()
    bar = bar_beats() * 60.0 / bpm
    pos = song_pos()
    wait = bar - (pos % bar)
    if wait < 0.03:
        wait += bar
    if wait > 4.0:
        wait = 0.0  # Very slow tempo: don't wait several seconds for a bar line.
    _pending = dict(seek=True, shift=shift, due=time.monotonic() + wait)

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
        if busy():
            # FL is exporting or otherwise busy: never touch the transport now.
            if p.get("kind") == "ping":
                _last_ping = time.monotonic()
            return
        if p.get("kind") == "nudge":
            nudge(p)
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
        if not 10 <= bpm <= 522 or not math.isfinite(pos) or pos < 0:
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
        end_nudge()
        if transport.getLoopMode() != mode:
            transport.setLoopMode()
        if abs(tempo_bpm() - bpm) > 0.005:
            set_tempo(bpm)
        current = snapshot()
        if playing and delay > 0:
            if current["playing"]:
                transport.globalTransport(midi.FPT_Play, 1, getattr(event, "pmeflags", midi.PME_System))
            set_song_pos(pos)
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
            set_song_pos(pos)
        if toggle:
            # FPT_Play is the documented play/pause command, preserving pause.
            flags = getattr(event, "pmeflags", midi.PME_System)
            transport.globalTransport(midi.FPT_Play, 1, flags)
            set_song_pos(pos)
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
