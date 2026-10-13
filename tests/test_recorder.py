import logging

import numpy as np
import pytest

from protocallist.audio import RATE, read
from protocallist.recorder import Recorder, RecorderError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    """A sound library the test plays the part of: it hands pieces of sound to whoever opened a stream."""

    def __init__(self, clock: Clock, no_microphone: bool = False, no_line: bool = False) -> None:
        self.clock = clock
        self.no_microphone, self.no_line = no_microphone, no_line
        self.deliver: dict[str, object] = {}
        self.streams: dict[str, list[FakeStream]] = {"mic": [], "line": []}
        self.opened: list[tuple[str, str]] = []
        self.closed = 0

    def _open(self, side: str, name: str, deliver) -> FakeStream:
        self.opened.append((side, name))
        self.deliver[side] = deliver
        self.streams[side].append(FakeStream())
        return self.streams[side][-1]

    def microphone(self, name, deliver):
        if self.no_microphone:
            raise RecorderError("no such microphone")
        return self._open("mic", name, deliver)

    def line(self, speaker, deliver):
        if self.no_line:
            raise RecorderError("no such line")
        return self._open("line", speaker, deliver)

    def close(self) -> None:
        self.closed += 1

    def hear(self, side: str, seconds: float, level: float = 0.5, rate: int = RATE, channels: int = 1) -> None:
        """`seconds` of sound pass and arrive as one piece, the way a sound card delivers it."""
        self.clock.now += seconds
        frames = np.full(int(rate * seconds) * channels, int(level * 32767), dtype=np.int16)
        self.deliver[side](frames, rate, channels)


@pytest.fixture
def clock():
    return Clock()


def recorder(backend: FakeBackend, speaker: str | None = "Earphone") -> Recorder:
    return Recorder(backend, "Headset Microphone", speaker, clock=backend.clock)


def test_both_sides_are_recorded_into_two_files_of_one_length(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    assert backend.opened == [("mic", "Headset Microphone"), ("line", "Earphone")]
    for _ in range(10):                                     # a second of both, a tenth at a time
        backend.hear("mic", 0.1, level=0.25)
        backend.hear("line", 0.0, level=0.5)                # the same tenth, heard on the other side
        backend.deliver["line"](np.full(1600, 16383, dtype=np.int16), RATE, 1)
    assert r.stop(clock.now) == pytest.approx(1.0)
    mic, line = read(tmp_path / "mic.wav"), read(tmp_path / "line.wav")
    assert len(mic) == len(line) == RATE
    assert mic[100] == pytest.approx(0.25, abs=0.01) and line[RATE - 100] == pytest.approx(0.5, abs=0.01)
    assert all(s.closed for side in backend.streams.values() for s in side) and backend.closed == 1


def test_what_is_heard_comes_at_48_khz_stereo_and_is_stored_at_16_khz_mono(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    for _ in range(10):
        backend.hear("line", 0.1, rate=48000, channels=2)
    r.stop(clock.now)
    line = read(tmp_path / "line.wav")
    assert len(line) == RATE
    assert line[RATE // 2] == pytest.approx(0.5, abs=0.02)


def test_when_nothing_plays_the_line_is_silent_for_exactly_that_long(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("line", 0.5)
    clock.now += 4.0                                        # nothing played: the sound library sends nothing at all
    backend.hear("line", 0.5)
    r.stop(clock.now)
    line = read(tmp_path / "line.wav")
    assert len(line) == 5 * RATE
    assert line[100] > 0.4 and np.all(line[RATE // 2:int(4.5 * RATE)] == 0) and line[-100] > 0.4


def test_the_microphone_is_let_go_of_and_taken_again_and_the_pause_is_silence(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    r.release_mic()
    assert backend.streams["mic"][0].closed and not backend.streams["line"][0].closed
    r.release_mic()                                         # twice is not an error
    clock.now += 3.0
    r.resume_mic()
    r.resume_mic()                                          # it is open already
    backend.hear("mic", 1.0)
    r.stop(clock.now)
    mic = read(tmp_path / "mic.wav")
    assert len(mic) == 5 * RATE and len(backend.streams["mic"]) == 2
    assert mic[RATE // 2] > 0.4 and np.all(mic[RATE:4 * RATE] == 0) and mic[-100] > 0.4


def test_the_end_of_the_conversation_cuts_what_was_recorded_after_it(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    started = clock.now
    r.start(tmp_path, started)
    backend.hear("mic", 2.0)
    backend.hear("line", 0.0)
    backend.deliver["line"](np.full(2 * RATE, 16383, dtype=np.int16), RATE, 1)
    r.release_mic()                                         # the microphone was let go of two seconds in...
    backend.hear("line", 3.0)                               # ...and the line was recorded for three more, in case
    assert r.stop(started + 2.0) == pytest.approx(2.0)
    assert len(read(tmp_path / "mic.wav")) == len(read(tmp_path / "line.wav")) == 2 * RATE


def test_a_conversation_that_ended_later_than_its_last_sound_is_filled_with_silence(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    clock.now += 2.0
    r.stop(clock.now)
    mic, line = read(tmp_path / "mic.wav"), read(tmp_path / "line.wav")
    assert len(mic) == len(line) == 3 * RATE and np.all(line == 0)


def test_without_a_line_the_microphone_is_still_recorded(tmp_path, clock, caplog):
    for backend, speaker in ((FakeBackend(clock, no_line=True), "Earphone"), (FakeBackend(clock), None)):
        folder = tmp_path / str(id(backend))
        folder.mkdir()
        r = recorder(backend, speaker)
        with caplog.at_level(logging.WARNING, logger="protocallist"):
            r.start(folder, clock.now)
        assert "only the microphone" in caplog.text
        backend.hear("mic", 1.0)
        r.stop(clock.now)
        assert len(read(folder / "mic.wav")) == RATE
        assert np.all(read(folder / "line.wav") == 0) and len(read(folder / "line.wav")) == RATE


def test_without_a_microphone_nothing_is_started_and_nothing_is_left_open(tmp_path, clock):
    backend = FakeBackend(clock, no_microphone=True)
    folder = tmp_path / "call"
    folder.mkdir()
    with pytest.raises(RecorderError):
        recorder(backend).start(folder, clock.now)
    assert list(folder.iterdir()) == [] and backend.closed == 1


def test_a_microphone_that_does_not_come_back_leaves_the_line_recording(tmp_path, clock, caplog):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    r.release_mic()
    backend.no_microphone = True
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        r.resume_mic()
    assert "cannot be opened again" in caplog.text
    backend.hear("line", 1.0)
    r.stop(clock.now)
    assert len(read(tmp_path / "line.wav")) == 2 * RATE


def test_a_disk_that_takes_no_more_stops_the_writing_and_is_said_once(tmp_path, clock, caplog, monkeypatch):
    from protocallist import audio

    real = audio.WavWriter.write

    def write(self, samples):
        if self.frames >= 2 * RATE:                         # the disk is full two seconds in
            raise OSError(28, "No space left on device")
        real(self, samples)

    monkeypatch.setattr(audio.WavWriter, "write", write)
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        for _ in range(7):
            backend.hear("mic", 1.0)
        r.stop(clock.now)
    assert isinstance(r.failed, OSError)
    assert caplog.text.count("cannot be written on") == 1
    assert read(tmp_path / "mic.wav")[2 * RATE - 1] > 0.4   # what was written before stays a recording


def test_the_log_says_how_much_silence_was_put_in(tmp_path, clock, caplog):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("line", 0.5)
    clock.now += 4.0
    backend.hear("line", 0.5)
    with caplog.at_level(logging.INFO, logger="protocallist"):
        r.stop(clock.now)
    assert "line: silence was put in 1 times, 4.0 s in all" in caplog.text
    assert "mic: silence was put in 0 times" in caplog.text


def test_an_aborted_recording_lets_go_of_everything(tmp_path, clock):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 0.5)
    r.abort()
    assert all(s.closed for side in backend.streams.values() for s in side) and backend.closed == 1
    (tmp_path / "mic.wav").unlink()                         # nothing holds the files any more
    (tmp_path / "line.wav").unlink()


# --- whatever refuses at the end, the rest is let go of: nothing keeps recording after a conversation


def refusing(stream):
    def close():
        raise OSError("the device is gone")

    stream.close = close


def test_a_stream_that_will_not_close_does_not_keep_the_rest_open(tmp_path, clock, caplog):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    backend.hear("line", 1.0)
    refusing(backend.streams["mic"][0])
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        seconds = r.stop(clock.now)
    assert seconds == 2.0
    assert backend.streams["line"][0].closed                 # the other stream is closed all the same
    assert backend.closed == 1                               # and the library
    assert "the mic stream could not be closed" in caplog.text
    assert len(read(tmp_path / "mic.wav")) == len(read(tmp_path / "line.wav")) == 2 * RATE
    backend.hear("mic", 1.0)                                 # the stuck stream goes on delivering
    assert r._pieces.empty()                                 # nobody listens: nothing piles up
    for name in ("mic.wav", "line.wav"):
        (tmp_path / name).unlink()                           # the files are closed: Windows lets them go


def test_a_microphone_that_will_not_let_go_does_not_stop_the_service_from_asking(tmp_path, clock, caplog):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    refusing(backend.streams["mic"][0])
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        r.release_mic()                                      # does not raise
    assert "the mic stream could not be closed" in caplog.text
    r.resume_mic()
    assert len(backend.streams["mic"]) == 2                  # and the microphone can be taken again
    r.abort()


def test_a_library_that_will_not_close_is_said_and_the_recording_is_whole(tmp_path, clock, caplog):
    backend = FakeBackend(clock)

    def close():
        raise OSError("already terminated")

    backend.close = close
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        assert r.stop(clock.now) == 1.0
    assert "the sound library could not be closed" in caplog.text
    assert len(read(tmp_path / "mic.wav")) == RATE


def test_files_are_closed_even_when_the_end_cannot_be_written(tmp_path, clock, caplog):
    backend = FakeBackend(clock)
    r = recorder(backend)
    r.start(tmp_path, clock.now)
    backend.hear("mic", 1.0)
    clock.now += 5.0                                         # five seconds of silence to put in at the end...

    def full(frames):
        raise OSError(28, "No space left on device")

    for side in r._sides.values():
        side.writer.silence = full                           # ...and a disk that takes no more
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        r.stop(clock.now)
    assert "could not be finished" in caplog.text
    assert len(read(tmp_path / "mic.wav")) == RATE           # what was written stays, and is a recording
    for name in ("mic.wav", "line.wav"):
        (tmp_path / name).unlink()                           # closed: Windows lets them go
