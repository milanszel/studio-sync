# PyInstaller recipe for Studio Sync on macOS and Windows.
# Used by Build-Mac-App.command and Build-Windows-Installer.bat; run on the
# system you are building for (PyInstaller can't build for another system).
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "web_app.py").read_text(encoding="utf-8"), re.M).group(1)

datas = [
    (str(ROOT / "interface.html"), "."),
    (str(ROOT / "partner_window.html"), "."),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "FL-Script" / "Studio-Sync" / "device_studio_sync.py"), "FL-Script/Studio-Sync"),
]
# Loaded by name at runtime, so PyInstaller can't see them on its own.
hidden = ["mido.backends.rtmidi", "rtmidi", "spake2", "audio_key", "screen_view", "sharing", "chunk_transfer"]
if sys.platform == "darwin":
    hidden += ["mac_capture", "objc", "Foundation", "AppKit", "WebKit", "Quartz", "CoreMedia", "ScreenCaptureKit"]
    icon = str(ROOT / "build" / "icon.icns")
elif sys.platform == "win32":
    hidden += ["windows_capture", "pyaudiowpatch", "clr"]
    icon = str(ROOT / "packaging" / "icon.ico")
else:
    icon = None

a = Analysis(
    [str(ROOT / "web_app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Studio Sync",
    console=False,
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Studio Sync", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Studio Sync.app",
        icon=icon,
        bundle_identifier="com.wemadeit.studiosync",
        version=VERSION,
        info_plist={
            "CFBundleName": "Studio Sync",
            "CFBundleDisplayName": "Studio Sync",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "14.0",
            "LSApplicationCategoryType": "public.app-category.music",
            "NSHighResolutionCapable": True,
            # macOS 15 asks before an app talks to other devices on the network.
            "NSLocalNetworkUsageDescription": "Studio Sync connects to the other laptops in your session to keep FL Studio in time and share files.",
        },
    )
