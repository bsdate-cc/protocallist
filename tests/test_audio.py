import wave

import numpy as np
import pytest

from protocallist.audio import RATE, SLACK, Resampler, Track, WavWriter, read, repair, silence


def tone(hz: float, rate: int, seconds: float, channels: int = 1, level: float = 0.5) -> np.ndarray:
    """A sine as a sound card gives it: 16-bit frames, the channels interleaved."""
    wave_ = np.sin(2 * np.pi * hz * np.arange(int(rate * seconds)) / rate) * level
    return np.repeat((wave_ * 32767).astype(np.int16), channels)


def loudness(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples ** 2)))


def pitch(samples: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.argmax(spectrum) * RATE / len(samples))


# --- any sound to 16 kHz mono


def test_sound_at_16_khz_mono_passes_as_it_is():
    frames = tone(440, RATE, 0.5)
    out = Resampler(RATE, 1).feed(frames)
    np.testing.assert_allclose(out, frames.astype(np.float32) / 32768)


def test_48_khz_stereo_becomes_16_khz_mono_of_the_same_length_pitch_and_loudness():
    out = Resampler(48000, 2).feed(tone(1000, 48000, 1.0, channels=2))
    assert abs(len(out) - RATE) <= 1
    assert abs(pitch(out[2000:14000]) - 1000) < 5
    assert loudness(out[2000:14000]) == pytest.approx(0.5 / np.sqrt(2), rel=0.03)


def test_what_16_khz_cannot_carry_is_removed_not_folded_into_the_speech():
    # 11 kHz at 48 kHz would come out as a false 5 kHz tone if it were only thinned out
    out = Resampler(48000, 1).feed(tone(11000, 48000, 1.0))
    assert loudness(out[2000:14000]) < 0.5 / np.sqrt(2) / 30


def test_pieces_give_the_same_sound_as_the_whole():
    frames = tone(700, 48000, 1.0, channels=2)
    whole = Resampler(48000, 2).feed(frames)
    resampler = Resampler(48000, 2)
    pieces = [resampler.feed(frames[a:a + 2 * 4411]) for a in range(0, len(frames), 2 * 4411)]   # an odd piece size
    np.testing.assert_allclose(np.concatenate(pieces), whole, atol=1e-6)


def test_a_rate_that_16_khz_does_not_divide():
    frames = tone(1000, 44100, 1.0)
    resampler = Resampler(44100, 1)
    out = np.concatenate([resampler.feed(frames[a:a + 4410]) for a in range(0, len(frames), 4410)])
    assert abs(len(out) - RATE) <= 2
    assert abs(pitch(out[2000:14000]) - 1000) < 5


def test_an_empty_piece_gives_nothing():
    assert len(Resampler(48000, 2).feed(np.zeros(0, dtype=np.int16))) == 0


# --- a recording on disk


def test_the_file_is_an_ordinary_wav(tmp_path):
    writer = WavWriter(tmp_path / "a.wav")
    sound = np.linspace(-1, 1, 4000, dtype=np.float32)
    writer.write(sound[:1500])
    writer.write(sound[1500:])
    writer.close()
    with wave.open(str(tmp_path / "a.wav"), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 1, 2, 4000)
    np.testing.assert_allclose(read(tmp_path / "a.wav"), sound, atol=1 / 16000)
    assert writer.frames == 4000


def test_a_sample_too_loud_is_clipped_not_wrapped(tmp_path):
    writer = WavWriter(tmp_path / "a.wav")
    writer.write(np.array([2.0, -2.0], dtype=np.float32))
    writer.close()
    assert read(tmp_path / "a.wav").tolist() == pytest.approx([1.0, -1.0], abs=1e-3)


def test_a_file_left_by_a_dead_process_is_a_recording_up_to_its_last_second(tmp_path):
    path = tmp_path / "a.wav"
    writer = WavWriter(path)
    writer.write(np.full(RATE * 2 + 5000, 0.25, dtype=np.float32))   # two seconds and a bit, never closed
    writer._file.flush()
    left = tmp_path / "left.wav"
    left.write_bytes(path.read_bytes())
    with wave.open(str(left), "rb") as w:
        assert w.getnframes() >= RATE * 2          # the header was kept true while the sound came
    assert repair(left) == RATE * 2 + 5000         # and nothing after it is lost
    assert len(read(left)) == RATE * 2 + 5000
    writer.close()


@pytest.mark.parametrize("cut", [0, 10, 43, 44, 45, 1001])
def test_a_file_cut_off_at_any_byte_is_made_readable(tmp_path, cut):
    path = tmp_path / "a.wav"
    writer = WavWriter(path)
    writer.write(np.full(4000, 0.5, dtype=np.float32))
    writer.close()
    path.write_bytes(path.read_bytes()[:cut])
    frames = repair(path)
    assert frames == max(cut - 44, 0) // 2
    with wave.open(str(path), "rb") as w:
        assert w.getnframes() == frames


def test_the_header_of_a_recording_longer_than_a_wav_can_say_does_not_break():
    from protocallist.audio import _header

    assert len(_header(3_000_000_000)) == 44        # 52 hours: the file is still read by its length


def test_a_recording_can_be_cut_down(tmp_path):
    writer = WavWriter(tmp_path / "a.wav")
    writer.write(np.full(5000, 0.5, dtype=np.float32))
    writer.truncate(3000)
    writer.truncate(4000)                          # longer than it is: nothing to cut
    writer.write(np.full(10, -0.5, dtype=np.float32))
    writer.close()
    sound = read(tmp_path / "a.wav")
    assert len(sound) == 3010 and sound[2999] > 0 and sound[3000] < 0


# --- one side of a conversation on its clock


def track(tmp_path) -> tuple[Track, WavWriter]:
    writer = WavWriter(tmp_path / "t.wav")
    return Track(writer), writer


def test_sound_that_comes_in_a_row_is_written_in_a_row(tmp_path):
    side, writer = track(tmp_path)
    piece = np.full(1600, 0.5, dtype=np.float32)
    for n in range(5):
        side.put(n * 0.1 + 0.03 * (n % 2), piece)          # a little late every other time
    assert writer.frames == 5 * 1600                        # no silence slipped in between


def test_what_did_not_come_is_silence_of_that_length(tmp_path):
    side, writer = track(tmp_path)
    piece = np.full(1600, 0.5, dtype=np.float32)
    side.put(0.0, piece)
    side.put(3.0, piece)                                    # nothing played for almost three seconds
    writer.close()
    sound = read(writer.path)
    assert len(sound) == 3 * RATE + 1600
    assert np.all(sound[1600:3 * RATE] == 0) and sound[3 * RATE] > 0


def test_a_gap_within_the_slack_is_not_filled(tmp_path):
    side, writer = track(tmp_path)
    side.put(0.0, np.full(1600, 0.5, dtype=np.float32))
    side.put(0.1 + (SLACK - 1) / RATE, np.full(1600, 0.5, dtype=np.float32))
    assert writer.frames == 3200


def test_a_track_counts_the_silence_it_put_in_between_pieces(tmp_path):
    side, writer = track(tmp_path)
    piece = np.full(1600, 0.5, dtype=np.float32)
    side.put(0.0, piece)
    side.put(0.1, piece)
    side.put(2.2, piece)                                    # two seconds of nothing
    side.put(2.3, piece)
    side.put(5.4, piece)                                    # three more
    side.pad(10.0)                                          # the end of the conversation is not a gap
    assert (side.gaps, side.gap_frames) == (2, 5 * RATE)


def test_a_track_can_be_filled_with_silence_up_to_a_moment(tmp_path):
    side, writer = track(tmp_path)
    side.put(0.0, np.full(1600, 0.5, dtype=np.float32))
    side.pad(2.0)
    side.pad(1.0)                                           # already past it: nothing happens
    assert writer.frames == 2 * RATE


# --- a long silence: a program may hold the microphone for a day while nothing plays


def test_silence_comes_a_second_at_a_time():
    pieces = list(silence(2 * RATE + 10))
    assert [len(piece) for piece in pieces] == [2 * RATE, 2 * RATE, 20]     # two bytes a frame
    assert not any(b"".join(pieces))
    assert list(silence(0)) == [] and list(silence(-5)) == []


def test_an_hour_of_silence_is_never_one_block_in_memory(tmp_path):
    writer = WavWriter(tmp_path / "long.wav")
    file, sizes = writer._file, []

    class Watched:
        def write(self, data):
            sizes.append(len(data))
            return file.write(data)

        def __getattr__(self, name):
            return getattr(file, name)

    writer._file = Watched()
    writer.silence(3600 * RATE)
    writer.close()
    assert max(sizes) <= 2 * RATE                            # a second of sound, no more
    assert writer.frames == 3600 * RATE
    assert (tmp_path / "long.wav").stat().st_size == 44 + 3600 * RATE * 2


# --- a recording read in pieces


def test_a_recording_is_read_piece_by_piece_as_it_lies_in_the_file(tmp_path):
    from protocallist import audio

    path = tmp_path / "mic.wav"
    w = audio.WavWriter(path)
    ramp = (np.arange(5 * RATE) % 2000 - 1000).astype(np.float32) / 32768
    w.write(ramp)
    w.close()
    assert audio.frames(path) == 5 * RATE
    piece = audio.read_frames(path, RATE, 2 * RATE)
    assert piece.dtype == np.int16 and len(piece) == 2 * RATE
    assert np.array_equal(piece, np.round(read(path)[RATE:3 * RATE] * 32768).astype(np.int16))
    assert piece[0] != piece[1]                                                # sound, not zeros
    assert len(audio.read_frames(path, 4 * RATE, 9 * RATE)) == RATE            # past the end: what there is
    assert len(audio.read_frames(path, 7 * RATE, RATE)) == 0


def test_a_piece_is_a_whole_wav_file_in_memory(tmp_path):
    import io

    from protocallist import audio

    samples = np.array([0, 1000, -1000, 32767, -32768], dtype=np.int16)
    with wave.open(io.BytesIO(audio.wav(samples))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 1, 2, 5)
        assert np.array_equal(np.frombuffer(w.readframes(5), dtype=np.int16), samples)
