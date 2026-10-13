"""Sound the way the recogniser takes it - 16 kHz, mono - in a file that survives being cut off,
on a clock that both sides of a conversation share."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

RATE = 16000

# ---- any sound to 16 kHz mono ---------------------------------------------------------------------


def _lowpass(rate: int, taps: int = 63) -> np.ndarray:
    """A filter for sound at `rate` that keeps what 16 kHz can carry and removes the rest."""
    cutoff = 0.45 * RATE / rate                 # as a share of `rate`
    k = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * k) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class Resampler:
    """A stream of 16-bit frames at any rate and channel count becomes mono at 16 kHz.
    Fed piece by piece it gives the same sound as fed at once."""

    def __init__(self, rate: int, channels: int) -> None:
        self._channels = channels
        self._same = rate == RATE
        self._step = rate / RATE
        self._taps = _lowpass(rate) if rate > RATE else None
        self._tail = np.zeros(0 if self._taps is None else len(self._taps) - 1, dtype=np.float32)
        self._hold = np.zeros(0, dtype=np.float32)      # filtered sound the output has not reached yet
        self._at = 0.0                                  # where in _hold the next output sample lies

    def feed(self, frames: np.ndarray) -> np.ndarray:
        mono = np.asarray(frames, dtype=np.int16).reshape(-1, self._channels).astype(np.float32).mean(axis=1) / 32768
        if self._same or not len(mono):
            return mono
        if self._taps is not None:
            joined = np.concatenate([self._tail, mono])
            self._tail = joined[len(joined) - (len(self._taps) - 1):]
            mono = np.convolve(joined, self._taps, mode="valid")
        hold = np.concatenate([self._hold, mono])
        # an output sample is taken between two neighbours, so the last one of `hold` waits for the next piece
        count = int(np.ceil((len(hold) - 1 - self._at) / self._step)) if len(hold) - 1 > self._at else 0
        where = self._at + self._step * np.arange(count)
        left = where.astype(np.int64)
        part = (where - left).astype(np.float32)
        out = hold[left] * (1 - part) + hold[left + 1] * part
        after = self._at + self._step * count
        keep = min(int(after), len(hold))
        self._hold, self._at = hold[keep:], after - keep
        return out.astype(np.float32)


# ---- a recording on disk ---------------------------------------------------------------------------

_HEADER = 44


def _header(frames: int) -> bytes:
    size = min(frames * 2, 0xFFFFFFFF - 36)     # a day and a half of sound: the header says as much as it can
    return (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) + b"data" + struct.pack("<I", size))


def silence(frames: int):
    """`frames` of silence as sound to be written, a second at a time: an hour of nothing -
    a program holds the microphone and nothing plays - must not be one block in memory."""
    second = bytes(RATE * 2)
    while frames > 0:
        piece = min(frames, RATE)
        yield second if piece == RATE else second[:piece * 2]
        frames -= piece


class WavWriter:
    """A 16 kHz mono 16-bit WAV written as the sound comes. Its header is brought up to date
    about once a second, so the file a dead process left behind is still a recording."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.frames = 0
        self._noted = 0                         # how many frames the header knows of
        self._file = open(path, "w+b")
        self._file.write(_header(0))

    def write(self, samples: np.ndarray) -> None:
        self._append((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())

    def silence(self, frames: int) -> None:
        for pcm in silence(frames):
            self._append(pcm)

    def _append(self, pcm: bytes) -> None:
        self._file.write(pcm)
        self.frames += len(pcm) // 2
        if self.frames - self._noted >= RATE:
            self._note()

    def _note(self) -> None:
        self._file.seek(0)
        self._file.write(_header(self.frames))
        self._file.seek(0, 2)
        self._file.flush()
        self._noted = self.frames

    def truncate(self, frames: int) -> None:
        """Cut the recording down to its first `frames` frames."""
        if frames < self.frames:
            self.frames = max(frames, 0)
            self._file.truncate(_HEADER + self.frames * 2)
            self._note()

    def close(self) -> None:
        if not self._file.closed:
            self._note()
            self._file.close()


def repair(path: Path) -> int:
    """Make the header of a recording that was cut off agree with what the file holds;
    the number of whole frames in it."""
    frames = max(path.stat().st_size - _HEADER, 0) // 2
    with open(path, "r+b") as f:
        f.write(_header(frames))
        f.truncate(_HEADER + frames * 2)
    return frames


def read(path: Path) -> np.ndarray:
    """The sound of a recording made by WavWriter."""
    data = path.read_bytes()[_HEADER:]
    return np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768


def frames(path: Path) -> int:
    """How many frames a recording holds - by the length of its file, whatever its header says."""
    return max(path.stat().st_size - _HEADER, 0) // 2


def read_frames(path: Path, start: int, count: int) -> np.ndarray:
    """`count` frames of a recording from frame `start`, as they lie in the file (16 bit): an
    hour of conversation is never in memory at once."""
    with open(path, "rb") as f:
        f.seek(_HEADER + start * 2)
        data = f.read(count * 2)
    return np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2")


def wav(samples: np.ndarray) -> bytes:
    """A whole WAV file in memory - 16 kHz, mono, 16 bit - for sending a piece of a recording."""
    pcm = samples.astype("<i2").tobytes()
    return _header(len(pcm) // 2) + pcm


# ---- one side of a conversation on its clock --------------------------------------------------------

# Pieces of sound come a little early or late; a real gap - nothing was playing - is longer.
SLACK = RATE // 5


class Track:
    """One side of a conversation on the clock of that conversation: what did not come is silence."""

    def __init__(self, writer: WavWriter) -> None:
        self._writer = writer
        self.gaps = 0               # how many times silence was put in between two pieces of sound...
        self.gap_frames = 0         # ...and how much of it

    def put(self, at: float, samples: np.ndarray) -> None:
        """`samples` begin `at` seconds after the conversation did."""
        missing = self.pad(at, SLACK)
        if missing:
            self.gaps += 1
            self.gap_frames += missing
        self._writer.write(samples)

    def pad(self, until: float, slack: int = 0) -> int:
        """Silence up to `until` seconds, if the track is behind by more than `slack` frames;
        the frames of silence written."""
        missing = int(round(until * RATE)) - self._writer.frames
        if missing <= slack:
            return 0
        self._writer.silence(missing)
        return missing
