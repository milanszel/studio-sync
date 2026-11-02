"""LAN file/frame transfers between every laptop in the room, separate from MIDI timing."""

import collections
import hashlib
import http.client
import io
import json
from pathlib import Path
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from chunk_transfer import Receipt, CHUNK, validate
from protocol import NonceGuard, request_auth

# Largest JPEG frame or control message accepted from another laptop.
MAX_FRAME = 3 * 1024 * 1024
AUTH_HEADER = "X-Room-Auth"
# Files received at the same time (one per sender is typical).
MAX_RECEIPTS = 4
# Keep this much disk free after a receive.
DISK_RESERVE = 1024**3
# Partner View: frame size, quality and pacing.
FRAME_SIZE = (1920, 1200)
FRAME_INTERVAL = 0.25
FRAME_INTERVAL_PLAYING = 0.5
FRAME_KEEPALIVE = 2.0
FRAME_TTL = 4.0


class Peer:
    """One keep alive HTTP connection to another laptop."""

    def __init__(self, ip, port, name):
        self.ip, self.port, self.name = ip, port, name
        self.conn = None
        self.lock = threading.Lock()

    def post(self, key, path, payload, timeout):
        headers = {
            AUTH_HEADER: request_auth(key, "POST", path, payload),
            "Content-Length": str(len(payload)),
            "Content-Type": "application/octet-stream",
        }
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPConnection(self.ip, self.port, timeout=timeout)
                    self.conn.timeout = timeout
                    if self.conn.sock is not None:
                        self.conn.sock.settimeout(timeout)
                    self.conn.request("POST", path, body=payload, headers=headers)
                    response = self.conn.getresponse()
                    body = response.read()
                    if response.status != 200:
                        raise ValueError(
                            self.name + " rejected the transfer: " + (response.reason or str(response.status))
                        )
                    return body
                except (http.client.HTTPException, ConnectionError, OSError) as exc:
                    self.close()
                    if attempt:
                        raise ValueError("Couldn't reach " + self.name + ": " + str(exc))
                    # A kept alive connection may have been closed by the other side; retry once fresh.
                    headers[AUTH_HEADER] = request_auth(key, "POST", path, payload)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None


class Sharing:
    def __init__(self, bridge, folder=None):
        self.bridge = bridge
        self.nonces = NonceGuard()
        self.folder = Path(folder or Path.home() / "Downloads" / "Studio-Sync-Received")
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.items = collections.deque(maxlen=40)
        self.message = ""
        # Partner View frames by sender address: ip -> (jpeg, received_at, name)
        self.frames = {}
        self.viewing = False
        self.view_error = ""
        self.view_thread = None
        self.local_frame = None
        self.local_at = 0
        self.slots = threading.BoundedSemaphore(12)
        self.receipts = {}
        self.sending = None
        self.peers = {}
        self.server = None
        if bridge is None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # Keep alive for frames and chunks.

            def log_message(self, *args):
                pass

            def fail(self, code, message=""):
                message = " ".join(str(message).split())
                body = message.encode("utf-8", "replace")[:300]
                self.send_response(code, message[:100] or None)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.connection.settimeout(120)
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    length = -1
                route = self.path.split("?")[0]
                limit = CHUNK if route == "/chunk" else MAX_FRAME
                if not 0 < length <= limit:
                    self.close_connection = True
                    self.fail(400, "Invalid transfer size.")
                    return
                payload = self.rfile.read(length)
                if len(payload) != length:
                    self.close_connection = True
                    return
                ip = self.client_address[0]
                if not owner.peer_ok(ip) or not owner.signed(
                    self.path, payload, self.headers.get(AUTH_HEADER, "")
                ):
                    self.fail(403, "Not in this room.")
                    return
                if not owner.slots.acquire(False):
                    self.fail(429, "Busy.")
                    return
                try:
                    owner.route(route, self.path, payload, ip)
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"OK")
                except Exception as exc:
                    self.fail(400, str(exc))
                finally:
                    owner.slots.release()

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.server.daemon_threads = True
        self.bridge.share_port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    @property
    def key(self):
        """The room secret, once this laptop has joined (it can change if the host restarts)."""
        return getattr(self.bridge, "key", None)

    # ---- who is in the room ------------------------------------------------

    def targets(self):
        b = self.bridge
        return b.share_targets(time.monotonic()) if b is not None else []

    def peer_ok(self, ip):
        return not self.done.is_set() and any(ip == t[0] for t in self.targets())

    def name_for(self, ip):
        return next((t[2] for t in self.targets() if t[0] == ip), ip)

    def signed(self, path, body, header):
        return bool(self.key) and self.nonces.check(self.key, "POST", path, body, header, time.time())

    def peer(self, ip, port, name):
        with self.lock:
            peer = self.peers.get((ip, port))
            if peer is None:
                peer = self.peers[(ip, port)] = Peer(ip, port, name)
            peer.name = name
            return peer

    def live_peers(self):
        targets = self.targets()
        if not targets:
            raise ValueError("Nobody else is connected with this version yet.")
        return [self.peer(*t) for t in targets]

    # ---- incoming ------------------------------------------------------------

    def route(self, route, path, payload, ip):
        if route == "/transfer-start":
            self.begin_receive(json.loads(payload), ip)
        elif route == "/chunk":
            from urllib.parse import parse_qs, urlsplit

            q = parse_qs(urlsplit(path).query)
            self.receive_chunk(q.get("id", [""])[0], int(q.get("offset", ["-1"])[0]), payload, ip)
        elif route == "/transfer-finish":
            self.finish_receive(json.loads(payload), ip)
        elif route == "/transfer-cancel":
            self.cancel_receive(json.loads(payload).get("id"), ip)
        elif route == "/frame":
            self.receive_frame(payload, ip)
        elif route == "/view-off":
            with self.lock:
                self.frames.pop(ip, None)
        else:
            raise ValueError("Unknown transfer.")

    def begin_receive(self, p, ip):
        validate(p.get("name"), p.get("size"))
        ident = p.get("id")
        if not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("Invalid transfer ID.")
        self.folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.folder).free < p["size"] + DISK_RESERVE:
            raise ValueError("Not enough free disk space on the receiving laptop.")
        with self.lock:
            if self.done.is_set():
                raise ValueError("Session closed.")
            if ident in self.receipts:
                raise ValueError("Transfer already started.")
            if len(self.receipts) >= MAX_RECEIPTS:
                raise ValueError("Too many files arriving at once. Try again shortly.")
            receipt = Receipt(self.folder, p["name"], p["size"])
            receipt.sender = ip
            sender = p.get("from")
            receipt.from_name = str(sender)[:40] if isinstance(sender, str) and sender else self.name_for(ip)
            self.receipts[ident] = receipt
            self.message = "Receiving " + p["name"] + " from " + receipt.from_name + "…"

    def receipt_for(self, ident, ip):
        with self.lock:
            receipt = self.receipts.get(ident)
            if receipt is None or receipt.sender != ip:
                raise ValueError("Transfer unavailable.")
            return receipt

    def receive_chunk(self, ident, offset, raw, ip):
        # Disk writes happen outside the shared lock so status polling never waits on them.
        self.receipt_for(ident, ip).write(offset, raw)

    def finish_receive(self, p, ip):
        receipt = self.receipt_for(p.get("id"), ip)
        with self.lock:
            self.receipts.pop(p.get("id"), None)
        try:
            item = receipt.finish(p.get("sha256"))
        except Exception:
            receipt.cancel()
            raise
        item["from"] = receipt.from_name
        with self.lock:
            self.items.appendleft(item)
            self.message = "Received " + item["name"] + " from " + receipt.from_name

    def cancel_receive(self, ident, ip):
        with self.lock:
            receipt = self.receipts.get(ident)
            if receipt is None or receipt.sender != ip:
                return
            self.receipts.pop(ident)
            self.message = "Transfer cancelled."
        receipt.cancel()

    # ---- outgoing files (to everyone) --------------------------------------

    def start_send(self, name, size):
        validate(name, size)
        peers = self.live_peers()
        with self.lock:
            if self.sending:
                raise ValueError("A file is already being sent.")
            ident = secrets.token_hex(16)
            job = self.sending = dict(
                id=ident, name=name, size=size, bytes=0, digest=hashlib.sha256(), peers=[], failed=[]
            )
        body = json.dumps(
            dict(id=ident, name=name, size=size, **{"from": getattr(self.bridge, "name", "")})
        ).encode()
        for peer in peers:
            try:
                peer.post(self.key, "/transfer-start", body, 10)
                job["peers"].append(peer)
            except ValueError as exc:
                job["failed"].append(str(exc))
        if not job["peers"]:
            with self.lock:
                self.sending = None
            raise ValueError("; ".join(job["failed"]) or "Nobody could receive the file.")
        self.message = "Sending " + name + " to " + ", ".join(p.name for p in job["peers"]) + "…"
        return ident

    def send_chunk(self, ident, offset, raw):
        with self.lock:
            job = self.sending
            if (
                not job
                or job["id"] != ident
                or offset != job["bytes"]
                or not 0 < len(raw) <= CHUNK
                or offset + len(raw) > job["size"]
            ):
                raise ValueError("Invalid transfer chunk.")
        path = "/chunk?id=" + ident + "&offset=" + str(offset)
        for peer in list(job["peers"]):
            try:
                peer.post(self.key, path, raw, 120)
            except ValueError as exc:
                # One laptop dropping out must not stop the others' copies.
                job["peers"].remove(peer)
                job["failed"].append(str(exc))
        with self.lock:
            if self.sending is not job:
                raise ValueError("Transfer cancelled.")
            if not job["peers"]:
                self.sending = None
                raise ValueError("; ".join(job["failed"]) or "Transfer failed.")
            job["digest"].update(raw)
            job["bytes"] += len(raw)

    def finish_send(self, ident):
        with self.lock:
            job = self.sending
            if not job or job["id"] != ident or job["bytes"] != job["size"]:
                raise ValueError("Incomplete transfer.")
        body = json.dumps(dict(id=ident, sha256=job["digest"].hexdigest())).encode()
        delivered = []
        for peer in job["peers"]:
            try:
                peer.post(self.key, "/transfer-finish", body, 30)
                delivered.append(peer.name)
            except ValueError as exc:
                job["failed"].append(str(exc))
        with self.lock:
            self.sending = None
            if delivered:
                self.items.appendleft(
                    dict(
                        id=ident,
                        name=job["name"],
                        size=job["size"],
                        direction="Sent",
                        to=", ".join(delivered),
                    )
                )
            note = (" Not delivered: " + "; ".join(job["failed"])) if job["failed"] else ""
            self.message = (
                "Sent " + job["name"] + " to " + ", ".join(delivered) + "." if delivered else ""
            ) + note
        if not delivered:
            raise ValueError(self.message.strip() or "Transfer failed.")

    def cancel_send(self, ident):
        with self.lock:
            job = self.sending
            if not job or job["id"] != ident:
                return
            self.sending = None
            self.message = "Transfer cancelled."
        body = json.dumps(dict(id=ident)).encode()
        for peer in job["peers"]:
            try:
                peer.post(self.key, "/transfer-cancel", body, 2)
            except ValueError:
                pass

    # ---- Partner View ---------------------------------------------------------

    def receive_frame(self, raw, ip):
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            if im.width > 2560 or im.height > 1800 or im.format != "JPEG":
                raise ValueError("Invalid frame.")
            im.verify()
        with self.lock:
            self.frames[ip] = (raw, time.monotonic(), self.name_for(ip))

    def sources(self):
        now = time.monotonic()
        with self.lock:
            live = [(ip, f) for ip, f in self.frames.items() if now - f[1] < FRAME_TTL and self.peer_ok(ip)]
        return [dict(id=ip, name=f[2]) for ip, f in sorted(live, key=lambda x: -x[1][1])]

    def get_frame(self, source=None):
        now = time.monotonic()
        with self.lock:
            if source:
                frame = self.frames.get(source)
                if frame and now - frame[1] < FRAME_TTL and self.peer_ok(source):
                    return frame[0]
                return None
            fresh = [f for ip, f in self.frames.items() if now - f[1] < FRAME_TTL and self.peer_ok(ip)]
            return max(fresh, key=lambda f: f[1])[0] if fresh else None

    def state(self):
        expired = []
        now = time.monotonic()
        with self.lock:
            for ident, receipt in list(self.receipts.items()):
                if now - receipt.at > 120:
                    expired.append(self.receipts.pop(ident))
                    self.message = "A transfer stalled and was removed. Ask them to send it again."
            if self.sending:
                job = self.sending
                transfer = dict(name=job["name"], size=job["size"], bytes=job["bytes"], direction="Sending")
            elif self.receipts:
                r = max(self.receipts.values(), key=lambda r: r.at)
                transfer = dict(
                    name=r.name, size=r.size, bytes=r.offset, direction="Receiving from " + r.from_name
                )
            else:
                transfer = None
            items = [{k: v for k, v in x.items() if k != "path"} for x in self.items]
            message, viewing, local_frame = self.message, self.viewing, bool(self.local_frame)
        sources = self.sources()
        for receipt in expired:
            receipt.cancel()
        return dict(
            transfer=transfer,
            folder=str(self.folder),
            items=items,
            message=message,
            viewing=viewing,
            local_frame=local_frame,
            preview=bool(viewing and local_frame and not sources),
            view_error=self.view_error,
            frame=bool(sources),
            sources=sources,
        )

    def received(self, ident):
        with self.lock:
            return next((x for x in self.items if x["id"] == ident and x["direction"] == "Received"), None)

    def start_view(self):
        self.viewing = True
        self.start_capture()

    def local_playing(self):
        local = getattr(self.bridge, "local", None)
        return bool(local and local.get("playing"))

    def start_capture(self):
        if self.view_thread and self.view_thread.is_alive():
            return
        self.view_error = ""

        def run():
            from screen_view import capture_fl

            last_digest, last_sent = None, 0
            try:
                while self.viewing and not self.done.is_set():
                    cycle_started = time.monotonic()
                    raw = capture_fl(FRAME_SIZE)
                    with self.lock:
                        self.local_frame = raw
                        self.local_at = time.monotonic()
                    digest = hashlib.sha1(raw).digest()
                    # Skip unchanged frames (keeping viewers alive), so an idle
                    # screen doesn't load the WiFi the timing packets share.
                    if digest != last_digest or cycle_started - last_sent >= FRAME_KEEPALIVE:
                        for peer in self.targets_quietly():
                            try:
                                peer.post(self.key, "/frame", raw, 0.75)
                            except ValueError:
                                pass
                        last_digest, last_sent = digest, cycle_started
                    interval = FRAME_INTERVAL_PLAYING if self.local_playing() else FRAME_INTERVAL
                    self.done.wait(max(0, interval - (time.monotonic() - cycle_started)))
            except Exception as exc:
                self.view_error = str(exc)
            finally:
                self.viewing = False
                with self.lock:
                    self.local_frame = None
                for peer in self.targets_quietly():
                    try:
                        peer.post(self.key, "/view-off", b"{}", 1)
                    except ValueError:
                        pass

        self.view_thread = threading.Thread(target=run, daemon=True)
        self.view_thread.start()

    def targets_quietly(self):
        try:
            return self.live_peers() if self.bridge is not None else []
        except ValueError:
            return []

    def stop_view(self):
        self.viewing = False

    def close(self):
        self.done.set()
        self.viewing = False
        with self.lock:
            receipts, self.receipts = list(self.receipts.values()), {}
            self.sending = None
        for receipt in receipts:
            receipt.cancel()
        if self.bridge is not None:
            self.bridge.share_port = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.view_thread and self.view_thread is not threading.current_thread():
            self.view_thread.join(timeout=2)
        with self.lock:
            for peer in self.peers.values():
                peer.close()
            self.local_frame = None
            self.frames = {}
