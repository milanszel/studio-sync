"""Opt-in capture of the FL main window only; never whole-desktop fallback."""

import io
import sys
import threading
import time
from native_memory import native_pool

PERMISSION = "Allow Screen Recording for Terminal/Python in System Settings, then reopen Studio Sync."
_mac_target = {"filter": None, "config": None, "until": 0, "max": (1920, 1200)}


def _is_fl(name, bundle=""):
    return "fl studio" in str(name).lower() or "flstudio" in str(bundle).lower()


def _cgimage_to_pil(cg):
    import Quartz as Q
    from PIL import Image

    width = Q.CGImageGetWidth(cg)
    height = Q.CGImageGetHeight(cg)
    raw = Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(cg))
    return Image.frombytes(
        "RGBA", (width, height), bytes(raw), "raw", "BGRA", Q.CGImageGetBytesPerRow(cg), 1
    ).convert("RGB")


def _wait(start, timeout=3):
    """Run an async ScreenCaptureKit call and wait for its completion handler."""
    done = threading.Event()
    box = {}

    def finish(value=None, error=None):
        box["value"], box["error"] = value, error
        done.set()

    start(finish)
    if not done.wait(timeout):
        raise ValueError("Screen capture timed out. Is FL Studio responding?")
    if box["error"]:
        raise ValueError(PERMISSION + " " + str(box["error"]))
    return box["value"]


def _mac_filter():
    """Find the FL main window. Cached briefly; window lookup is slower than capture."""
    import ScreenCaptureKit as SC

    now = time.monotonic()
    if _mac_target["filter"] is not None and now < _mac_target["until"]:
        return _mac_target["filter"], _mac_target["config"]

    content = _wait(
        lambda finish: SC.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, True, lambda content, error: finish(content, error)
        )
    )
    windows = []
    for w in content.windows() or []:
        app = w.owningApplication()
        if app is not None and _is_fl(app.applicationName(), app.bundleIdentifier()) and w.windowLayer() == 0:
            windows.append(w)
    if not windows:
        raise ValueError("Open the FL window and keep its piano roll visible.")
    window = max(windows, key=lambda w: w.frame().size.width * w.frame().size.height)
    filt = SC.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    scale = float(filt.pointPixelScale()) if hasattr(filt, "pointPixelScale") else 2.0
    width, height = window.frame().size.width * scale, window.frame().size.height * scale
    # Capture at the size we send, not full Retina size: a 5K window is 60 MB a frame.
    fit = min(1.0, _mac_target["max"][0] / width, _mac_target["max"][1] / height)
    config = SC.SCStreamConfiguration.alloc().init()
    config.setWidth_(max(2, int(width * fit)))
    config.setHeight_(max(2, int(height * fit)))
    config.setShowsCursor_(False)
    _mac_target.update(filter=filt, config=config, until=now + 2)
    return filt, config


@native_pool
def _capture_mac(max_size):
    import objc
    import ScreenCaptureKit as SC

    if not hasattr(SC, "SCScreenshotManager"):
        raise ValueError("Partner View needs macOS 14 Sonoma or later.")
    if _mac_target["max"] != tuple(max_size):
        _mac_target.update(max=tuple(max_size), until=0)
    filt, config = _mac_filter()

    def handled(finish):
        def handler(image, error):
            # Runs on a ScreenCaptureKit thread. Turn the frame into JPEG bytes
            # right here, inside its own autorelease pool, so no native image
            # memory outlives this call (it used to pile up frame after frame).
            with objc.autorelease_pool():
                try:
                    if error is not None or image is None:
                        finish(None, error or PERMISSION)
                    else:
                        finish(_jpeg(_cgimage_to_pil(image), max_size), None)
                except Exception as exc:
                    finish(None, exc)

        return handler

    try:
        return _wait(
            lambda finish: SC.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                filt, config, handled(finish)
            )
        )
    except ValueError:
        _mac_target["until"] = 0  # Window may have closed or moved; look it up again.
        raise


def _capture_windows():
    import ctypes
    from ctypes import wintypes
    from PIL import ImageGrab

    u = ctypes.windll.user32
    windows = []
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, unused):
        title = ctypes.create_unicode_buffer(1024)
        u.GetWindowTextW(hwnd, title, 1024)
        if u.IsWindowVisible(hwnd) and "fl studio" in title.value.lower():
            windows.append(hwnd)
        return True

    u.EnumWindows(callback(visit), 0)
    if not windows:
        raise ValueError("Open FL Studio and keep its piano roll visible.")
    return ImageGrab.grab(window=windows[0]).convert("RGB")


def _jpeg(im, max_size):
    im.thumbnail(max_size)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=85)
    im.close()
    return out.getvalue()


@native_pool
def capture_fl(max_size=(1280, 900)):
    """One JPEG of the FL window, at most max_size."""
    if sys.platform == "darwin":
        return _capture_mac(max_size)
    if sys.platform == "win32":
        return _jpeg(_capture_windows(), max_size)
    raise ValueError("Window sharing requires Mac or Windows.")
