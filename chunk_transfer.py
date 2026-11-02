"""Bounded-memory file receipt; incomplete files never appear as completed items."""

import hashlib
import os
from pathlib import Path
import secrets
import threading
import time

MAX_FILE = 500 * 1024 * 1024
CHUNK = 2 * 1024 * 1024
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {p + str(i) for p in ("COM", "LPT") for i in range(1, 10)}
EXTENSIONS = {".mid", ".midi", ".wav", ".mp3", ".aif", ".aiff", ".flac", ".ogg", ".m4a", ".zip"}


def validate(name, size):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 160
        or any(c in name for c in "/\\:")
        or any(ord(c) < 32 for c in name)
        or name.endswith((".", " "))
        or name.startswith(".")
    ):
        raise ValueError("Invalid filename.")
    if Path(name).stem.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        raise ValueError("That filename can't be saved on Windows. Rename the file and send it again.")
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError("Choose a ZIP, MIDI or audio file.")
    if type(size) is not int or not 0 < size <= MAX_FILE:
        raise ValueError("File limit is 500 MB.")


def available_path(folder, name):
    """The real filename, or 'name (2).ext' when that name is taken."""
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


class Receipt:
    def __init__(self, folder, name, size):
        validate(name, size)
        self.name = name
        self.size = size
        self.offset = 0
        self.digest = hashlib.sha256()
        self.at = time.monotonic()
        self.lock = threading.Lock()
        self.ident = secrets.token_hex(16)
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.temp = self.folder / ("." + self.ident + ".part")
        self.file = self.temp.open("xb")

    def write(self, offset, raw):
        with self.lock:
            if self.file.closed:
                raise ValueError("Transfer cancelled.")
            if (
                type(offset) is not int
                or offset != self.offset
                or not 0 < len(raw) <= CHUNK
                or self.offset + len(raw) > self.size
            ):
                raise ValueError("Invalid chunk position or size.")
            self.file.write(raw)
            self.digest.update(raw)
            self.offset += len(raw)
            self.at = time.monotonic()

    def finish(self, digest):
        with self.lock:
            if self.offset != self.size or not secrets.compare_digest(self.digest.hexdigest(), str(digest)):
                raise ValueError("File integrity check failed.")
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            path = available_path(self.folder, self.name)
            self.temp.replace(path)
            return dict(id=self.ident, name=path.name, size=self.size, direction="Received", path=str(path))

    def cancel(self):
        with self.lock:
            self.file.close()
            self.temp.unlink(missing_ok=True)
