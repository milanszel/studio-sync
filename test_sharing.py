import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from sharing import Sharing, AUTH_HEADER
import os
from protocol import request_auth
from test_chunk_transfer import LoopRoom


class Room:
    """A laptop whose share targets are set by the test."""

    KEY = os.urandom(32)  # Everyone in one test room shares the room secret.

    def __init__(self, code="ROOM-CODE-TEST"):
        self.code = code
        self.key = Room.KEY
        self.share_port = None
        self.local = None
        self.targets = []

    def share_targets(self, now):
        return list(self.targets)


def raw_post(port, path, body, header):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers={AUTH_HEADER: header} if header else {})
        return conn.getresponse().status
    finally:
        conn.close()


class SharingTests(unittest.TestCase):
    def test_requests_need_signature_membership_and_no_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            try:
                port = b.share_port
                body = json.dumps(dict(id="a" * 32, name="a.zip", size=3)).encode()
                path = "/transfer-start"
                self.assertEqual(raw_post(port, path, body, None), 403)
                wrong = request_auth(os.urandom(32), "POST", path, body)
                self.assertEqual(raw_post(port, path, body, wrong), 403)
                good = request_auth(s.key, "POST", path, body)
                self.assertEqual(raw_post(port, path, body, good), 200)
                # The exact same signed request again is a replay.
                self.assertEqual(raw_post(port, path, body, good), 403)
                stale = request_auth(s.key, "POST", path, body).split(":", 1)[1]
                self.assertEqual(raw_post(port, path, body, str(int(time.time()) - 600) + ":" + stale), 403)
                b.online = False
                fresh = request_auth(s.key, "POST", path, body)
                self.assertEqual(raw_post(port, path, body, fresh), 403)
            finally:
                s.close()

    def test_received_files_keep_their_names(self):
        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            try:
                for _ in range(2):
                    ident = s.start_send("Pattern 1.mid", 4)
                    s.send_chunk(ident, 0, b"MThd")
                    s.finish_send(ident)
                names = sorted(p.name for p in Path(folder).iterdir())
                self.assertEqual(names, ["Pattern 1 (2).mid", "Pattern 1.mid"])
                received = [x for x in s.items if x["direction"] == "Received"]
                self.assertEqual(received[0]["name"], "Pattern 1 (2).mid")
                self.assertEqual(received[0]["from"], "Studio B")
            finally:
                s.close()

    def test_file_goes_to_everyone_even_if_one_leaves(self):
        with tempfile.TemporaryDirectory() as root:
            folders = [Path(root) / n for n in ("a", "b", "c")]
            rooms = [Room() for _ in folders]
            shares = [Sharing(r, f) for r, f in zip(rooms, folders)]
            try:
                sender, first, second = rooms
                sender.targets = [
                    ("127.0.0.1", first.share_port, "First"),
                    ("127.0.0.1", second.share_port, "Second"),
                ]
                first.targets = second.targets = [("127.0.0.1", sender.share_port, "Sender")]
                ident = shares[0].start_send("Loop.wav", 6)
                shares[0].send_chunk(ident, 0, b"RIF")
                shares[2].close()  # Second laptop leaves mid transfer.
                shares[0].send_chunk(ident, 3, b"F00")
                shares[0].finish_send(ident)
                self.assertEqual((folders[1] / "Loop.wav").read_bytes(), b"RIFF00")
                self.assertIn("Not delivered", shares[0].message)
                self.assertIn("First", shares[0].items[0]["to"])
            finally:
                for s in shares:
                    s.close()

    def test_frames_from_several_laptops(self):
        from PIL import Image
        import io

        def jpeg(color):
            out = io.BytesIO()
            Image.new("RGB", (32, 20), color).save(out, format="JPEG")
            return out.getvalue()

        with tempfile.TemporaryDirectory() as folder:
            b = Room()
            s = Sharing(b, folder)
            try:
                b.targets = [("10.0.0.2", 1, "Ann"), ("10.0.0.3", 1, "Ben")]
                s.receive_frame(jpeg("red"), "10.0.0.2")
                time.sleep(0.01)
                s.receive_frame(jpeg("blue"), "10.0.0.3")
                self.assertEqual([x["name"] for x in s.sources()], ["Ben", "Ann"])
                self.assertEqual(s.get_frame(), s.get_frame("10.0.0.3"))
                self.assertNotEqual(s.get_frame("10.0.0.2"), s.get_frame("10.0.0.3"))
                b.targets = [("10.0.0.2", 1, "Ann")]
                self.assertIsNone(s.get_frame("10.0.0.3"))
                self.assertTrue(s.state()["frame"])
            finally:
                s.close()

    def test_frame_expiry_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            b = Room()
            s = Sharing(b, folder)
            try:
                self.assertIsNone(s.get_frame())
                from unittest.mock import patch

                with patch.object(s, "start_capture") as capture:
                    s.start_view()
                    capture.assert_called_once()
                self.assertTrue(s.viewing)
                s.local_frame = b"preview"
                self.assertTrue(s.state()["preview"])
                s.stop_view()
                self.assertFalse(s.viewing)
            finally:
                s.close()

    def test_status_polling_not_blocked_by_chunk_write(self):
        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            try:
                ident = "b" * 32
                s.begin_receive(dict(id=ident, name="a.zip", size=3), "127.0.0.1")
                receipt = s.receipts[ident]
                entered, release = threading.Event(), threading.Event()
                original = receipt.write

                def slow_write(offset, raw):
                    entered.set()
                    release.wait(2)
                    original(offset, raw)

                receipt.write = slow_write
                worker = threading.Thread(target=s.receive_chunk, args=(ident, 0, b"abc", "127.0.0.1"))
                worker.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                s.state()
                self.assertLess(time.monotonic() - started, 0.5)
                release.set()
                worker.join(2)
                self.assertEqual(s.state()["transfer"]["bytes"], 3)
            finally:
                s.close()

    def test_disk_space_is_checked(self):
        from unittest.mock import patch
        import shutil

        with tempfile.TemporaryDirectory() as folder:
            s = Sharing(LoopRoom(), folder)
            try:
                with patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(0, 0, 10)):
                    with self.assertRaisesRegex(ValueError, "disk space"):
                        s.begin_receive(dict(id="c" * 32, name="a.zip", size=3), "127.0.0.1")
            finally:
                s.close()

    def test_digest_matches_after_fanout(self):
        with tempfile.TemporaryDirectory() as folder:
            s = Sharing(LoopRoom(), folder)
            try:
                data = bytes(range(256)) * 10
                ident = s.start_send("Kit.zip", len(data))
                s.send_chunk(ident, 0, data)
                s.finish_send(ident)
                item = next(x for x in s.items if x["direction"] == "Received")
                self.assertEqual(
                    hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                )
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
