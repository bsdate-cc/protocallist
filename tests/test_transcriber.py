"""Recognition: real recordings on disk and a real archive; the gateway and the compressor are stand-ins."""
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from protocallist import audio, gateway, opus, paths, summaries, transcriber
from protocallist.audio import RATE, WavWriter
from protocallist.config import Config
from protocallist.gateway import Refused, Segment, Unavailable
from protocallist.store import DONE, FAILED, MADE, MINE, RECORDED, Store
from protocallist.transcriber import IDLE, Transcriber

AT = datetime(2026, 10, 6, 12, 0, 0)


def sound(seconds: float, quiet: tuple = ()) -> np.ndarray:
    """A tone, silent in the given (from, to) seconds."""
    samples = (np.sin(2 * np.pi * 220 * np.arange(int(RATE * seconds)) / RATE) * 0.4).astype(np.float32)
    for a, b in quiet:
        samples[int(a * RATE):int(b * RATE)] = 0.0
    return samples


def recording(path, seconds: float, quiet: tuple = ()):
    w = WavWriter(path)
    w.write(sound(seconds, quiet))
    w.close()
    return path


@pytest.fixture(autouse=True)
def short_pieces(monkeypatch):
    """Twenty seconds stand for ten minutes, five for the half minute where the cut is looked for."""
    monkeypatch.setattr(transcriber, "PIECE_S", 20)
    monkeypatch.setattr(transcriber, "SEARCH_S", 5)


class Gate:
    """Stands in for the gateway: one phrase a second into every piece it is given."""
    model = "gigaam-v3"

    def __init__(self, world) -> None:
        self.world = world

    def models(self):
        if isinstance(self.world.known, Exception):
            raise self.world.known
        return self.world.known

    def transcribe(self, wav: bytes, model: str = "") -> list[Segment]:
        world = self.world
        world.pieces.append((len(wav) - 44) // 2)              # how many frames it was given
        world.models.append(model)                             # ...and which model was to read them
        world.quiet.append(float(np.abs(np.frombuffer(wav[44:], dtype="<i2").astype(np.float64)).mean()) < 50)
        if world.meanwhile is not None:
            world.meanwhile()                                  # what happens while the gateway has the piece
        if world.refusal is not None:
            raise world.refusal
        if world.then is not None:
            world.then()
        if world.wordless:
            return []
        return [Segment(1.0, 2.5, f"phrase {len(world.pieces)}" if model else f"фраза {len(world.pieces)}")]


class World:
    def __init__(self, keep_audio_days: int = 30) -> None:
        self.store = Store(paths.database_file(), paths.calls_dir())
        self.cfg = Config()
        self.cfg.storage.keep_audio_days = keep_audio_days
        self.now, self.today = 1000.0, AT + timedelta(days=1)
        self.pieces: list[int] = []
        self.models: list[str] = []
        self.quiet: list[bool] = []         # of every piece it was given: was it silence?
        self.wordless = False               # the gateway reads every piece and hears no word in any
        self.meanwhile = None               # called when a piece has reached the gateway, before it answers or refuses
        # what the gateway says it has; None - it does not say: an older one
        self.known = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, True)]
        self.real = False                   # the small file is made for real, not stood in for
        self.connected = 0
        self.unavailable: Unavailable | None = None
        self.refusal: Exception | None = None
        self.then = None                    # what happens in the world after a piece was taken
        self.recording = False
        self.compressed: list[str] = []
        self.compress_error: Exception | None = None
        self.t = Transcriber(self.store, self.cfg, connect=self._connect, busy=lambda: self.recording,
                             compress=self._compress, clock=lambda: self.now, today=lambda: self.today)

    def _connect(self):
        self.connected += 1
        if self.unavailable is not None:
            raise self.unavailable
        return Gate(self)

    def _compress(self, mic, line, out) -> None:
        self.compressed.append(out.name.removesuffix(".opus"))
        if self.compress_error is not None:
            raise self.compress_error
        if self.real:
            opus.compress(mic, line, out)
        else:
            out.write_bytes(b"OggS")

    def call(self, mic_s: float | None = 30.0, line_s: float | None = 30.0, minutes_later: int = 0):
        call = self.store.begin(AT + timedelta(minutes=minutes_later), "Zoom")
        folder = self.store.folder(call)
        for name, seconds in (("mic.wav", mic_s), ("line.wav", line_s)):
            if seconds is not None:
                recording(folder / name, seconds)
        return self.store.finish(call.id, max(mic_s or 0.0, line_s or 0.0))

    def files(self, call) -> list[str]:
        """What is left of a conversation on disk, its name written as *: "*.opus" lies beside the working
        folder, "*/mic.wav" inside it, "*/" is the folder left empty."""
        root, name = self.store.root, call.folder
        beside = [f"*{p.name[len(name):]}" for p in root.iterdir() if p.is_file() and p.name.startswith(name + ".")]
        inside = [f"*/{p.name}" for p in (root / name).iterdir()] if (root / name).is_dir() else []
        return sorted(beside + inside + (["*/"] if (root / name).is_dir() and not inside else []))


@pytest.fixture
def world():
    w = World()
    yield w
    w.store.close()


# --- cutting


def cuts(path):
    """The pieces of a recording that lies on disk."""
    return transcriber.cuts(audio.frames(path), lambda start, count: audio.read_frames(path, start, count))


def test_a_recording_is_cut_at_the_quietest_place_of_the_last_seconds_of_a_piece(tmp_path):
    path = recording(tmp_path / "mic.wav", 50.0, quiet=((17.0, 17.6), (36.2, 36.8)))
    pieces = list(cuts(path))
    assert len(pieces) == 3
    assert abs(pieces[0][1] / RATE - 17.3) < 0.15                    # in the middle of the first pause
    assert abs(pieces[1][0] / RATE + pieces[1][1] / RATE - 36.5) < 0.15
    assert pieces[0][0] == 0 and sum(count for _, count in pieces) == 50 * RATE     # nothing lost...
    assert all(a + n == b for (a, n), (b, _) in zip(pieces, pieces[1:]))            # ...and nothing said twice
    assert all(count <= 20 * RATE for _, count in pieces)


def test_without_a_quiet_place_the_cut_still_falls_into_the_last_seconds(tmp_path):
    path = recording(tmp_path / "mic.wav", 45.0)
    first, second, third = cuts(path)
    assert 15 * RATE <= first[1] <= 20 * RATE and 15 * RATE <= second[1] <= 20 * RATE
    assert third[0] + third[1] == 45 * RATE


def test_a_recording_no_longer_than_a_piece_is_one_piece_and_an_empty_one_is_none(tmp_path):
    assert list(cuts(recording(tmp_path / "short.wav", 12.0))) == [(0, 12 * RATE)]
    assert list(cuts(recording(tmp_path / "exact.wav", 20.0))) == [(0, 20 * RATE)]
    assert list(cuts(recording(tmp_path / "empty.wav", 0.0))) == []


# --- one conversation


def test_both_sides_of_a_conversation_come_out_on_one_clock(world):
    call = world.call(mic_s=50.0, line_s=30.0)
    world.t.step()
    done = world.store.get(call.id)
    assert (done.state, done.model, done.attempts) == (DONE, "gigaam-v3", 0)
    phrases = world.store.phrases(call.id)
    assert [p.speaker for p in phrases].count("me") == 3 and [p.speaker for p in phrases].count("them") == 2
    assert phrases == sorted(phrases, key=lambda p: p.start_s)
    mine = [p for p in phrases if p.speaker == "me"]
    assert mine[0].start_s == 1.0 and mine[0].end_s == 2.5              # a second into the first piece
    assert 16.0 <= mine[1].start_s <= 21.0 and 31.0 <= mine[2].start_s <= 41.0     # ...and into the next two
    assert sum(world.pieces[:3]) == 50 * RATE and sum(world.pieces[3:]) == 30 * RATE    # every frame was sent, once
    assert world.t.status == IDLE


def test_the_recordings_become_one_small_file_and_the_text_stays_in_the_archive(world):
    call = world.call()
    world.t.step()
    assert world.files(call) == ["*.opus"]         # the recordings and their folder are gone; no file of the text
    assert world.store.sound(call) == paths.calls_dir() / "2026-10-06_12-00-00_Zoom.opus"
    said = [phrase.speaker for phrase in world.store.phrases(call.id)]
    assert said == ["me", "them", "me", "them"]                          # by the clock, not one side after the other
    assert world.compressed == [call.folder]


def test_conversations_are_recognised_one_at_a_turn_the_earliest_first(world):
    later, earlier = world.call(minutes_later=30), world.call()
    world.t.step()
    assert (world.store.get(earlier.id).state, world.store.get(later.id).state) == (DONE, RECORDED)
    world.t.step()
    assert world.store.get(later.id).state == DONE


def test_with_nothing_waiting_the_gateway_is_not_even_asked(world):
    world.t.step()
    assert world.connected == 0 and world.t.status == IDLE


def test_a_conversation_recorded_with_one_side_only_is_recognised_from_that_side(world):
    call = world.call(line_s=None)
    world.t.step()
    assert {p.speaker for p in world.store.phrases(call.id)} == {"me"}
    assert world.store.get(call.id).state == DONE and world.compressed == [call.folder]


def test_what_was_said_never_reaches_the_log(world, caplog):
    world.call()
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        world.t.step()
    assert "is recognised: 4 phrases" in caplog.text and "фраза" not in caplog.text


# --- the gateway is not there


def test_a_gateway_that_is_not_there_is_waited_for_and_no_attempt_is_counted(world, caplog):
    call = world.call()
    world.unavailable = Unavailable(gateway.DOWN, "connection refused")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
        assert world.t.status == gateway.DOWN
        waiting = world.store.get(call.id)
        assert (waiting.state, waiting.attempts) == (RECORDED, 0)
        world.now += 30
        world.t.step()
        assert world.connected == 1                                  # not asked again every second...
        world.now += transcriber.RETRY_S
        world.t.step()
        assert world.connected == 2                                  # ...but once a minute
    assert caplog.text.count("recognition waits") == 1               # and said once
    world.unavailable = None
    world.now += transcriber.RETRY_S
    world.t.step()
    assert world.store.get(call.id).state == DONE and world.t.status == IDLE


def test_trouble_with_the_gateway_is_said_again_after_it_had_been_well(world, caplog):
    world.call(), world.call(minutes_later=5), world.call(minutes_later=9)
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.unavailable = Unavailable(gateway.DOWN, "connection refused")
        world.t.step()
        world.unavailable = None
        world.now += transcriber.RETRY_S
        world.t.step()                                               # the first conversation is recognised
        world.unavailable = Unavailable(gateway.DOWN, "connection refused")
        world.t.step()                                               # down again: that is news again
    assert caplog.text.count("recognition waits") == 2


def test_a_gateway_that_is_too_old_or_still_loading_is_said_as_such(world):
    world.call()
    for kind in (gateway.OLD, gateway.LOADING):
        world.unavailable = Unavailable(kind)
        world.now += transcriber.RETRY_S
        world.t.step()
        assert world.t.status == kind


# --- an attempt that failed


def test_a_piece_that_was_refused_is_an_attempt_and_the_third_fails_the_conversation(world, caplog):
    call = world.call()
    world.refusal = Refused("the gateway answered 500: boom")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        for attempt in (1, 2):
            world.t.step()
            again = world.store.get(call.id)
            assert (again.state, again.attempts) == (RECORDED, attempt)
        world.t.step()
    failed = world.store.get(call.id)
    assert (failed.state, failed.attempts, failed.error) == (FAILED, 3, "the gateway answered 500: boom")
    assert world.files(call) == ["*/line.wav", "*/mic.wav"]                 # its sound stays, and there is no half text
    assert "failed for good" in caplog.text
    world.refusal = None
    world.t.step()
    assert world.store.get(call.id).state == FAILED                     # not tried by itself any more
    world.store.retry_failed()
    world.t.step()
    assert world.store.get(call.id).state == DONE


def test_after_a_refusal_the_pieces_recognised_before_it_are_not_asked_for_again(world):
    call = world.call(mic_s=50.0, line_s=30.0)                          # five pieces

    def the_gateway_breaks():
        if len(world.pieces) == 2:
            world.refusal = Refused("the gateway answered 500: boom")

    world.then = the_gateway_breaks
    world.t.step()
    assert (world.store.get(call.id).state, world.store.get(call.id).attempts) == (RECORDED, 1)
    assert len(world.pieces) == 3                                       # two came back, the third was refused
    world.refusal, world.then = None, None
    world.t.step()
    assert world.store.get(call.id).state == DONE
    assert len(world.pieces) == 6 and len(world.store.phrases(call.id)) == 5      # only the last three were sent


def test_a_conversation_without_recordings_fails_like_any_other(world):
    call = world.call(mic_s=None, line_s=None)
    for _ in range(3):
        world.t.step()
    failed = world.store.get(call.id)
    assert failed.state == FAILED and "no recordings" in failed.error and world.pieces == []


# --- a conversation that is being recorded comes first


def test_nothing_is_recognised_while_a_conversation_is_being_recorded(world):
    call = world.call()
    world.recording = True
    world.t.step()
    assert world.store.get(call.id).state == RECORDED and world.connected == 0
    world.recording = False
    world.t.step()
    assert world.store.get(call.id).state == DONE


def test_recognition_gives_way_to_a_conversation_that_began_and_asks_for_no_piece_twice(world):
    call = world.call(mic_s=50.0, line_s=30.0)                          # five pieces

    def a_call_begins():
        if len(world.pieces) == 2:
            world.recording = True

    world.then = a_call_begins
    world.t.step()
    waiting = world.store.get(call.id)
    assert (waiting.state, waiting.attempts) == (RECORDED, 0) and len(world.pieces) == 2
    assert world.store.phrases(call.id) == [] and world.t.status == IDLE
    world.recording, world.then = False, None
    world.t.step()
    assert world.store.get(call.id).state == DONE
    assert len(world.pieces) == 5 and sum(world.pieces) == 80 * RATE    # the first two were not sent again
    assert len(world.store.phrases(call.id)) == 5


# --- the sound afterwards


def test_sound_that_cannot_be_compressed_stays_as_it_was_and_the_conversation_is_still_done(world, caplog):
    call = world.call()
    world.compress_error = RuntimeError("the library fell over")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.t.step()
    assert world.store.get(call.id).state == DONE
    assert world.files(call) == ["*/line.wav", "*/mic.wav"]
    assert "could not be compressed (RuntimeError: the library fell over)" in caplog.text


def test_with_no_days_to_keep_the_sound_goes_as_soon_as_there_is_the_text(world):
    world.cfg.storage.keep_audio_days = 0
    call = world.call()
    world.t.step()
    assert world.files(call) == [] and world.compressed == []
    assert world.store.get(call.id).audio_deleted_at is not None


def test_sound_is_deleted_when_it_has_outlived_its_days_and_the_text_stays(world):
    old, new = world.call(), world.call(minutes_later=5)
    world.t.step()
    world.t.step()
    world.today = AT + timedelta(days=31)
    world.store._db.execute("UPDATE calls SET ended_at = ? WHERE id = ?",
                            ((AT + timedelta(days=30, hours=1)).astimezone().isoformat(timespec="seconds"), new.id))
    world.t.step()                                                      # the same day: the look is not due yet
    assert world.files(old) == ["*.opus"]
    world.now += transcriber.SWEEP_S
    world.t.step()
    assert world.files(old) == [] and world.store.get(old.id).audio_deleted_at.startswith("2026-11-06")
    assert world.files(new) == ["*.opus"] and world.store.get(new.id).audio_deleted_at is None


def test_a_negative_number_of_days_keeps_the_sound_for_ever(world):
    world.cfg.storage.keep_audio_days = -1
    call = world.call()
    world.t.step()
    world.today = AT + timedelta(days=4000)
    world.now += transcriber.SWEEP_S
    world.t.step()
    assert world.files(call) == ["*.opus"]


# --- the thread of its own


def test_the_thread_recognises_what_waits_and_leaves_when_told(world):
    world.now = None
    world.t._clock = time.monotonic
    call = world.call()
    world.t.start()
    deadline = time.monotonic() + 10
    while world.store.get(call.id).state != DONE and time.monotonic() < deadline:
        time.sleep(0.05)
    world.t.stop()
    assert world.store.get(call.id).state == DONE and not world.t._thread.is_alive()


def test_a_turn_that_fails_is_logged_and_the_next_one_is_made(world, caplog, monkeypatch):
    turns = []

    def step():
        turns.append(1)
        if len(turns) == 1:
            raise RuntimeError("the archive is locked")
        world.t._stop.set()

    monkeypatch.setattr(world.t, "step", step)
    monkeypatch.setattr(world.t._stop, "wait", lambda seconds: world.t._stop.is_set())
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.t._run()
    assert len(turns) == 2 and "a turn of recognition failed" in caplog.text


# --- what nobody foresaw


def test_trouble_nobody_foresaw_is_an_attempt_and_leaves_no_conversation_half_recognised(world, caplog):
    call = world.call()
    world.refusal = RuntimeError("what nobody foresaw")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
    after = world.store.get(call.id)
    assert (after.state, after.attempts, after.error) == (RECORDED, 1, "RuntimeError")
    assert world.t.status == IDLE and "call 1: attempt 1" in caplog.text and "Traceback" in caplog.text
    world.t.step()
    world.t.step()
    assert world.store.get(call.id).state == FAILED                    # like any other: the menu offers to try again
    assert world.files(call) == ["*/line.wav", "*/mic.wav"]


def test_a_text_the_archive_does_not_take_is_an_attempt_too(world, monkeypatch):
    def full(call_id, phrases, model, moment):
        raise sqlite3.OperationalError("database or disk is full")

    call = world.call()
    with monkeypatch.context() as patch:
        patch.setattr(world.store, "done", full)
        world.t.step()
    after = world.store.get(call.id)
    assert (after.state, after.attempts, after.error) == (RECORDED, 1, "OperationalError")
    world.t.step()                                                      # there is room again
    assert world.store.get(call.id).state == DONE
    assert len(world.pieces) == 4 and sum(world.pieces) == 60 * RATE     # and no piece was asked for twice


# --- what a start finds half done


def started_anew(world) -> Transcriber:
    """The program started again over the same archive."""
    return Transcriber(world.store, world.cfg, connect=world._connect, busy=lambda: world.recording,
                       compress=world._compress, clock=lambda: world.now, today=lambda: world.today)


def closed_right_after_the_text_went_into_the_archive(world, patch) -> None:
    patch.setattr(world.t, "_sound", lambda call: None)


def test_a_conversation_recognised_as_the_program_was_closed_gets_its_files_at_the_next_start(world, monkeypatch, caplog):
    call = world.call()
    with monkeypatch.context() as patch:
        closed_right_after_the_text_went_into_the_archive(world, patch)
        world.t.step()
    assert world.store.get(call.id).state == DONE and world.files(call) == ["*/line.wav", "*/mic.wav"]
    with caplog.at_level(logging.INFO, logger="protocallist"):
        started_anew(world).step()
    assert world.files(call) == ["*.opus"]
    assert world.connected == 1 and len(world.pieces) == 4             # the gateway was not asked again
    assert "call 1: its files are finished" in caplog.text


def test_recordings_that_could_not_be_compressed_are_tried_again_at_the_next_start_and_not_before(world):
    call = world.call()
    world.compress_error = RuntimeError("the library fell over")
    world.t.step()
    world.t.step()
    assert world.files(call) == ["*/line.wav", "*/mic.wav"] and len(world.compressed) == 1
    world.compress_error = None
    started_anew(world).step()
    assert world.files(call) == ["*.opus"] and len(world.compressed) == 2
    started_anew(world).step()
    assert len(world.compressed) == 2                                   # what is whole is left alone


def test_with_no_days_to_keep_the_next_start_leaves_the_text_and_no_sound(world, monkeypatch):
    world.cfg.storage.keep_audio_days = 0
    call = world.call()
    with monkeypatch.context() as patch:
        closed_right_after_the_text_went_into_the_archive(world, patch)
        world.t.step()
    started_anew(world).step()
    assert world.files(call) == [] and world.compressed == []
    assert world.store.get(call.id).audio_deleted_at is not None and len(world.store.phrases(call.id)) == 4


def test_what_a_start_finishes_waits_for_a_conversation_to_end_and_goes_one_a_turn(world, monkeypatch):
    first, second = world.call(), world.call(minutes_later=5)
    with monkeypatch.context() as patch:
        closed_right_after_the_text_went_into_the_archive(world, patch)
        world.t.step()
        world.t.step()
    again = started_anew(world)
    world.recording = True
    again.step()
    assert world.compressed == []
    world.recording = False
    again.step()
    assert world.compressed == [first.folder]
    again.step()
    assert world.compressed == [first.folder, second.folder]


def test_a_conversation_whose_sound_was_deleted_by_hand_is_left_in_peace(world, caplog):
    call = world.call()
    world.t.step()
    world.store.sound(call).unlink()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="protocallist"):
        started_anew(world).step()
    assert caplog.text == "" and world.files(call) == []


def test_conversations_whose_files_are_whole_take_no_turn_of_a_start(world):
    whole = world.call()
    world.t.step()
    waiting = world.call(minutes_later=5)
    started_anew(world).step()
    assert world.store.get(waiting.id).state == DONE                    # the first turn went to the one that waits
    assert world.files(whole) == ["*.opus"] and len(world.compressed) == 2    # one small file each, made once


def test_sound_that_was_deleted_by_the_rule_is_not_brought_back_as_a_small_file(world):
    call = world.call()
    world.t.step()
    world.store.folder(call).mkdir()
    recording(world.store.folder(call) / "mic.wav", 5.0)                # one that could not be deleted at the time
    world.store.audio_deleted(call.id, world.today)
    started_anew(world).step()
    assert len(world.compressed) == 1 and world.files(call) == ["*.opus", "*/mic.wav"]


# --- small things


def test_the_icon_is_not_told_of_recognition_while_the_gateway_is_only_looked_for(world):
    seen = []

    def looked_for():
        seen.append(t.status)
        return world._connect()

    t = Transcriber(world.store, world.cfg, connect=looked_for, compress=world._compress, clock=lambda: world.now,
                    today=lambda: world.today)
    call = world.call()
    world.unavailable = Unavailable(gateway.DOWN, "nobody listens")
    t.step()
    world.now += transcriber.RETRY_S
    t.step()
    assert seen == [IDLE, gateway.DOWN] and t.status == gateway.DOWN    # never "at work" with nobody to work with
    world.unavailable, world.then = None, lambda: seen.append(t.status)
    world.now += transcriber.RETRY_S
    t.step()
    assert seen[2] == gateway.DOWN and set(seen[3:]) == {transcriber.WORKING} and len(seen) == 7
    assert t.status == IDLE and world.store.get(call.id).state == DONE


def held(path: Path):
    """Path.unlink that cannot delete this one file: it is open in a player."""
    unlink = Path.unlink

    def busy(self, missing_ok=False):
        if self == path:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return unlink(self, missing_ok=missing_ok)

    return busy


def test_sound_that_could_not_be_deleted_is_not_said_to_be_deleted_and_is_tried_again(world, monkeypatch, caplog):
    call = world.call()
    world.t.step()
    sound = world.store.sound(call)
    world.today = AT + timedelta(days=31)
    world.now += transcriber.SWEEP_S
    with monkeypatch.context() as patch, caplog.at_level(logging.INFO, logger="protocallist"):
        patch.setattr(Path, "unlink", held(sound))
        world.t.step()
    assert sound.exists() and world.store.get(call.id).audio_deleted_at is None
    assert "2026-10-06_12-00-00_Zoom.opus could not be deleted" in caplog.text and "its sound is deleted" not in caplog.text
    world.now += transcriber.SWEEP_S
    world.t.step()                                                      # a day later nobody holds it
    assert world.files(call) == [] and world.store.get(call.id).audio_deleted_at is not None


def test_with_no_days_to_keep_a_recording_that_could_not_be_deleted_is_not_said_to_be_deleted(world, monkeypatch):
    world.cfg.storage.keep_audio_days = 0
    call = world.call()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", held(world.store.folder(call) / "mic.wav"))
        world.t.step()
    assert world.files(call) == ["*/mic.wav"] and world.store.get(call.id).audio_deleted_at is None
    world.now += transcriber.SWEEP_S
    world.t.step()
    assert world.files(call) == [] and world.store.get(call.id).audio_deleted_at is not None


def test_half_files_a_killed_program_left_are_cleared_at_the_next_start(world):
    call = world.call()
    world.t.step()
    waiting = world.call(minutes_later=5)
    world.unavailable = Unavailable(gateway.DOWN, "nobody listens")     # so that the one that waits goes on waiting

    def half(path: Path, hours: int) -> None:
        path.write_bytes(b"half")
        written = (world.today + timedelta(hours=hours)).timestamp()
        os.utime(path, (written, written))

    half(world.store.root / f"{call.folder}.opus.part", -1)
    half(world.store.root / f"{call.folder}.transcript.md.part", -1)
    half(world.store.root / f"{call.folder}.md.part", +1)              # written since this start: somebody is at it
    half(world.store.folder(waiting) / "audio.opus.part", -1)           # as an older version of the program left them
    started_anew(world).step()
    assert world.files(call) == ["*.md.part", "*.opus"]
    assert world.files(waiting) == ["*/line.wav", "*/mic.wav"]


# --- where the files of a conversation lie


def test_the_working_folder_goes_when_there_are_no_recordings_left_in_it(world):
    call = world.call()
    world.t.step()
    assert not world.store.folder(call).exists()
    kept = World(keep_audio_days=0)
    try:
        silent = kept.call(minutes_later=5)
        kept.t.step()
        assert kept.files(silent) == [] and not kept.store.folder(silent).exists()
    finally:
        kept.store.close()


def test_a_recognised_conversation_is_about_its_beginning_until_it_has_a_summary(world):
    call = world.call()
    assert world.store.get(call.id).topic is None
    world.t.step()
    assert sorted(world.store.get(call.id).topic.split(" фраза ")) == ["2", "3", "4", "фраза 1"]


def test_a_working_folder_that_holds_something_else_is_left_with_it(world):
    call = world.call()
    (world.store.folder(call) / "notes.txt").write_text("my own", encoding="utf-8")
    world.t.step()
    assert world.files(call) == ["*.opus", "*/notes.txt"]


def test_sound_that_outlived_its_days_takes_what_is_left_of_the_working_folder_with_it(world):
    call = world.call()
    world.compress_error = RuntimeError("the library fell over")
    world.t.step()
    assert world.files(call) == ["*/line.wav", "*/mic.wav"]
    world.today = AT + timedelta(days=31)
    world.now += transcriber.SWEEP_S
    world.t.step()
    assert world.files(call) == [] and world.store.get(call.id).audio_deleted_at is not None


# --- an archive laid out the older way


SUMMARY = "# Выжимка\n\n## Коротко\nГоворили о плане.\n"


def laid_out_the_older_way(world, call, summary: str | None = SUMMARY) -> Path:
    """Put the files of a recognised conversation where an older version of the program kept them: inside
    its folder, under three names that are the same for every conversation."""
    folder = world.store.folder(call)
    folder.mkdir()
    os.replace(world.store.sound(call), folder / "audio.opus")
    (folder / "transcript.md").write_text("# Разговор\n", encoding="utf-8")
    if summary is not None:
        (folder / "summary.md").write_text(summary, encoding="utf-8")
    return folder


def test_the_files_of_an_older_archive_are_moved_to_where_they_lie_now(world, caplog):
    call = world.call()
    world.t.step()
    folder = laid_out_the_older_way(world, call)
    world.recording = True
    started_anew(world).step()
    assert world.files(call) == ["*/audio.opus", "*/summary.md", "*/transcript.md"]     # not while a conversation is recorded
    world.recording = False
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        started_anew(world).step()
    assert world.files(call) == ["*.md", "*.opus"] and not folder.exists()
    assert "WARNING" not in caplog.text and "ERROR" not in caplog.text
    assert world.store.sound(call).read_bytes() == b"OggS" and len(world.compressed) == 1      # moved, not made again
    made = world.store.summary(call.id)
    assert (made.kind, made.text, made.maker, made.seconds) == (MADE, SUMMARY, None, None)
    assert world.store.summary_file(call).read_text(encoding="utf-8") == SUMMARY
    assert world.store.get(call.id).topic == "Говорили о плане."


def test_sound_of_an_older_archive_that_outlived_its_days_is_deleted_where_it_lies(world):
    call = world.call()
    world.t.step()
    laid_out_the_older_way(world, call)
    world.today = AT + timedelta(days=31)
    started_anew(world).step()                                          # the look for old sound, then the move
    left = world.files(call)
    assert "*.opus" not in left and "*/audio.opus" not in left and "*.md" in left      # the summary stays
    assert world.store.get(call.id).audio_deleted_at is not None


def test_a_conversation_recognised_before_there_was_a_line_about_it_gets_one_at_the_next_start(world):
    call = world.call()
    world.t.step()
    world.store._db.execute("UPDATE calls SET topic = NULL WHERE id = ?", (call.id,))      # as an older version left it
    started_anew(world).step()
    assert world.store.get(call.id).topic.startswith("фраза ")


def test_a_summary_file_that_begins_with_a_byte_order_mark_is_taken_without_it(world):
    call = world.call()
    world.t.step()
    laid_out_the_older_way(world, call, summary="\ufeff" + SUMMARY)     # as Notepad saves a file
    started_anew(world).step()
    assert world.store.summary(call.id).text == SUMMARY and world.store.get(call.id).topic == "Говорили о плане."


def test_an_older_folder_that_holds_something_else_is_left_with_it(world):
    call = world.call()
    world.t.step()
    folder = laid_out_the_older_way(world, call, summary=None)
    (folder / "my notes").mkdir()
    (folder / "my notes" / "notes.md").write_text("my own", encoding="utf-8")
    started_anew(world).step()
    assert world.files(call) == ["*.opus", "*/my notes"]
    assert (folder / "my notes" / "notes.md").read_text(encoding="utf-8") == "my own"
    assert world.store.summary(call.id) is None


def test_a_summary_of_the_archive_is_not_replaced_by_a_file_of_an_older_layout(world):
    call = world.call()
    world.t.step()
    world.store.save_summary(call.id, MINE, "## Коротко\nПереезд в четверг.\n", AT)
    laid_out_the_older_way(world, call)
    started_anew(world).step()
    assert set(world.store.summaries(call.id)) == {MINE} and world.files(call) == ["*.md", "*.opus"]
    assert world.store.summary_file(call).read_text(encoding="utf-8") == "## Коротко\nПереезд в четверг.\n"     # the one in force


def test_files_that_could_not_be_moved_are_moved_at_the_next_start(world, monkeypatch, caplog):
    call = world.call()
    world.t.step()
    laid_out_the_older_way(world, call)
    replace = os.replace

    def held(src, dst):
        if Path(src).name == "audio.opus":
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return replace(src, dst)

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="protocallist"):
        patch.setattr(transcriber.os, "replace", held)
        started_anew(world).step()
    assert "call 1: its files could not be moved" in caplog.text and "PermissionError" in caplog.text
    assert "*/audio.opus" in world.files(call) and world.store.summary(call.id).text == SUMMARY
    started_anew(world).step()
    assert world.files(call) == ["*.md", "*.opus"] and len(world.compressed) == 1


# --- the text of a conversation is in the archive, not in a file


def text_beside(world, call) -> Path:
    """The text for reading as an earlier version of the program wrote it beside the sound."""
    path = world.store.root / f"{call.folder}.transcript.md"
    path.write_text("# Разговор\n", encoding="utf-8")
    return path


def test_the_text_file_an_earlier_version_wrote_is_removed_at_the_next_start(world, caplog):
    call = world.call()
    world.t.step()
    text = text_beside(world, call)
    nobodys = world.store.root / "2026-01-01_00-00-00_Zoom.transcript.md"      # of no conversation of the archive
    nobodys.write_text("# Разговор\n", encoding="utf-8")
    world.recording = True
    started_anew(world).step()
    assert text.exists()                                                # not while a conversation is recorded
    world.recording = False
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        started_anew(world).step()
    assert world.files(call) == ["*.opus"] and caplog.text == ""
    assert nobodys.exists()                                             # only what the archive holds the text of
    assert len(world.store.phrases(call.id)) == 4 and len(world.compressed) == 1


def test_a_conversation_that_is_not_recognised_keeps_whatever_lies_under_its_name(world):
    waiting = world.call()
    text = text_beside(world, waiting)
    world.unavailable = Unavailable(gateway.DOWN, "nobody listens")     # so that it goes on waiting
    started_anew(world).step()
    assert world.store.get(waiting.id).state == RECORDED and text.exists()


def test_a_text_file_that_cannot_be_removed_is_said_in_the_log_and_tried_again_at_the_next_start(world, monkeypatch, caplog):
    call, waiting = world.call(), world.call(minutes_later=5)
    world.t.step()
    text = text_beside(world, call)
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="protocallist"):
        patch.setattr(Path, "unlink", held(text))
        started_anew(world).step()
    assert text.exists() and "call 1: the file of its text could not be deleted (PermissionError)" in caplog.text
    assert world.store.get(waiting.id).state == DONE                    # and recognition went on
    started_anew(world).step()
    assert not text.exists()


# --- small things


def test_a_conversation_forgotten_while_its_sound_was_made_small_leaves_no_sound(world):
    call = world.call()
    made = world.t._compress

    def forgotten_first(mic, line, out):
        world.store.discard(call.id)                                    # the user deleted it at this very moment
        made(mic, line, out)

    world.t._compress = forgotten_first
    world.t.step()
    assert world.store.get(call.id) is None and world.files(call) == []


def test_a_summary_file_that_cannot_be_read_does_not_keep_the_sound_from_being_moved(world, caplog):
    call = world.call()
    world.t.step()
    folder = laid_out_the_older_way(world, call, summary=None)
    (folder / "summary.md").write_bytes("Выжимка".encode("cp1251"))     # saved by hand, and not as UTF-8
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        started_anew(world).step()
    assert world.files(call) == ["*.opus", "*/summary.md"]              # the sound has moved; the file waits where it was
    assert "call 1: its files could not be moved" in caplog.text and "UnicodeDecodeError" in caplog.text
    assert world.store.summary(call.id) is None


def test_a_summary_the_archive_holds_gets_its_file_beside_the_sound_at_a_start(world):
    """Its file could not be written when the summary was brought into the archive: the next start writes it."""
    call = world.call()
    world.t.step()
    world.store.save_summary(call.id, MADE, SUMMARY, AT, "opus", 60.0)
    laid_out_the_older_way(world, call, summary=None)
    started_anew(world).step()
    assert world.files(call) == ["*.md", "*.opus"]
    assert world.store.summary_file(call).read_text(encoding="utf-8") == SUMMARY


def test_an_archive_of_the_first_version_with_its_folders_is_brought_over_in_one_start(caplog):
    """The database as the first version of the program made it, and a conversation as that version laid it
    out - in one go: the first opening of the archive, and the first turn of recognition after it."""
    folder = paths.calls_dir() / "2026-10-05_16-11-51_Zoom"
    folder.mkdir()
    (folder / "audio.opus").write_bytes(b"OggS")
    (folder / "transcript.md").write_text("# Разговор\n", encoding="utf-8")
    (folder / "summary.md").write_text(SUMMARY, encoding="utf-8")
    old = sqlite3.connect(paths.database_file())
    old.executescript("""
        CREATE TABLE calls (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, duration_s REAL,
                            program TEXT NOT NULL, title TEXT, folder TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
                            attempts INTEGER NOT NULL DEFAULT 0, model TEXT, audio_deleted_at TEXT);
        CREATE TABLE phrases (id INTEGER PRIMARY KEY, call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                              speaker TEXT NOT NULL, voice INTEGER, start_s REAL NOT NULL, end_s REAL NOT NULL,
                              text TEXT NOT NULL);
        CREATE INDEX phrases_by_call ON phrases (call_id, start_s);
        INSERT INTO calls (id, started_at, ended_at, duration_s, program, folder, state, model)
            VALUES (7, '2026-10-05T16:11:51+04:00', '2026-10-05T16:12:51+04:00', 60.0, 'Zoom', '2026-10-05_16-11-51_Zoom',
                    'done', 'gigaam-v3');
        INSERT INTO phrases (call_id, speaker, start_s, end_s, text) VALUES (7, 'me', 0.5, 2.0, 'начнём с плана');
        PRAGMA user_version = 1;
    """)
    old.close()
    world = World()
    try:
        with caplog.at_level(logging.WARNING, logger="protocallist"):
            world.t.step()
        call = world.store.get(7)
        assert world.files(call) == ["*.md", "*.opus"] and not folder.exists() and "WARNING" not in caplog.text
        made = world.store.summary(7)
        assert (made.kind, made.text) == (MADE, SUMMARY) and call.topic == "Говорили о плане."
        assert world.store.summary_file(call).read_text(encoding="utf-8") == SUMMARY and len(world.store.phrases(7)) == 1
        assert world.store.begin(AT, "Zoom").id == 8 and world.compressed == []
    finally:
        world.store.close()


def test_the_users_own_summary_survives_a_start_that_finishes_what_another_left_half_done(world, monkeypatch):
    call = world.call()
    with monkeypatch.context() as patch:
        closed_right_after_the_text_went_into_the_archive(world, patch)
        world.t.step()
    world.store.save_summary(call.id, MADE, SUMMARY, AT, "opus", 60.0)
    world.store.save_summary(call.id, MINE, "## Коротко\nСам.\n", AT)
    summaries.settle(world.store, call)
    started_anew(world).step()                                          # makes the sound small, and nothing else
    assert world.files(call) == ["*.md", "*.opus"]
    both = world.store.summaries(call.id)
    assert (both[MINE].text, both[MADE].text) == ("## Коротко\nСам.\n", SUMMARY)
    assert world.store.summary_file(call).read_text(encoding="utf-8") == "## Коротко\nСам.\n"


# --- recognising anew, with another model: from the small file, the text there is kept meanwhile

ENGLISH = "parakeet-unified-en"


@pytest.fixture
def talk(world):
    """A conversation recognised the usual way, its sound in the small file: fifty seconds of the
    user, thirty of the others."""
    world.real = True
    call = world.call(mic_s=50.0, line_s=30.0)
    world.t.step()
    world.pieces.clear()
    world.models.clear()
    world.quiet.clear()
    return world.store.get(call.id)


def test_a_conversation_is_recognised_anew_from_its_small_file_with_the_model_asked_for(world, talk, caplog):
    before, file = world.store.phrases(talk.id), world.store.sound(talk)
    written = file.stat().st_mtime_ns
    assert world.store.redo(talk.id, ENGLISH)
    world.today = AT + timedelta(days=2)
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        world.t.step()
    after = world.store.get(talk.id)
    assert (after.model, after.redo_model, after.redo_error, after.state) == (ENGLISH, None, None, DONE)
    assert datetime.fromisoformat(after.text_at) == (AT + timedelta(days=2)).astimezone()
    assert world.models == [ENGLISH] * 6                                 # every piece named the model
    # the user spoke to the end, the others fell silent after thirty seconds: each side was read as whose it is
    assert world.quiet == [False, False, False, False, False, True]
    assert sum(world.pieces[:3]) == 50 * RATE and sum(world.pieces[3:]) == 50 * RATE     # both sides whole: the file has them equally long
    phrases = world.store.phrases(talk.id)
    assert phrases != before and [p.speaker for p in phrases].count("me") == 3 and [p.speaker for p in phrases].count("them") == 3
    assert phrases == sorted(phrases, key=lambda p: (p.start_s, p.speaker)) and after.topic.startswith("phrase")
    assert world.files(talk) == ["*.opus"] and file.stat().st_mtime_ns == written     # nothing new on disk; the sound is not made anew
    assert world.compressed == [talk.folder] and world.t.status == IDLE
    assert f"call {talk.id} (Zoom, 50 s) is recognised anew with {ENGLISH}: 6 phrases" in caplog.text
    assert "phrase 1" not in caplog.text and "фраза" not in caplog.text


def test_the_user_is_on_the_left_of_the_small_file_and_the_others_on_the_right(world, talk):
    file = world.store.sound(talk)
    mine, theirs = opus.Side(file, 0), opus.Side(file, 1)
    assert mine.frames() == theirs.frames() == 50 * RATE
    loud = lambda side, second: float(np.abs(side.read(second * RATE, RATE).astype(np.float64)).mean())
    assert loud(mine, 40) > 1000 and loud(theirs, 40) < 50               # after thirty seconds only the user is heard
    assert loud(mine, 10) > 1000 and loud(theirs, 10) > 1000
    assert len(mine.read(49 * RATE, 5 * RATE)) == RATE                   # what is past the end is not there
    with pytest.raises(OSError):
        opus.Side(file.with_name("nothing.opus"), 0).frames()


def test_what_waits_for_its_first_text_goes_before_what_is_recognised_anew(world, talk):
    world.store.redo(talk.id, ENGLISH)
    new = world.call(minutes_later=30)
    world.t.step()
    assert world.store.get(new.id).state == DONE and world.store.get(talk.id).redo_model == ENGLISH
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH


def test_the_text_there_is_stays_until_the_new_one_is_whole(world, talk):
    before, seen = world.store.phrases(talk.id), []
    world.store.redo(talk.id, ENGLISH)
    world.then = lambda: seen.append((world.store.phrases(talk.id), world.t.status))
    world.t.step()
    assert len(seen) == 6 and all(told == (before, transcriber.WORKING) for told in seen)       # ...and the icon says that it works


def test_recognition_anew_that_was_refused_leaves_the_text_and_is_tried_once(world, talk, caplog):
    before = world.store.phrases(talk.id)
    world.store.redo(talk.id, ENGLISH)
    world.refusal = Refused("the gateway answered 500: the model could not be loaded: bad file")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
    after = world.store.get(talk.id)
    assert (after.redo_model, after.redo_error, after.model) == (
        None, "the gateway answered 500: the model could not be loaded: bad file", "gigaam-v3")
    assert world.store.phrases(talk.id) == before and len(world.pieces) == 1 and after.attempts == 0 and world.t.status == IDLE
    assert f"call {talk.id}: recognising it anew with {ENGLISH} failed" in caplog.text
    world.refusal = None
    world.t.step()
    assert len(world.pieces) == 1                                        # one attempt: it is not tried by itself again


def test_a_model_that_is_not_in_place_and_sound_that_is_gone_are_said_in_words_the_page_knows(world, talk):
    world.store.redo(talk.id, ENGLISH)
    world.refusal = gateway.NoModel("the files of the model are not on this computer")
    world.t.step()
    assert world.store.get(talk.id).redo_error == transcriber.NO_MODEL
    world.refusal = None
    world.store.redo(talk.id, ENGLISH)
    world.store.sound(talk).unlink()
    world.t.step()
    assert world.store.get(talk.id).redo_error == transcriber.NO_SOUND and len(world.pieces) == 1     # the gateway was not even asked


def test_a_gateway_is_not_asked_to_read_with_a_model_it_does_not_say_it_has(world, talk):
    """It reads with its main model whatever name it does not know: the text would be the old one over again."""
    for known, why in (([gateway.Model("gigaam-v3", "ru", True, True)], transcriber.NO_MODEL),
                       ([gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, False)], transcriber.NO_MODEL),
                       (None, transcriber.NO_CHOICE)):
        world.known = known
        assert world.store.redo(talk.id, ENGLISH)
        world.t.step()
        after = world.store.get(talk.id)
        assert (after.redo_model, after.redo_error, after.model, world.pieces) == (None, why, "gigaam-v3", []), known
    world.known = [gateway.Model("gigaam-v3", "ru", True, True)]
    world.store.redo(talk.id, "gigaam-v3")                              # the main one is a model like any other
    world.t.step()
    assert world.store.get(talk.id).redo_error is None and world.models == ["gigaam-v3"] * 6


def test_recognition_anew_waits_for_a_gateway_that_is_not_there(world, talk):
    world.store.redo(talk.id, ENGLISH)
    world.unavailable = Unavailable(gateway.DOWN)
    world.t.step()
    assert world.store.get(talk.id).redo_model == ENGLISH and world.t.status == gateway.DOWN
    world.unavailable = None
    world.t.step()                                                       # asked again only after a minute
    assert world.store.get(talk.id).redo_model == ENGLISH
    world.now += 61
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH and world.store.get(talk.id).redo_error is None


def test_recognition_anew_gives_way_to_a_conversation_that_began_and_asks_for_no_piece_twice(world, talk):
    world.store.redo(talk.id, ENGLISH)

    def begins():
        world.recording = len(world.pieces) == 2

    world.then = begins
    world.t.step()
    assert world.store.get(talk.id).redo_model == ENGLISH and len(world.pieces) == 2
    world.recording, world.then = False, None
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH and len(world.pieces) == 6


def test_a_conversation_forgotten_while_it_was_recognised_anew_gets_no_text(world, talk, caplog):
    world.store.redo(talk.id, ENGLISH)

    def forgotten():
        if len(world.pieces) == 6:       # the last piece is being read: nothing fails, there is nobody to give the text to
            world.store.discard(talk.id)

    world.then = forgotten
    with caplog.at_level(logging.INFO, logger="protocallist"):
        world.t.step()
    assert world.store.get(talk.id) is None and world.store.phrases(talk.id) == [] and "Traceback" not in caplog.text
    assert world.t.status == IDLE and "is recognised anew" not in caplog.text


def test_what_one_model_read_is_not_taken_for_what_another_reads(world, talk):
    world.store.redo(talk.id, ENGLISH)

    def refused_at_the_second():
        if len(world.pieces) == 1:
            world.refusal = Refused("the gateway answered 500: boom")

    world.then = refused_at_the_second
    world.t.step()                                                       # one piece read by the English model, then a refusal
    assert world.store.get(talk.id).redo_error and world.models == [ENGLISH] * 2
    world.then = world.refusal = None
    world.store.redo(talk.id, "gigaam-v3")
    world.t.step()
    assert world.models[2:] == ["gigaam-v3"] * 6 and not world.store.phrases(talk.id)[0].text.startswith("phrase 1")


def test_a_conversation_asked_for_once_more_is_read_once_more(world, talk):
    for _ in range(2):
        world.store.redo(talk.id, ENGLISH)
        world.t.step()
    assert world.models == [ENGLISH] * 12 and world.store.get(talk.id).redo_error is None


def test_a_small_file_that_is_no_sound_is_said_so_and_the_text_stays(world, talk, caplog):
    before = world.store.phrases(talk.id)
    world.store.sound(talk).write_bytes(b"not sound at all " * 100)
    world.store.redo(talk.id, ENGLISH)
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
    after = world.store.get(talk.id)
    assert after.redo_error == transcriber.BAD_SOUND and after.redo_model is None     # in a word the page knows...
    assert "failed (the sound cannot be read (" in caplog.text           # ...and in the log what the library said
    assert world.store.phrases(talk.id) == before and world.pieces == [] and "Traceback" not in caplog.text


def test_what_was_asked_for_before_the_program_was_closed_is_recognised_anew_after_it_starts(world, talk):
    world.store.redo(talk.id, ENGLISH)

    def begins():
        world.recording = len(world.pieces) == 2

    world.then = begins
    world.t.step()                                                       # two pieces read - and the program is closed
    world.recording, world.then = False, None
    again = started_anew(world)
    again.step()
    assert world.store.get(talk.id).model == ENGLISH and len(world.pieces) == 2 + 6      # from the beginning: what was read is gone


def test_the_first_text_of_a_conversation_knows_when_it_was_written(world):
    call = world.call()
    world.today = AT + timedelta(days=3)
    world.t.step()
    assert datetime.fromisoformat(world.store.get(call.id).text_at) == (AT + timedelta(days=3)).astimezone()


def test_when_what_waited_for_the_gateway_is_deleted_nothing_waits_any_more(world, talk):
    world.store.redo(talk.id, ENGLISH)
    world.unavailable = Unavailable(gateway.DOWN)
    world.t.step()
    assert world.t.status == gateway.DOWN
    world.store.discard(talk.id)
    world.now += 61
    world.t.step()
    assert world.t.status == IDLE                                        # the icon does not go on saying that something waits


def test_a_small_file_that_is_not_what_the_program_keeps_is_not_read(tmp_path):
    import soundfile

    mono, fast = tmp_path / "mono.opus", tmp_path / "fast.opus"
    soundfile.write(str(mono), np.zeros(RATE, dtype=np.int16), RATE, format="OGG", subtype="OPUS")
    soundfile.write(str(fast), np.zeros((48000, 2), dtype=np.int16), 48000, format="OGG", subtype="OPUS")
    for path in (mono, fast):                                            # one side only; another rate: its text would be anything
        with pytest.raises(OSError, match="not what the program keeps"):
            opus.Side(path, 0).frames()
        with pytest.raises(OSError, match="not what the program keeps"):
            opus.Side(path, 1).read(0, RATE)
        path.unlink()                                                    # ...and the file is let go of


def test_what_was_read_anew_is_not_read_again_after_a_new_conversation_got_its_text_in_between(world, talk):
    world.store.redo(talk.id, ENGLISH)

    def begins():
        world.recording = len(world.pieces) == 2

    world.then = begins
    world.t.step()                                                       # two pieces read - and a call begins
    world.recording, world.then = False, None
    new = world.call(minutes_later=30)                                   # ...and ends: there is a recording to recognise
    world.t.step()
    assert world.store.get(new.id).state == DONE and world.store.get(talk.id).redo_model == ENGLISH
    asked = len(world.pieces)
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH and len(world.pieces) - asked == 4     # only the four that were left


def test_recognition_anew_steps_aside_for_a_conversation_that_waits_for_its_first_text(world, talk):
    world.store.redo(talk.id, ENGLISH)
    came = []

    def ended_meanwhile():                       # a short call began and ended while one piece was being read
        if len(world.pieces) == 2 and not came:
            came.append(world.call(minutes_later=30))

    world.then = ended_meanwhile
    world.t.step()
    assert len(world.pieces) == 2 and world.store.get(talk.id).redo_model == ENGLISH and world.t.status == IDLE
    world.then = None
    world.t.step()
    assert world.store.get(came[0].id).state == DONE and world.store.get(talk.id).model == "gigaam-v3"     # the new one first
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH


def test_a_request_taken_back_in_the_middle_stops_the_reading_and_changes_nothing(world, talk, caplog):
    before = world.store.phrases(talk.id)
    world.store.redo(talk.id, ENGLISH)

    def taken_back():
        if len(world.pieces) == 2:
            world.store.cancel_redo(talk.id)

    world.then = taken_back
    with caplog.at_level(logging.INFO, logger="protocallist"):
        world.t.step()
        world.t.step()
    after = world.store.get(talk.id)
    assert len(world.pieces) == 2 and (after.redo_model, after.redo_error, after.model) == (None, None, "gigaam-v3")
    assert world.store.phrases(talk.id) == before and "anew" not in caplog.text and world.t.status == IDLE


def test_a_conversation_deleted_in_the_middle_is_read_no_further_and_nothing_is_said_of_it(world, talk, caplog):
    world.store.redo(talk.id, ENGLISH)

    def deleted():
        if len(world.pieces) == 2:
            world.store.discard(talk.id)

    world.then = deleted
    with caplog.at_level(logging.INFO, logger="protocallist"):
        world.t.step()
        world.t.step()
    assert len(world.pieces) == 2 and world.store.get(talk.id) is None and "anew" not in caplog.text
    assert "Traceback" not in caplog.text and world.t.status == IDLE


def test_a_model_the_gateway_could_not_load_and_a_text_without_a_word_leave_the_text_there_is(world, talk, caplog):
    before = world.store.phrases(talk.id)
    world.store.redo(talk.id, ENGLISH)
    world.refusal = gateway.ModelFailed("the gateway answered 500: the model could not be loaded: not enough memory")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
    assert world.store.get(talk.id).redo_error == transcriber.NO_LOAD and "not enough memory" in caplog.text
    world.refusal, world.wordless = None, True
    world.store.redo(talk.id, ENGLISH)
    world.t.step()
    after = world.store.get(talk.id)
    assert (after.redo_error, after.model) == (transcriber.NO_WORDS, "gigaam-v3") and world.store.phrases(talk.id) == before
    world.wordless, asked = False, len(world.pieces)
    world.store.redo(talk.id, ENGLISH)                                   # asked once more, it is read once more:
    world.t.step()                                                       # what the model did not hear then is not remembered
    assert world.store.get(talk.id).model == ENGLISH and len(world.pieces) - asked == 6


def test_a_gateway_that_stumbles_over_the_question_of_its_models_is_waited_for(world, talk):
    world.store.redo(talk.id, ENGLISH)
    world.known = Unavailable(gateway.DOWN, "TimeoutError when the gateway was asked for its models")
    world.t.step()
    assert world.store.get(talk.id).redo_model == ENGLISH and world.t.status == gateway.DOWN and world.pieces == []
    world.known = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, True)]
    world.now += 61
    world.t.step()
    assert world.store.get(talk.id).model == ENGLISH


def test_a_program_that_leaves_while_the_new_text_is_written_says_nothing_and_the_request_stays(world, talk, caplog):
    world.store.redo(talk.id, ENGLISH)
    whole = world.store.redone

    def leaving(*args):
        world.t._stop.set()                      # the program is leaving, and the archive is closed under the thread
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    world.store.redone = leaving
    with caplog.at_level(logging.WARNING, logger="protocallist"), pytest.raises(sqlite3.ProgrammingError):
        world.t.step()
    world.store.redone = whole
    assert caplog.text == "" and world.store.get(talk.id).redo_model == ENGLISH


def test_an_attempt_that_ends_does_not_write_its_text_over_a_request_made_meanwhile_with_another_model(world, talk, caplog):
    before = world.store.phrases(talk.id)
    world.store.redo(talk.id, ENGLISH)

    def another():                               # the last piece is at the gateway: taken back, and asked for with another model
        if len(world.pieces) == 6:
            world.store.cancel_redo(talk.id)
            world.store.redo(talk.id, "gigaam-v3")

    world.meanwhile = another
    with caplog.at_level(logging.INFO, logger="protocallist"):
        world.t.step()
    after = world.store.get(talk.id)
    assert (after.redo_model, after.redo_error, after.model) == ("gigaam-v3", None, "gigaam-v3")      # the new request stands
    assert world.store.phrases(talk.id) == before and "is recognised anew" not in caplog.text
    world.meanwhile = None
    world.t.step()                                                       # ...and is the next to be read, whole
    assert world.store.get(talk.id).redo_model is None and world.models[6:] == ["gigaam-v3"] * 6


def test_an_attempt_that_fails_says_nothing_of_a_request_made_meanwhile_nor_of_one_taken_back(world, talk, caplog):
    for asked_again in (True, False):
        world.store.redo(talk.id, ENGLISH)
        world.pieces.clear()

        def gone():                              # the last piece is at the gateway, which is about to refuse it
            if len(world.pieces) == 6:
                world.store.cancel_redo(talk.id)
                if asked_again:
                    world.store.redo(talk.id, "another")
                world.refusal = Refused("the gateway answered 500: boom")

        world.meanwhile = gone
        with caplog.at_level(logging.INFO, logger="protocallist"):
            world.t.step()
        after = world.store.get(talk.id)
        assert (after.redo_model, after.redo_error) == ("another" if asked_again else None, None), asked_again
        assert "failed" not in caplog.text, asked_again                  # nothing is said of an attempt that is nobody's
        world.meanwhile = world.refusal = None
        world.store.cancel_redo(talk.id)


def test_an_answer_that_did_not_come_in_time_is_said_in_a_word_the_page_knows(world, talk, caplog):
    world.store.redo(talk.id, ENGLISH)
    world.refusal = gateway.TimedOut("no answer in 600 s")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.t.step()
    after = world.store.get(talk.id)
    assert (after.redo_error, after.redo_model) == (transcriber.NO_ANSWER, None) and "failed (no answer in 600 s)" in caplog.text


def test_sound_that_fails_in_the_middle_of_the_file_is_sound_that_cannot_be_read(world, talk, monkeypatch):
    import soundfile

    def broken(self, frames, *args, **kwargs):
        raise RuntimeError("the file is damaged at this place")

    side = opus.Side(world.store.sound(talk), 0)
    assert side.frames() == 50 * RATE
    monkeypatch.setattr(soundfile.SoundFile, "seek", broken)
    with pytest.raises(OSError, match=r"the sound cannot be read \(RuntimeError\)"):
        side.read(10 * RATE, RATE)
    world.store.redo(talk.id, ENGLISH)
    world.t.step()
    assert world.store.get(talk.id).redo_error == transcriber.BAD_SOUND and world.pieces == []


def test_a_long_conversation_deleted_while_it_is_read_leaves_no_word_of_a_failure_in_the_log(world, talk, caplog):
    """The next piece is looked for in the file before anybody asks whether the conversation is still there."""
    world.store.redo(talk.id, ENGLISH)

    def deleted():
        if len(world.pieces) == 1:
            world.store.discard(talk.id)

    world.then = deleted
    with caplog.at_level(logging.INFO, logger="protocallist"):
        world.t.step()
        world.t.step()
    assert world.store.get(talk.id) is None and len(world.pieces) == 1
    assert "failed" not in caplog.text and "cannot be read" not in caplog.text and "Traceback" not in caplog.text
    assert world.t.status == IDLE
