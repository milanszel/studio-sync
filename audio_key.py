"""Local, opt-in FL audio analysis. No recording or network upload."""

import collections
import queue
import sys
import threading
import time

# Windows capture records the default output device, not FL alone. ASIO output
# bypasses that device entirely, so say so instead of failing silently.
WINDOWS_NOTE = " · Windows hears all system sound on the default output. FL must use that device, not ASIO."
NO_AUDIO = "No audio · check FL output and capture settings"

NAMES = ("C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B")


def chroma(samples, rate):
    import numpy as np

    x = np.asarray(samples, dtype=float)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) < 2048 or np.sqrt(np.mean(x * x)) < 0.0003:
        return None
    n = 8192
    total = np.zeros(12)
    for start in range(0, max(1, len(x) - n + 1), n // 2):
        frame = x[start : start + n]
        if len(frame) < n:
            frame = np.pad(frame, (0, n - len(frame)))
        mag = np.abs(np.fft.rfft(frame * np.hanning(n)))
        freq = np.fft.rfftfreq(n, 1 / rate)
        peaks = np.where((mag[1:-1] > mag[:-2]) & (mag[1:-1] > mag[2:]))[0] + 1
        peaks = peaks[(freq[peaks] > 55) & (freq[peaks] < 2500)]
        if not len(peaks):
            continue
        peaks = peaks[mag[peaks] > mag.max() * 0.06]
        if not len(peaks):
            continue
        # Interpolate spectral peaks to reduce FFT-bin pitch error.
        logmag = np.log(mag + 1e-12)
        delta = (
            0.5
            * (logmag[peaks - 1] - logmag[peaks + 1])
            / (logmag[peaks - 1] - 2 * logmag[peaks] + logmag[peaks + 1] + 1e-12)
        )
        midi = 69 + 12 * np.log2((peaks + np.clip(delta, -0.5, 0.5)) * rate / n / 440)
        # Reduce overtone votes when a stronger lower fundamental is present.
        # Vectorized: Python loops here held the GIL and delayed the sync thread.
        f = freq[peaks]
        m = mag[peaks]
        stronger = m[None, :] > m[:, None] * 0.6
        overtone = np.zeros(len(peaks), dtype=bool)
        for h in (2, 3, 4, 5):
            target = f / h
            near = np.abs(f[None, :] - target[:, None]) < np.maximum(4, target * 0.015)[:, None]
            overtone |= (near & stronger).any(axis=1)
        salience = np.where(overtone, 0.25, 1.0)
        close = np.abs(midi - np.rint(midi)) < 0.3
        np.add.at(total, np.rint(midi[close]).astype(int) % 12, np.sqrt(mag[peaks[close]]) * salience[close])
    return total / total.sum() if total.sum() else None


def estimate(c):
    import numpy as np

    profiles = [
        ("major", [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]),
        ("minor", [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]),
    ]
    c = np.asarray(c)
    if np.count_nonzero(c > c.max() * 0.15) < 3:
        return dict(key="Need more notes", alternative="", strength="Uncertain")
    ranked = []
    for mode, p in profiles:
        scales = (
            ([0, 2, 4, 5, 7, 9, 11],) if mode == "major" else ([0, 2, 3, 5, 7, 8, 10], [0, 2, 3, 5, 7, 8, 11])
        )
        for root in range(12):
            correlation = float(np.corrcoef(c, np.roll(p, root))[0, 1])
            outside = min(sum(c[i] for i in range(12) if (i - root) % 12 not in scale) for scale in scales)
            ranked.append((correlation - 2 * float(outside), NAMES[root] + " " + mode, correlation))
    ranked.sort(reverse=True)
    a, b = ranked[:2]
    clear = a[0] > 0.25 and a[0] - b[0] > 0.06 and a[2] > 0.5
    return dict(
        key=a[1] if clear else "Uncertain",
        alternative=b[1] if clear else a[1] + " / " + b[1],
        strength="Clearer estimate" if a[0] > 0.55 and a[0] - b[0] > 0.15 else "Tentative",
    )


class AudioKey:
    def __init__(self):
        self.lock = threading.RLock()
        self.stream = None
        self.worker = None
        self.done = None
        self.result = dict(
            listening=False, key="—", alternative="", strength="", message="Listen to FL audio", level=0
        )

    def state(self):
        with self.lock:
            return dict(self.result)

    def stop(self):
        if self.done:
            self.done.set()
        stream, self.stream = self.stream, None
        if stream:
            try:
                stream.stop()
            finally:
                pass
        if self.worker:
            self.worker.join(timeout=2)
        with self.lock:
            self.result.update(listening=False, level=0, message="Listening stopped")

    def start(self):
        self.stop()
        if sys.platform == "darwin":
            from mac_capture import MacCapture as Capture
        elif sys.platform == "win32":
            from windows_capture import WindowsCapture as Capture
        else:
            raise ValueError("Audio detection requires Mac or Windows.")
        inbox = queue.Queue(maxsize=8)
        done = threading.Event()
        self.done = done

        def callback(data, rate):
            try:
                inbox.put_nowait((data, rate))
            except queue.Full:
                pass

        def error(message):
            done.set()
            with self.lock:
                self.result.update(
                    listening=False, key="—", alternative="", strength="", level=0, message=message
                )

        def work():
            import numpy as np

            history = collections.deque(maxlen=60)
            last_sound = time.monotonic()
            candidate = None
            repeats = 0
            try:
                while not done.is_set():
                    try:
                        data, rate = inbox.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    level = float(np.sqrt(np.mean(data * data)))
                    c = chroma(data, rate)
                    with self.lock:
                        self.result["level"] = min(1, level * 8)
                        if c is not None:
                            last_sound = time.monotonic()
                            history.append(c)
                            guess = (
                                estimate(np.mean(history, axis=0))
                                if len(history) >= 20
                                else dict(key="Listening…", alternative="", strength="Gathering notes")
                            )
                            if guess["key"] == candidate:
                                repeats += 1
                            else:
                                candidate = guess["key"]
                                repeats = 1
                            if repeats >= 4 or guess["key"] in ("Uncertain", "Listening…", "Need more notes"):
                                self.result.update(guess)
                            else:
                                self.result.update(
                                    key="Uncertain", alternative=guess["key"], strength="Checking consistency"
                                )
                            self.result["message"] = "Likely key · play several bars of melody or chords" + (
                                WINDOWS_NOTE if sys.platform == "win32" else ""
                            )
                        elif time.monotonic() - last_sound > 2:
                            history.clear()
                            candidate = None
                            repeats = 0
                            self.result.update(
                                key="—",
                                alternative="",
                                strength="",
                                message=NO_AUDIO + (WINDOWS_NOTE if sys.platform == "win32" else ""),
                            )
            except Exception as exc:
                with self.lock:
                    self.result.update(listening=False, message="Analysis stopped: " + str(exc))
                done.set()

        try:
            # SCK sends small buffers; accumulate half a second before analysis.
            chunks = []
            count = 0

            def collect(data, rate):
                nonlocal chunks, count
                chunks.append(data)
                count += len(data)
                if count >= rate // 2:
                    import numpy as np

                    callback(np.concatenate(chunks), rate)
                    chunks = []
                    count = 0

            self.stream = Capture(collect, error)
            with self.lock:
                self.result.update(
                    listening=True,
                    key="Listening…",
                    alternative="",
                    strength="",
                    message="Waiting for FL audio" + (WINDOWS_NOTE if sys.platform == "win32" else ""),
                )
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()
            self.stream.start()
        except Exception:
            self.stop()
            raise
