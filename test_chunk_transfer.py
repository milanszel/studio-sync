import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from chunk_transfer import CHUNK, MAX_FILE, Receipt, validate
from sharing import Sharing


class LoopRoom:
    """A room whose only other laptop is this one again, over real HTTP on localhost."""

    def __init__(self, code="ROOM-CODE-TEST", name="Studio B"):
        import os

        self.code, self.name = code, name
        self.key = os.urandom(32)
        self.share_port = None
        self.local = None
        self.online = True

    def share_targets(self, now):
        return [("127.0.0.1", self.share_port, self.name)] if self.online and self.share_port else []


class ChunkTests(unittest.TestCase):
    def test_local_binary_upload_and_token(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from web_app import make_handler

        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SimpleNamespace(sharing=s), "secret"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                ident = s.start_send("a.zip", 3)
                url = f"http://127.0.0.1:{server.server_port}/api/transfer-chunk?id={ident}&offset=0"
                with self.assertRaises(Exception):
                    urllib.request.urlopen(urllib.request.Request(url, data=b"abc"))
                self.assertEqual(next(iter(s.receipts.values())).offset, 0)
                req = urllib.request.Request(url, data=b"abc", headers={"X-Studio-Token": "secret"})
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(response.status, 200)
                s.finish_send(ident)
                item = next(x for x in s.items if x["direction"] == "Received")
                self.assertEqual(Path(item["path"]).read_bytes(), b"abc")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                s.close()

    def test_validation(self):
        validate("Samples.zip", MAX_FILE)
        for name, size in [
            ("a.zip", MAX_FILE + 1),
            ("../a.zip", 1),
            ("a.exe", 1),
            ("a.zip", 0),
            ("a.zip", True),
            ("C:a.zip", 1),
            ("CON.wav", 1),
            ("aux.mid", 1),
        ]:
            with self.assertRaises(ValueError):
                validate(name, size)

    def test_offsets_integrity_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            r = Receipt(folder, "a.zip", 6)
            r.write(0, b"abc")
            with self.assertRaises(ValueError):
                r.write(0, b"abc")
            with self.assertRaises(ValueError):
                r.finish("bad")
            r.write(3, b"def")
            item = r.finish(hashlib.sha256(b"abcdef").hexdigest())
            self.assertEqual(Path(item["path"]).read_bytes(), b"abcdef")
            self.assertFalse(r.temp.exists())
            r = Receipt(folder, "a.zip", 2)
            r.write(0, b"a")
            r.cancel()
            self.assertFalse(r.temp.exists())

    def test_real_http_500_mb_transfer(self):
        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            try:
                ident = s.start_send("Large.zip", MAX_FILE)
                raw = b"x" * CHUNK
                digest = hashlib.sha256()
                for offset in range(0, MAX_FILE, CHUNK):
                    s.send_chunk(ident, offset, raw)
                    digest.update(raw)
                    self.assertEqual(next(iter(s.receipts.values())).offset, offset + CHUNK)
                s.finish_send(ident)
                item = next(x for x in s.items if x["direction"] == "Received")
                actual = hashlib.sha256()
                with Path(item["path"]).open("rb") as f:
                    while chunk := f.read(CHUNK):
                        actual.update(chunk)
                self.assertEqual(actual.digest(), digest.digest())
                self.assertEqual(Path(item["path"]).stat().st_size, MAX_FILE)
                self.assertIsNone(s.sending)
                self.assertFalse(s.receipts)
            finally:
                s.close()

    def test_cancel_and_bad_checksum_never_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            b = LoopRoom()
            s = Sharing(b, folder)
            try:
                ident = s.start_send("a.zip", 3)
                s.send_chunk(ident, 0, b"abc")
                s.cancel_send(ident)
                self.assertFalse(list(Path(folder).iterdir()))
                ident = "a" * 32
                s.begin_receive(dict(id=ident, name="a.zip", size=3), "127.0.0.1")
                s.receive_chunk(ident, 0, b"abc", "127.0.0.1")
                with self.assertRaises(ValueError):
                    s.finish_receive(dict(id=ident, sha256="bad"), "127.0.0.1")
                self.assertFalse(s.items)
                self.assertFalse(list(Path(folder).iterdir()))
                s.begin_receive(dict(id=ident, name="a.zip", size=3), "127.0.0.1")
                s.receipts[ident].at -= 121
                s.state()
                self.assertFalse(s.receipts)
            finally:
                s.close()
