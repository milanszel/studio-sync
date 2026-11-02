"""WASAPI loopback input. Captures the default Windows output, not a mic."""

import threading


class WindowsCapture:
    def __init__(self, on_audio, on_error):
        self.on_audio = on_audio
        self.on_error = on_error
        self.done = threading.Event()
        self.thread = None

    def start(self):
        import pyaudiowpatch as pa
        import numpy as np

        def run():
            audio = None
            stream = None
            try:
                audio = pa.PyAudio()
                device = audio.get_default_wasapi_loopback()
                rate = int(device["defaultSampleRate"])
                channels = int(device["maxInputChannels"])
                if not device.get("isLoopbackDevice") or channels < 1:
                    raise RuntimeError("No Windows output loopback device available.")
                stream = audio.open(
                    format=pa.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=1024,
                )
                while not self.done.is_set():
                    raw = stream.read(1024, exception_on_overflow=False)
                    self.on_audio(np.frombuffer(raw, dtype=np.float32).reshape(-1, channels).copy(), rate)
            except Exception as exc:
                if not self.done.is_set():
                    self.on_error("Windows audio capture stopped: " + str(exc))
            finally:
                if stream:
                    try:
                        stream.stop_stream()
                    finally:
                        stream.close()
                if audio:
                    audio.terminate()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.done.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
