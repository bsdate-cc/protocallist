"""Two recordings into one small stereo file - written by the real library."""
import numpy as np
import pytest
import soundfile

from protocallist import audio, opus
from protocallist.audio import RATE, WavWriter


@pytest.fixture
def folder(tmp_path):
    """The folder of one conversation."""
    made = tmp_path / "call"
    made.mkdir()
    return made


def recording(path, seconds: float, hz: float, level: float = 0.4):
    w = WavWriter(path)
    w.write((np.sin(2 * np.pi * hz * np.arange(int(RATE * seconds)) / RATE) * level).astype(np.float32))
    w.close()
    return path


def loudness(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def test_the_microphone_is_on_the_left_and_what_was_heard_on_the_right(folder):
    mic, line = recording(folder / "mic.wav", 25.0, 300), recording(folder / "line.wav", 12.0, 1100)
    out = folder / "audio.opus"
    opus.compress(mic, line, out)
    info = soundfile.info(str(out))
    assert (info.channels, info.samplerate) == (2, RATE) and abs(info.frames - 25 * RATE) <= RATE
    sound, _ = soundfile.read(str(out), dtype="float32")
    left, right = sound[:, 0], sound[:, 1]
    assert loudness(left[RATE:24 * RATE]) > 0.2                          # the microphone, all 25 seconds of it
    assert loudness(right[RATE:11 * RATE]) > 0.2                         # what was heard, for its 12 seconds...
    assert loudness(right[13 * RATE:24 * RATE]) < 0.01                   # ...and silence after them
    assert out.stat().st_size < (mic.stat().st_size + line.stat().st_size) / 4
    assert sorted(p.name for p in folder.iterdir()) == ["audio.opus", "line.wav", "mic.wav"]   # the recordings are not touched here


def test_a_long_recording_is_written_block_by_block(folder, monkeypatch):
    monkeypatch.setattr(opus, "BLOCK", RATE)                             # a second at a time
    read = []
    real = audio.read_frames
    monkeypatch.setattr(audio, "read_frames", lambda path, start, count: read.append(count) or real(path, start, count))
    recording(folder / "mic.wav", 5.5, 300)
    opus.compress(folder / "mic.wav", folder / "line.wav", folder / "audio.opus")
    assert read == [RATE] * 5 + [RATE // 2]                              # never more than a block in memory
    assert abs(soundfile.info(str(folder / "audio.opus")).frames - int(5.5 * RATE)) <= RATE


def test_a_conversation_recorded_without_one_side_has_silence_there(folder):
    recording(folder / "mic.wav", 6.0, 300)
    opus.compress(folder / "mic.wav", folder / "line.wav", folder / "audio.opus")
    sound, _ = soundfile.read(str(folder / "audio.opus"), dtype="float32")
    assert loudness(sound[RATE:5 * RATE, 0]) > 0.2 and loudness(sound[:, 1]) < 0.01


def test_nothing_to_compress_is_said_and_leaves_no_file(folder):
    with pytest.raises(ValueError, match="no sound"):
        opus.compress(folder / "mic.wav", folder / "line.wav", folder / "audio.opus")
    (folder / "mic.wav").write_bytes(bytes(44))                        # a header and not one frame
    with pytest.raises(ValueError, match="no sound"):
        opus.compress(folder / "mic.wav", folder / "line.wav", folder / "audio.opus")
    assert not (folder / "audio.opus").exists()


def test_a_file_that_did_not_come_out_whole_is_not_kept(folder, monkeypatch):
    # The recordings are deleted after this returns: it must not return over half a file.
    from types import SimpleNamespace

    recording(folder / "mic.wav", 6.0, 300)
    monkeypatch.setattr(soundfile, "info", lambda path: SimpleNamespace(channels=2, frames=2 * RATE))
    with pytest.raises(ValueError, match="holds"):
        opus.compress(folder / "mic.wav", folder / "line.wav", folder / "audio.opus")
    assert [p.name for p in folder.iterdir()] == ["mic.wav"]


def test_an_older_file_is_replaced_only_by_a_whole_new_one(folder, monkeypatch):
    recording(folder / "mic.wav", 3.0, 300)
    out = folder / "audio.opus"
    out.write_bytes(b"what was there before")
    monkeypatch.setattr(soundfile, "info", lambda path: (_ for _ in ()).throw(RuntimeError("the library fell over")))
    with pytest.raises(RuntimeError):
        opus.compress(folder / "mic.wav", folder / "line.wav", out)
    assert out.read_bytes() == b"what was there before"
