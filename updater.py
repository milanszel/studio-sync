"""Checks for a newer Studio Sync, downloads it and installs it in one click.

Installers are published to a public GitHub repo (RELEASES_REPO) with a
latest.json that names the newest version; the code repo stays private.
Each download is checked against the SHA-256 in latest.json before it runs.

latest.json:
    {"version": "0.12.0", "notes": "...",
     "files": {"windows": {"name": ..., "url": ..., "size": ..., "sha256": ...},
               "mac-arm64": {...}, "mac-x86_64": {...}}}
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

RELEASES_REPO = "milanszel/studio-sync-releases"
LATEST_URL = "https://raw.githubusercontent.com/%s/main/latest.json" % RELEASES_REPO
CHECK_EVERY = 6 * 3600
USER_AGENT = "StudioSync-Updater"


def parse_version(text):
    numbers = re.findall(r"\d+", str(text))[:3]
    return tuple(int(n) for n in numbers) + (0,) * (3 - len(numbers)) if numbers else (0, 0, 0)


def real_mac_arch():
    """arm64 on Apple silicon even when this app runs under Rosetta."""
    try:
        out = subprocess.run(
            ["sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if out == "1":
            return "arm64"
    except (OSError, subprocess.SubprocessError):
        pass
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def pick_asset(files, system=None, arch=None):
    system = system or sys.platform
    if system.startswith("win"):
        return files.get("windows")
    if system == "darwin":
        arch = arch or real_mac_arch()
        order = [arch] + (["x86_64"] if arch == "arm64" else [])  # Intel builds run on Apple silicon.
        for wanted in order:
            if files.get("mac-" + wanted):
                return files["mac-" + wanted]
    return None


def app_bundle():
    """/Applications/Studio Sync.app when running from an installed Mac app."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


class Updater:
    def __init__(self, version, frozen, folder=None, opener=None):
        self.version = version
        self.frozen = frozen
        self.folder = Path(folder or Path.home() / ".studio-sync" / "updates")
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=20))
        self.lock = threading.Lock()
        self.latest = None  # dict(version, notes, asset)
        self.status = "idle"  # idle, checking, available, downloading, installing, error, current
        self.progress = 0.0
        self.message = ""
        self.last_check = 0.0
        self.thread = None

    # ---- checking ------------------------------------------------------------

    def state(self):
        with self.lock:
            latest = self.latest
            return dict(
                status=self.status,
                current=self.version,
                latest=latest["version"] if latest else None,
                notes=latest["notes"] if latest else "",
                progress=round(self.progress, 3),
                message=self.message,
                can_install=self.frozen and latest is not None,
            )

    def start(self):
        def loop():
            while True:
                if self.status not in ("downloading", "installing"):
                    self.check()
                time.sleep(CHECK_EVERY)

        threading.Thread(target=loop, daemon=True).start()

    def check(self):
        with self.lock:
            self.status, self.message = "checking", ""
        try:
            req = urllib.request.Request(
                LATEST_URL + "?t=%d" % (time.time() // 300), headers={"User-Agent": USER_AGENT}
            )
            with self.open(req) as response:
                release = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            with self.lock:
                self.status, self.message = "idle", "Couldn't check for updates: %s" % exc
            return
        self.last_check = time.time()
        version = str(release.get("version", ""))
        asset = pick_asset(release.get("files") or {})
        with self.lock:
            if asset and parse_version(version) > parse_version(self.version):
                self.latest = dict(version=version, notes=release.get("body") or "", asset=asset)
                self.status = "available"
            else:
                self.latest = None
                self.status = "current"

    # ---- installing ----------------------------------------------------------

    def install(self):
        with self.lock:
            if self.latest is None:
                raise ValueError("No update available.")
            if not self.frozen:
                raise ValueError("Updates install into the installed app. You're running from source.")
            if self.status in ("downloading", "installing"):
                return
            self.status, self.progress, self.message = "downloading", 0.0, ""
            asset = self.latest["asset"]
        self.thread = threading.Thread(target=self.run_install, args=(asset,), daemon=True)
        self.thread.start()

    def run_install(self, asset):
        try:
            path = self.download(asset)
            with self.lock:
                self.status = "installing"
            if sys.platform.startswith("win"):
                self.install_windows(path)
            elif sys.platform == "darwin":
                self.install_mac(path)
            else:
                raise ValueError("Updates aren't supported on this system.")
        except Exception as exc:
            with self.lock:
                self.status, self.message = "error", "Update failed: %s" % exc

    def download(self, asset):
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.iterdir():
            if old.is_file():
                old.unlink(missing_ok=True)
            else:
                shutil.rmtree(old, ignore_errors=True)
        name = Path(asset["name"]).name
        target = self.folder / name
        partial = target.with_suffix(target.suffix + ".part")
        size = int(asset.get("size") or 0)
        digest = hashlib.sha256()
        req = urllib.request.Request(asset["url"], headers={"User-Agent": USER_AGENT})
        done = 0
        with self.open(req) as response, open(partial, "wb") as out:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if size:
                    with self.lock:
                        self.progress = min(1.0, done / size)
        if size and done != size:
            raise ValueError("download incomplete (%d of %d bytes)" % (done, size))
        if str(asset.get("sha256", "")).lower() != digest.hexdigest():
            partial.unlink(missing_ok=True)
            raise ValueError("the download was corrupted. Try again.")
        os.replace(partial, target)
        return target

    def install_windows(self, path):
        # The installer ends this app itself, installs (Windows asks for
        # permission once), updates the FL script and opens the new version.
        # "start" goes through ShellExecute, so Windows can ask for admin
        # permission, and the installer is not a child of this app (the
        # installer ends Studio Sync and everything it started).
        subprocess.Popen(
            'cmd /c start "" "%s" /SILENT /SUPPRESSMSGBOXES /NORESTART' % path,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )
        with self.lock:
            self.message = "Installing. Studio Sync will reopen in a moment."
        threading.Timer(1.5, lambda: os._exit(0)).start()

    def install_mac(self, path):
        bundle = app_bundle()
        if bundle is None:
            raise ValueError("couldn't find the installed app")
        mount = Path(tempfile.mkdtemp(prefix="studio-sync-dmg-"))
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-quiet", "-mountpoint", str(mount), str(path)],
            check=True,
            timeout=120,
        )
        try:
            source = next(mount.glob("*.app"), None)
            if source is None:
                raise ValueError("the download has no app in it")
            staged = self.folder / "Studio Sync.app"
            shutil.rmtree(staged, ignore_errors=True)
            subprocess.run(["ditto", str(source), str(staged)], check=True, timeout=300)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], timeout=60)
        if not os.access(bundle.parent, os.W_OK):
            subprocess.run(["open", str(path)])
            raise ValueError("no permission to replace %s. The installer is open: drag Studio Sync to Applications." % bundle)
        # A helper swaps the app once this one has quit, then opens it.
        script = self.folder / "swap.sh"
        script.write_text(
            """#!/bin/bash
while kill -0 %(pid)d 2>/dev/null; do sleep 0.2; done
rm -rf "%(old)s.previous"
mv "%(old)s" "%(old)s.previous" && ditto "%(new)s" "%(old)s" && rm -rf "%(old)s.previous" || {
  rm -rf "%(old)s"; mv "%(old)s.previous" "%(old)s"
}
xattr -dr com.apple.quarantine "%(old)s" 2>/dev/null
open "%(old)s"
"""
            % dict(pid=os.getpid(), old=bundle, new=staged)
        )
        script.chmod(0o755)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True, close_fds=True)
        with self.lock:
            self.message = "Installing. Studio Sync will reopen in a moment."
        threading.Timer(0.5, lambda: os._exit(0)).start()
