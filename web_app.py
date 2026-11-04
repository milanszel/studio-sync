"""Local browser interface for Studio Sync. Network MIDI stays in Bridge."""

import collections
import hmac
import re
import platform
import json
import queue
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from audio_key import AudioKey
from sharing import Sharing
from chunk_transfer import CHUNK
import mido
from bridge import Bridge
from discovery import find_hosts
from protocol import FL_PROTOCOL, check_code, new_code

# Bundled data (interface pages, FL script) lives next to the code, or in
# PyInstaller's unpack folder when running as an installed app.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
FROZEN = bool(getattr(sys, "frozen", False))
VERSION = "0.10.1"


def write_json_atomic(path, data):
    """Write to a temp file, then swap it in, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        temp.write_text(json.dumps(data), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Controller:
    def __init__(self, ports=None):
        self.audio_key = AudioKey()
        self.auto_listen = False
        self.audio_monitor_done = threading.Event()
        self.audio_monitor = None
        self.sharing = None
        self.ports = ports
        self.bridge = None
        self.lock = threading.RLock()
        self.settings_path = Path.home() / ".studio-sync" / "settings.json"
        try:
            self.settings = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            self.settings = {}
        if not isinstance(self.settings, dict):
            self.settings = {}
        self.settings = {
            **dict(
                role="Host",
                address="",
                code=new_code(),
                start_wait=400,
                start_mode="together",
                audio_delay=0,
                to_fl="Studio Sync To FL",
                from_fl="Studio Sync From FL",
            ),
            **self.settings,
        }
        try:
            check_code(self.settings["code"])
            # 0.8 made long XXXX-XXXX-XXXX codes; 0.9 doesn't need them.
            if re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", str(self.settings["code"])):
                self.settings["code"] = new_code()
        except ValueError:
            self.settings["code"] = new_code()
        self.current = {}
        self.partner_window = None
        self.app_url = None
        self.history = collections.deque(maxlen=500)
        self.last_health = None
        self.record("App opened")

    def start_auto_listen(self):
        self.auto_listen = True
        if self.audio_monitor and self.audio_monitor.is_alive():
            return

        def monitor():
            # Restart listening if capture stops (FL closed and reopened, etc).
            while not self.audio_monitor_done.wait(10):
                self.ensure_audio_listening()

        self.audio_monitor = threading.Thread(target=monitor, daemon=True)
        self.audio_monitor.start()

    def ensure_audio_listening(self):
        with self.lock:
            if (
                self.audio_monitor_done.is_set()
                or not self.auto_listen
                or self.audio_key.state()["listening"]
            ):
                return
            try:
                self.audio_key.start()
            except Exception as exc:
                with self.audio_key.lock:
                    self.audio_key.result.update(listening=False, message=str(exc))

    def close_audio(self):
        self.audio_monitor_done.set()
        with self.lock:
            self.auto_listen = False
            self.audio_key.stop()
        if self.audio_monitor:
            self.audio_monitor.join(timeout=2)

    def buffer_hint(self):
        """FL reports its playhead in steps of its audio buffer. Laptops with very
        different buffer sizes end up a constant few ms apart."""
        mine = self.current.get("step_ms")
        others = [(p["name"], p["step_ms"]) for p in self.current.get("people") or [] if p.get("step_ms")]
        if not mine or not others:
            return ""
        far = [(n, ms) for n, ms in others if abs(ms - mine) > 8]
        if not far:
            return ""
        listed = ", ".join("%s about %d ms" % (n, ms) for n, ms in far)
        return (
            "Audio buffer sizes differ (this laptop about %d ms, %s). Set the same buffer size in FL's "
            "Audio settings on every laptop, or fine tune with Earlier and Later." % (mine, listed)
        )

    def save_settings(self):
        write_json_atomic(self.settings_path, self.settings)

    def record(self, message):
        self.history.append(dict(time=time.strftime("%Y-%m-%dT%H:%M:%S%z"), message=message))

    def disconnect(self):
        if self.sharing:
            self.sharing.close()
            self.sharing = None
        if self.bridge:
            self.bridge.close()
            self.record("Disconnected")
        self.bridge = None
        self.current = {}

    def state(self):
        if self.bridge:
            self.bridge.audio_key = self.audio_key.state()
        with self.lock:
            if self.bridge:
                while True:
                    try:
                        event = self.bridge.events.get_nowait()
                        if isinstance(event, dict):
                            self.current = event
                        else:
                            self.current["error"] = str(event)
                            self.record(str(event))
                    except queue.Empty:
                        break
            running = bool(self.bridge and self.bridge.thread.is_alive())
            health = (
                running,
                bool(self.current.get("fl")),
                bool(self.current.get("peer")),
                self.current.get("error", ""),
            )
            if health != self.last_health:
                self.record(dict(running=health[0], fl=health[1], peer=health[2], error=health[3]))
                self.last_health = health
            if self.bridge and not running:
                hint = "Connection stopped. Disconnect, then connect again."
            elif not self.bridge:
                hint = "Choose Host or Join, then connect. Your last room and MIDI ports are saved."
            elif not self.current.get("fl"):
                hint = "No FL response. Enable the selected input and output in FL MIDI settings, assign both port 42, and choose Studio Sync Transport for the input."
            elif not self.current.get("people"):
                hint = "FL is responding. Open Studio Sync on the other laptops and join this room."
            elif self.current.get("fl_protocol", 0) < FL_PROTOCOL:
                hint = "Your FL script is out of date. Use Install in Settings, then click Update MIDI scripts in FL."
            elif any(x.get("problem") for x in self.current.get("people") or []):
                hint = " ".join(
                    x["name"] + ": " + x["problem"] for x in self.current["people"] if x.get("problem")
                )
            elif self.buffer_hint():
                hint = self.buffer_hint()
            elif self.current.get("outdated"):
                hint = "Update the FL script on: " + ", ".join(self.current["outdated"]) + "."
            elif not self.current.get("peer"):
                hint = "Connected. Waiting for FL on the other laptops."
            else:
                count = len(self.current.get("people") or []) + 1
                hint = "All %d laptops are in sync." % count
            return dict(
                sharing=self.sharing.state() if self.sharing else {},
                audio_key=self.audio_key.state(),
                settings=dict(self.settings),
                connected=self.bridge is not None,
                running=running,
                status=dict(self.current),
                hint=hint,
            )

    def dialog(self, kind, **kwargs):
        import webview

        names = {"folder": ("FOLDER", "FOLDER_DIALOG"), "save": ("SAVE", "SAVE_DIALOG")}[kind]
        mode = (
            getattr(webview.FileDialog, names[0])
            if hasattr(webview, "FileDialog")
            else getattr(webview, names[1])
        )
        result = self.window.create_file_dialog(mode, **kwargs)
        if not result:
            return None
        return Path(result[0] if isinstance(result, (tuple, list)) else result)

    def action(self, action, data):
        # Native dialogs block until closed; keep them outside the lock so
        # status polling and the sync bridge keep running meanwhile.
        if action == "choose-save-folder":
            folder = self.dialog("folder")
            with self.lock:
                if folder:
                    if not folder.is_dir():
                        raise ValueError("Choose an existing folder.")
                    self.settings["save_folder"] = str(folder)
                    self.save_settings()
                    if self.sharing:
                        self.sharing.folder = folder
                return dict(folder=self.settings.get("save_folder", ""))
        if action == "save-file-as":
            with self.lock:
                item = self.sharing.received(data.get("id")) if self.sharing else None
            if not item:
                raise ValueError("Received file unavailable.")
            dest = self.dialog("save", save_filename=item["name"])
            if dest and dest.resolve() != Path(item["path"]).resolve():
                temp = dest.parent / (".studio-sync-save-" + secrets.token_hex(8) + ".part")
                try:
                    shutil.copyfile(item["path"], temp)
                    temp.replace(dest)
                finally:
                    temp.unlink(missing_ok=True)
            return dict()
        if action == "discover":
            # Network wait; no shared state involved.
            return dict(hosts=find_hosts())
        if action in ("transfer-start", "transfer-finish", "transfer-cancel"):
            # Network transfers run without the controller lock.
            sharing = self.sharing
            if not sharing:
                raise ValueError("Connect a session first.")
            if action == "transfer-start":
                return dict(id=sharing.start_send(data.get("name"), data.get("size")))
            if action == "transfer-finish":
                sharing.finish_send(data.get("id"))
            else:
                sharing.cancel_send(data.get("id"))
            return self.state()
        with self.lock:
            if action == "pop-out":
                import webview

                if self.partner_window:
                    self.partner_window.show()
                else:
                    if not self.app_url:
                        raise ValueError("Desktop window is not ready.")
                    window = webview.create_window(
                        "Studio Sync · Partner View",
                        self.app_url + "/partner",
                        width=560,
                        height=400,
                        min_size=(360, 260),
                        on_top=True,
                        background_color="#f3f5f9",
                    )
                    self.partner_window = window

                    def closed():
                        if self.partner_window is window:
                            self.partner_window = None

                    window.events.closed += closed
                return dict()
            elif action == "reveal-file":
                if not self.sharing:
                    raise ValueError("No active shared files.")
                item = self.sharing.received(data.get("id"))
                if not item:
                    raise ValueError("Received file unavailable.")
                import subprocess

                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", item["path"]])
                elif sys.platform == "win32":
                    subprocess.Popen(["explorer.exe", "/select," + item["path"]])
                else:
                    raise ValueError("Show in folder requires Mac or Windows.")
            elif action in ("start-view", "stop-view"):
                if not self.sharing:
                    self.sharing = Sharing(None, self.settings.get("save_folder"))
                if action == "start-view":
                    self.sharing.start_view()
                else:
                    self.sharing.stop_view()
            elif action == "listen":
                self.settings["listen"] = True
                self.save_settings()
                self.audio_key.start()
                self.start_auto_listen()
            elif action == "stop-listen":
                self.settings["listen"] = False
                self.save_settings()
                self.auto_listen = False
                self.audio_key.stop()
            elif action == "disconnect":
                self.disconnect()
            elif action == "offset":
                # Earlier/Later on the main screen: works while playing; the
                # sync engine glides to the new spot.
                delay = float(self.settings.get("audio_delay", 0)) + float(data.get("change_ms", 0))
                delay = max(-250.0, min(250.0, round(delay)))
                self.settings["audio_delay"] = delay
                if self.bridge:
                    self.bridge.audio_delay = delay / 1000
                self.save_settings()
            elif action == "new-code":
                if self.bridge:
                    raise ValueError("Leave the session before changing the room code.")
                self.settings["code"] = new_code()
                self.save_settings()
            elif action in ("connect", "timing"):
                wait = float(data.get("start_wait", 400))
                delay = float(data.get("audio_delay", 0))
                if not 100 <= wait <= 1000 or not -250 <= delay <= 250:
                    raise ValueError("Start wait: 100–1,000 ms. Offset: -250 to 250 ms.")
                if action == "connect":
                    role = data.get("role", "Host")
                    address = str(data.get("address", "")).strip()
                    code = str(data.get("code", "")).strip()
                    if role not in ("Host", "Join"):
                        raise ValueError("Choose Host or Join.")
                    if role == "Join" and not address:
                        raise ValueError("Find a host or enter its address.")
                    check_code(code)
                    self.disconnect()
                    to_fl = str(data.get("to_fl", self.settings["to_fl"]))
                    from_fl = str(data.get("from_fl", self.settings["from_fl"]))
                    if not self.ports:
                        if to_fl not in mido.get_output_names() or from_fl not in mido.get_input_names():
                            raise ValueError(
                                "Selected MIDI ports are unavailable. Open your MIDI port app, refresh ports in Setup, and select both ports."
                            )
                    self.bridge = Bridge(
                        role == "Host",
                        address,
                        code,
                        to_fl,
                        from_fl,
                        False,
                        shared_ports=self.ports,
                        start_mode=self.settings.get("start_mode", "together"),
                    )
                    try:
                        self.sharing = Sharing(self.bridge, self.settings.get("save_folder"))
                    except Exception:
                        self.disconnect()
                        raise
                    self.settings.update(role=role, address=address, code=code, to_fl=to_fl, from_fl=from_fl)
                    self.record("Connected as " + role)
                mode = data.get("start_mode", self.settings.get("start_mode", "together"))
                if mode not in ("together", "instant"):
                    raise ValueError("Choose a start mode.")
                if self.bridge:
                    self.bridge.start_wait = wait / 1000
                    self.bridge.audio_delay = delay / 1000
                    if self.bridge.host:
                        self.bridge.start_mode = mode
                self.settings.update(start_wait=wait, audio_delay=delay, start_mode=mode)
                self.save_settings()
            elif action == "export":
                snapshot = self.state()
                safe_settings = {k: v for k, v in self.settings.items() if k not in ("code", "address")}
                report = dict(
                    app_version=VERSION,
                    platform=platform.platform(),
                    python=sys.version,
                    settings=safe_settings,
                    running=snapshot["running"],
                    status=snapshot["status"],
                    history=list(self.history),
                    note="Clock offset is host minus Join monotonic clock, not audible latency. corrections counts hard seeks made to fix drift. Room code and host address omitted.",
                )
                folder = Path.home() / "Downloads"
                folder.mkdir(parents=True, exist_ok=True)
                name = (
                    "Studio-Sync-Diagnostics-"
                    + time.strftime("%Y%m%d-%H%M%S")
                    + "-"
                    + secrets.token_hex(2)
                    + ".json"
                )
                target = folder / name
                target.write_text(json.dumps(report, indent=2), encoding="utf-8")
                return dict(message="Diagnostic log saved to " + str(target))
            elif action == "install":
                folder = Path(str(data.get("folder", ""))).expanduser()
                if not folder.is_dir() or not str(data.get("folder", "")).strip():
                    raise ValueError("Enter your existing FL Studio user data folder.")
                dest = folder / "Settings" / "Hardware" / "Studio-Sync"
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / "device_studio_sync.py"
                if target.exists():
                    shutil.copy2(target, target.with_suffix(".py.backup"))
                shutil.copy2(ROOT / "FL-Script" / "Studio-Sync" / "device_studio_sync.py", target)
                return dict(message="Script installed. Restart FL Studio or update MIDI scripts.")
            elif action == "ports":
                return dict(
                    inputs=["Studio Sync From FL"] if self.ports else mido.get_input_names(),
                    outputs=["Studio Sync To FL"] if self.ports else mido.get_output_names(),
                )
            else:
                raise ValueError("Unknown action.")
            return self.state()


def make_handler(controller, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, ctype="application/json"):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, suffix):
            # Byte ranges let the audio player seek; WebKit may refuse to play without them.
            mime = {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".m4a": "audio/mp4",
                ".flac": "audio/flac",
                ".aif": "audio/aiff",
                ".aiff": "audio/aiff",
            }.get(suffix, "application/octet-stream")
            size = path.stat().st_size
            start, end = 0, size - 1
            ranged = self.headers.get("Range", "")
            if ranged:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", ranged.strip())
                if not match or (not match[1] and not match[2]):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def token_ok(self, value):
            return hmac.compare_digest(str(value or ""), token)

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.allowed_host():
                self.reply(403, "{}")
                return
            if self.path == "/":
                self.reply(
                    200,
                    (ROOT / "interface.html").read_text().replace("__TOKEN__", token),
                    "text/html; charset=utf-8",
                )
            elif self.path.split("?")[0] in ("/api/frame", "/api/local-frame", "/api/file"):
                from urllib.parse import parse_qs, urlsplit

                query = parse_qs(urlsplit(self.path).query)
                if not self.token_ok(query.get("token", [""])[0]):
                    self.reply(403, "{}")
                    return
                sharing = controller.sharing
                if not sharing:
                    self.reply(404, "{}")
                    return
                if self.path.startswith(("/api/frame", "/api/local-frame")):
                    raw = (
                        sharing.local_frame
                        if self.path.startswith("/api/local-frame")
                        else sharing.get_frame(query.get("source", [""])[0] or None)
                    )
                    if raw:
                        # The viewer asks with the tag of the frame it shows; an unchanged
                        # frame costs nothing and creates no new image in the window.
                        import hashlib

                        tag = '"%s"' % hashlib.sha1(raw).hexdigest()[:16]
                        if self.headers.get("If-None-Match") == tag:
                            self.send_response(304)
                            self.send_header("ETag", tag)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                        else:
                            self.send_response(200)
                            self.send_header("Content-Type", "image/jpeg")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("ETag", tag)
                            self.send_header("Content-Length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw)
                    else:
                        self.reply(404, "{}")
                else:
                    item = sharing.received(query.get("id", [""])[0])
                    if item:
                        self.send_file(Path(item["path"]), Path(item["name"]).suffix.lower())
                    else:
                        self.reply(404, "{}")
            elif self.path == "/partner":
                self.reply(
                    200,
                    (ROOT / "partner_window.html").read_text().replace("__TOKEN__", token),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/api/state" and self.token_ok(self.headers.get("X-Studio-Token")):
                self.reply(200, json.dumps(controller.state()))
            else:
                self.reply(404, "{}")

        def do_POST(self):
            if not self.allowed_host():
                self.reply(403, "{}")
                return
            if not self.token_ok(self.headers.get("X-Studio-Token")):
                self.reply(403, "{}")
                return
            try:
                if self.path.startswith("/api/transfer-chunk?"):
                    from urllib.parse import parse_qs, urlsplit

                    q = parse_qs(urlsplit(self.path).query)
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= CHUNK:
                        raise ValueError("Invalid chunk size.")
                    self.connection.settimeout(120)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete chunk.")
                    sharing = controller.sharing
                    if not sharing:
                        raise ValueError("Session closed.")
                    sharing.send_chunk(q.get("id", [""])[0], int(q.get("offset", ["-1"])[0]), raw)
                    self.reply(200, "{}")
                    return
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 16384:
                    raise ValueError("Invalid request size.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Invalid request.")
                result = controller.action(self.path.removeprefix("/api/"), data)
                self.reply(200, json.dumps(result))
            except Exception as exc:
                self.reply(400, json.dumps(dict(error=str(exc))))

    return Handler


INSTANCE_PORT = 42613


def single_instance():
    """Hold a local port for the app's lifetime; a second copy can't bind it.
    Two copies would fight over the MIDI ports and the session port."""
    import socket

    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind(("127.0.0.1", INSTANCE_PORT))
        lock.listen(1)
    except OSError:
        lock.close()
        return None
    return lock


def setup_logging():
    """An installed app has no terminal: keep output in ~/.studio-sync/studio-sync.log."""
    if not FROZEN and sys.stdout is not None:
        return
    folder = Path.home() / ".studio-sync"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "studio-sync.log"
    if log.exists() and log.stat().st_size > 2_000_000:
        log.replace(folder / "studio-sync.old.log")
    stream = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
    stream.write("\n--- Studio Sync %s started %s ---\n" % (VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
    sys.stdout = sys.stderr = stream


def self_test(result_path):
    """Run by the build scripts on the built app: every module and bundled file
    the app needs at runtime must load. Writes OK or the failures to a file,
    because a windowed app has no console."""
    problems = []
    for name in ("interface.html", "partner_window.html", "FL-Script/Studio-Sync/device_studio_sync.py"):
        if not (ROOT / name).is_file():
            problems.append("missing bundled file " + name)
    modules = [
        "numpy",
        "PIL.Image",
        "webview",
        "rtmidi",
        "mido.backends.rtmidi",
        "bridge",
        "sharing",
        "audio_key",
    ]
    if sys.platform == "darwin":
        modules += [
            "objc",
            "Foundation",
            "ScreenCaptureKit",
            "CoreMedia",
            "Quartz",
            "mac_capture",
            "screen_view",
        ]
    elif sys.platform == "win32":
        modules += ["pyaudiowpatch", "windows_capture", "screen_view"]
    import importlib

    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            problems.append("cannot import %s: %s" % (name, exc))
    try:
        mido.set_backend("mido.backends.rtmidi", load=True)
        mido.get_output_names()
    except Exception as exc:
        problems.append("MIDI backend: %s" % exc)
    try:
        from protocol import pake_join, pake_host

        state, message = pake_join("ABC123")
        reply, key = pake_host("ABC123", message)
        if state.finish(reply) != key:
            raise ValueError("keys differ")
    except Exception as exc:
        problems.append("room code handshake unavailable: %s" % exc)
    text = "OK %s\n" % VERSION if not problems else "FAILED\n" + "\n".join(problems) + "\n"
    Path(result_path).write_text(text, encoding="utf-8")
    return 0 if not problems else 1


def main():
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else "studio-sync-self-test.txt"
        sys.exit(self_test(target))
    setup_logging()
    import webview

    ports = None
    server = None
    controller = None
    thread = None
    instance = single_instance()
    if instance is None:
        print("Studio Sync is already open. Use the window that is already running.", flush=True)
        return
    try:
        # Prepare MIDI before creating any native window.
        if sys.platform == "darwin":
            output = mido.open_output("Studio Sync To FL", virtual=True)
            try:
                ports = (output, mido.open_input("Studio Sync From FL", virtual=True))
            except BaseException:
                output.close()
                raise
        # Hand the GIL over sooner so the sync thread isn't held up by other work.
        sys.setswitchinterval(0.001)
        controller = Controller(ports)
        if controller.settings.get("listen"):
            # Listening is opt in; resume only if it was on last time.
            controller.start_auto_listen()
            controller.ensure_audio_listening()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, secrets.token_urlsafe(32)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        controller.app_url = url
        print("Studio Sync " + VERSION + " is running. Close its window to quit.", flush=True)
        controller.window = webview.create_window(
            "Studio Sync", url, width=600, height=820, min_size=(480, 650), background_color="#e5e6ef"
        )

        def close_partner():
            window = controller.partner_window
            if window:
                window.destroy()

        controller.window.events.closed += close_partner
        webview.start()
    finally:
        if server:
            if thread:
                server.shutdown()
                thread.join(timeout=2)
            server.server_close()
        if controller:
            controller.close_audio()
            controller.disconnect()
        if ports:
            for port in ports:
                port.close()
        instance.close()


if __name__ == "__main__":
    main()
