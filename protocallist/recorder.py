"""The two sides of one conversation, written to disk as the sound comes.

The microphone is one stream, what is heard in the earphones is another. Each is read by
the sound library on a thread of its own, so neither waits for the other; one more thread
turns the pieces into 16 kHz mono and puts them into two files on one clock.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from protocallist.audio import RATE, Resampler, Track, WavWriter

log = logging.getLogger(__name__)

# A piece of sound as the library gives it: 16-bit frames with the channels interleaved,
# the rate and the number of channels.
Deliver = Callable[[np.ndarray, int, int], None]


class RecorderError(Exception):
    pass


class Stream(Protocol):
    def close(self) -> None: ...


class Backend(Protocol):
    """As much of a sound library as a recorder needs. `deliver` is called on the library's own thread."""

    def microphone(self, name: str, deliver: Deliver) -> Stream: ...
    def line(self, speaker: str, deliver: Deliver) -> Stream: ...
    def close(self) -> None: ...


class _Side:
    def __init__(self, path: Path) -> None:
        self.writer = WavWriter(path)
        self.track = Track(self.writer)
        self.format: tuple[int, int] | None = None
        self.resampler: Resampler | None = None

    def put(self, at: float, frames: np.ndarray, rate: int, channels: int) -> None:
        if self.format != (rate, channels):
            self.format, self.resampler = (rate, channels), Resampler(rate, channels)
        self.track.put(at, self.resampler.feed(frames))


class Recorder:
    """One conversation. start(), then release_mic()/resume_mic() as the conversation
    program lets go of the microphone and takes it again, then stop() or abort()."""

    def __init__(self, backend: Backend, microphone: str, speaker: str | None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._backend = backend
        self._microphone = microphone
        self._speaker = speaker
        self._clock = clock
        self._zero = 0.0
        self._sides: dict[str, _Side] = {}
        self._streams: dict[str, Stream] = {}
        self._pieces: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._over = False                      # the recording has ended: what still arrives is dropped
        self.failed: Exception | None = None    # why the recording could not be written on, if it could not

    def start(self, folder: Path, now: float) -> None:
        """Begin recording into `folder`; `now` is the moment the conversation began, on the recorder's clock."""
        self._zero = now
        # The microphone first: without it there is no conversation to record at all.
        try:
            self._streams["mic"] = self._backend.microphone(self._microphone,
                                                            lambda *piece: self._arrived("mic", *piece))
        except Exception:
            self._backend.close()
            raise
        self._sides = {"mic": _Side(folder / "mic.wav"), "line": _Side(folder / "line.wav")}
        self._thread = threading.Thread(target=self._write, name="recorder", daemon=True)
        self._thread.start()
        try:
            if self._speaker is None:
                raise RecorderError("no playback device to record from")
            self._streams["line"] = self._backend.line(self._speaker, lambda *piece: self._arrived("line", *piece))
        except Exception as e:
            # Half a conversation is better than none: the other side stays silent in the recording.
            log.warning("what is heard cannot be recorded (%s): only the microphone is", e)

    def _arrived(self, side: str, frames: np.ndarray, rate: int, channels: int) -> None:
        if self._over:
            return                              # a stream that would not close: nobody writes any more
        # the piece ended just now, so it began its own length ago
        at = self._clock() - len(frames) / channels / rate - self._zero
        self._pieces.put((side, at, frames, rate, channels))

    def _write(self) -> None:
        while (piece := self._pieces.get()) is not None:
            if self.failed is not None:
                continue                # nothing more is written; what is on disk stays
            side, at, frames, rate, channels = piece
            try:
                self._sides[side].put(max(at, 0.0), frames, rate, channels)
            except Exception as e:      # a full disk, most likely
                self.failed = e
                log.error("the recording cannot be written on (%s: %s): it stops here", type(e).__name__, e)

    def release_mic(self) -> None:
        """Let go of the microphone at once: a headset stays in its telephone mode for as long as anybody holds it."""
        stream = self._streams.pop("mic", None)
        if stream is not None:
            self._shut("mic", stream)

    def resume_mic(self) -> None:
        if "mic" in self._streams:
            return
        try:
            self._streams["mic"] = self._backend.microphone(self._microphone,
                                                            lambda *piece: self._arrived("mic", *piece))
        except Exception as e:
            log.warning("the microphone cannot be opened again (%s): only what is heard is recorded", e)

    @staticmethod
    def _shut(name: str, stream: Stream) -> None:
        try:
            stream.close()
        except Exception as e:                  # the device was pulled out, most likely
            log.warning("the %s stream could not be closed (%s: %s)", name, type(e).__name__, e)

    def _close(self) -> None:
        """Let go of everything, whatever refuses: a stream that will not close must not keep
        the other one recording, the writing going or the sound library open."""
        self._over = True
        for name, stream in self._streams.items():
            self._shut(name, stream)
        self._streams.clear()
        if self._thread is not None:
            self._pieces.put(None)
            self._thread.join()
            self._thread = None
        try:
            self._backend.close()
        except Exception as e:
            log.warning("the sound library could not be closed (%s: %s)", type(e).__name__, e)

    def stop(self, end: float) -> float:
        """The conversation ended at `end`, on the recorder's clock: both files get exactly
        that length. Returns the length in seconds."""
        self._close()
        seconds = max(end - self._zero, 0.0)
        for name, side in self._sides.items():
            # silence between pieces is expected in what is heard (nothing was playing) and
            # in the microphone only across a pause: more than that is sound that came late
            log.info("%s: silence was put in %d times, %.1f s in all", name, side.track.gaps,
                     side.track.gap_frames / RATE)
            try:
                side.track.pad(seconds)
                side.writer.truncate(int(round(seconds * RATE)))
            except OSError as e:
                log.error("%s.wav could not be finished (%s): what is on disk stays", name, e)
            self._shut_file(name, side)
        return seconds

    def abort(self) -> None:
        """Stop without caring what was recorded: the files are about to be deleted."""
        self._close()
        for name, side in self._sides.items():
            self._shut_file(name, side)

    @staticmethod
    def _shut_file(name: str, side: _Side) -> None:
        """Close a file whatever happened before: left open, it cannot be deleted or read by others."""
        try:
            side.writer.close()
        except OSError as e:
            log.error("%s.wav could not be closed (%s)", name, e)


class _PortAudioStream:
    def __init__(self, stream) -> None:
        self._stream = stream

    def close(self) -> None:
        try:
            self._stream.stop_stream()
        finally:
            self._stream.close()


class PortAudio:
    """The real sound library. It learns the devices of the system when it is made, so one
    is made for every conversation: a headset plugged in a minute ago must be there."""

    def __init__(self) -> None:
        import pyaudiowpatch

        self._pa = pyaudiowpatch
        self._audio = pyaudiowpatch.PyAudio()
        self._wasapi = self._audio.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)["index"]

    def _open(self, device: dict, deliver: Deliver) -> Stream:
        rate, channels = int(device["defaultSampleRate"]), int(device["maxInputChannels"])

        def callback(data, frames, when, status):
            deliver(np.frombuffer(data, dtype=np.int16), rate, channels)
            return None, self._pa.paContinue

        return _PortAudioStream(self._audio.open(
            format=self._pa.paInt16, channels=channels, rate=rate, input=True, input_device_index=device["index"],
            frames_per_buffer=rate // 10, stream_callback=callback))

    def microphone(self, name: str, deliver: Deliver) -> Stream:
        for index in range(self._audio.get_device_count()):
            device = self._audio.get_device_info_by_index(index)
            if (device["hostApi"] == self._wasapi and device["maxInputChannels"] > 0
                    and not device.get("isLoopbackDevice") and device["name"] == name):
                return self._open(device, deliver)
        raise RecorderError(f"the sound library sees no microphone named {name!r}")

    def line(self, speaker: str, deliver: Deliver) -> Stream:
        # what plays into a device can be recorded from its twin, which the library names "<device> [Loopback]"
        for device in self._audio.get_loopback_device_info_generator():
            if device["name"].startswith(speaker):
                return self._open(device, deliver)
        raise RecorderError(f"the sound library cannot record what plays into {speaker!r}")

    def close(self) -> None:
        self._audio.terminate()
