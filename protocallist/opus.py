"""Both sides of a conversation in one small file: stereo Opus, the microphone on the left,
what was heard on the right. An hour takes about 26 MB instead of 230."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import numpy as np

from protocallist import audio

BLOCK = audio.RATE * 10         # ten seconds at a time: a conversation of hours is never in memory at once


class Side:
    """One side of a conversation, read back from the small file: channel 0 is the microphone,
    channel 1 what was heard. Read from any place, a piece at a time - never the whole of it.
    A file that cannot be opened as sound is an OSError."""

    def __init__(self, path: Path, channel: int) -> None:
        self._path, self._channel = path, channel

    @contextlib.contextmanager
    def _open(self):
        import soundfile

        try:
            file = soundfile.SoundFile(str(self._path))
        except Exception as e:      # the library has errors of its own kind for a file that is no sound
            raise OSError(f"the sound cannot be read ({type(e).__name__})") from e
        with file:
            if file.samplerate != audio.RATE or file.channels != 2:     # not written by this program: its text would be anything
                raise OSError(f"the sound is not what the program keeps ({file.samplerate} Hz, {file.channels} channels)")
            yield file

    def frames(self) -> int:
        with self._open() as f:
            return int(f.frames)

    def read(self, start: int, count: int) -> np.ndarray:
        """`count` frames from `start`, as 16-bit samples; fewer where the file ends."""
        with self._open() as f:
            try:
                f.seek(start)
                return f.read(count, dtype="int16", always_2d=True)[:, self._channel].copy()
            except Exception as e:      # a file damaged in the middle: the library's own kind of error again
                raise OSError(f"the sound cannot be read ({type(e).__name__})") from e


def compress(mic: Path, line: Path, out: Path) -> None:
    """Write `out` from the two recordings; a side that is missing or shorter is silence.
    Raises if there is nothing to write, or if what was written is not the whole of it -
    the caller deletes the recordings only after this returns."""
    import soundfile        # here, not at the top: without the library conversations are still recorded and recognised

    sides = [path for path in (mic, line)]
    length = max((audio.frames(path) for path in sides if path.is_file()), default=0)
    if not length:
        raise ValueError("there is no sound to compress")
    part = out.with_name(out.name + ".part")
    try:
        with soundfile.SoundFile(str(part), "w", samplerate=audio.RATE, channels=2, format="OGG", subtype="OPUS") as f:
            for start in range(0, length, BLOCK):
                block = np.zeros((min(BLOCK, length - start), 2), dtype=np.int16)
                for channel, path in enumerate(sides):
                    if path.is_file():
                        got = audio.read_frames(path, start, len(block))
                        block[:len(got), channel] = got
                f.write(block)
        written = soundfile.info(str(part))
        if written.channels != 2 or abs(written.frames - length) > audio.RATE:
            raise ValueError(f"the compressed file holds {written.frames} frames of {length}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
