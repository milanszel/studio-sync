"""ScreenCaptureKit FL-only audio input; native imports are lazy."""

import threading
from native_memory import native_pool

_OUTPUT_CLASS = None


def audio_format(asbd):
    # PyObjC may expose AudioStreamBasicDescription as a tuple rather than
    # an object with named fields. Never index a named struct to dereference it.
    def field(name, index):
        return getattr(asbd, name) if hasattr(asbd, name) else asbd[index]

    return tuple(
        int(field(name, index))
        for name, index in (
            ("mSampleRate", 0),
            ("mFormatID", 1),
            ("mFormatFlags", 2),
            ("mChannelsPerFrame", 6),
            ("mBitsPerChannel", 7),
        )
    )


class MacCapture:
    def __init__(self, on_audio, on_error):
        self.on_audio = on_audio
        self.on_error = on_error
        self.lock = threading.RLock()
        self.closed = False
        self.stream = None
        self.output = None

    @native_pool
    def start(self):
        import objc
        import ScreenCaptureKit as SC
        import CoreMedia as CM
        from Foundation import NSObject
        import numpy as np

        global _OUTPUT_CLASS
        if _OUTPUT_CLASS is None:

            class StudioAudioOutput(
                NSObject,
                protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")],
            ):
                def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
                    with objc.autorelease_pool():
                        owner = self.owner
                        if owner is None or owner.closed or kind != SC.SCStreamOutputTypeAudio:
                            return
                        try:
                            block = CM.CMSampleBufferGetDataBuffer(sample)
                            if block is None:
                                return
                            count = CM.CMSampleBufferGetNumSamples(sample)
                            fmt = CM.CMSampleBufferGetFormatDescription(sample)
                            asbd = CM.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
                            if asbd is None or count <= 0:
                                return
                            size = CM.CMBlockBufferGetDataLength(block)
                            status, raw = CM.CMBlockBufferCopyDataBytes(block, 0, size, None)
                            if status:
                                raise RuntimeError("Cannot copy audio buffer: " + str(status))
                            rate, format_id, flags, channels, bits = audio_format(asbd)
                            if format_id != 0x6C70636D or not flags & 1 or bits not in (32, 64):
                                raise RuntimeError("Unexpected audio format")
                            dtype = (">" if flags & 2 else "<") + ("f4" if bits == 32 else "f8")
                            values = np.frombuffer(raw, dtype=dtype)
                            if len(values) != count * channels:
                                raise RuntimeError("Unexpected audio buffer size")
                            data = (
                                values.reshape(channels, count).T
                                if flags & 32
                                else values.reshape(count, channels)
                            )
                            owner.on_audio(data.copy(), rate)
                        except Exception as exc:
                            owner.fail(str(exc))

                def stream_didStopWithError_(self, stream, error):
                    owner = self.owner
                    if owner is not None and not owner.closed:
                        owner.fail(str(error))

            _OUTPUT_CLASS = StudioAudioOutput
        self.output = _OUTPUT_CLASS.alloc().init()
        self.output.owner = self

        @native_pool
        def content_ready(content, error):
            try:
                if error:
                    raise RuntimeError(
                        "Allow Screen Recording for Terminal/Python in System Settings → Privacy & Security, then quit and reopen Studio Sync. "
                        + str(error)
                    )
                apps = [
                    a
                    for a in content.applications()
                    if "fl studio" in str(a.applicationName()).lower()
                    or "flstudio" in str(a.bundleIdentifier()).lower()
                ]
                if not apps:
                    raise RuntimeError("Open FL Studio before clicking Listen.")
                displays = content.displays()
                if not displays:
                    raise RuntimeError("No display available for audio capture.")
                filt = SC.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                    displays[0], apps, []
                )
                config = SC.SCStreamConfiguration.alloc().init()
                config.setCapturesAudio_(True)
                config.setExcludesCurrentProcessAudio_(True)
                config.setSampleRate_(48000)
                config.setChannelCount_(2)
                config.setWidth_(2)
                config.setHeight_(2)
                stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(filt, config, self.output)
                ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self.output, SC.SCStreamOutputTypeAudio, None, None
                )
                if not ok:
                    raise RuntimeError(str(err))
                with self.lock:
                    if self.closed:
                        return
                    self.stream = stream
                    stream.startCaptureWithCompletionHandler_(
                        lambda err: self.fail(str(err)) if err and not self.closed else None
                    )
            except Exception as exc:
                self.fail(str(exc))

        SC.SCShareableContent.getShareableContentWithCompletionHandler_(content_ready)

    def fail(self, message):
        if self.closed:
            return
        self.on_error(message)
        self.stop()

    @native_pool
    def stop(self):
        with self.lock:
            self.closed = True
            stream, self.stream = self.stream, None
            output, self.output = self.output, None
            if stream:
                # Keep the delegate alive until asynchronous capture shutdown.
                @native_pool
                def stopped(error):
                    if output is not None:
                        output.owner = None

                stream.stopCaptureWithCompletionHandler_(stopped)
            elif output is not None:
                output.owner = None
