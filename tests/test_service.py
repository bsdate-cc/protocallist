import logging
import threading
from datetime import datetime

import numpy as np
import pytest

from protocallist import paths
from protocallist.audio import RATE, WavWriter, read
from protocallist.config import RecordingConfig
from protocallist.recorder import RecorderError
from protocallist.service import Service, mend
from protocallist.store import RECORDED, Store

ZOOM = ["Zoom.exe"]
FREE: list[str] = []


class FakeRecorder:
    """Stands in for the recorder: remembers what it was told and leaves a file like the real one."""

    def __init__(self, log: list, fail: bool = False) -> None:
        self.log, self.fail = log, fail
        self.zero = 0.0
        self.failed: Exception | None = None

    def start(self, folder, now):
        if self.fail:
            raise RecorderError("the microphone would not open")
        self.zero = now
        (folder / "mic.wav").write_bytes(b"sound")
        self.log.append("start")

    def release_mic(self):
        self.log.append("release")

    def resume_mic(self):
        self.log.append("resume")

    def stop(self, end):
        self.log.append(f"stop {end - self.zero:g}")
        return end - self.zero

    def abort(self):
        self.log.append("abort")


class World:
    """The clock, the microphone and the recorder of a test, and the service among them."""

    def __init__(self, **settings) -> None:
        self.now = 500.0
        self.holders: list[str] | None = FREE
        self.log: list[str] = []
        self.fail = False
        self.asked: list[str] = []
        self.store = Store(paths.database_file(), paths.calls_dir())
        cfg = RecordingConfig(enabled=True, microphone="Headset Microphone", **settings)
        self.service = Service(cfg, self.store, self._holders, lambda: FakeRecorder(self.log, self.fail),
                               title=lambda program: f"window of {program}", clock=lambda: self.now,
                               today=lambda: datetime(2026, 10, 5, 16, 11, 51))

    def _holders(self, microphone):
        self.asked.append(microphone)
        return self.holders

    def at(self, seconds: float, holders) -> None:
        """`seconds` after the test began the microphone is held by `holders`, and the service looks."""
        self.now, self.holders = 500.0 + seconds, holders
        self.service.tick()


@pytest.fixture
def world():
    w = World()
    yield w
    w.store.close()


def test_a_conversation_is_recorded_and_waits_for_recognition(world):
    world.at(0, FREE)
    world.at(1, ZOOM)
    assert world.service.call.program == "Zoom" and world.asked[-1] == "Headset Microphone"
    world.at(2, ZOOM)
    world.at(301, FREE)                 # the microphone was let go of five minutes in
    world.at(361, FREE)                 # a minute later that is certain
    assert world.log == ["start", "release", "stop 300"]
    assert world.service.call is None
    [call] = world.store.calls()
    assert (call.state, call.program, call.title, call.duration_s) == (RECORDED, "Zoom", "window of Zoom.exe", 300.0)
    assert (world.store.folder(call) / "mic.wav").is_file()


def test_the_recorder_is_told_when_the_microphone_goes_and_comes_back(world):
    world.at(0, ZOOM)
    world.at(100, FREE)
    world.at(130, ZOOM)
    world.at(400, FREE)
    world.at(460, FREE)
    assert world.log == ["start", "release", "resume", "release", "stop 400"]


def test_too_short_to_be_a_conversation_leaves_nothing(world):
    # A program that touched the microphone for a moment: nobody talks for three seconds.
    world.at(0, ZOOM)
    world.at(3, FREE)
    world.at(70, FREE)
    assert world.log == ["start", "release", "stop 3"]
    assert world.store.calls() == [] and list(paths.calls_dir().iterdir()) == []


def test_a_call_of_a_quarter_of_a_minute_is_a_conversation_too(world):
    # "Where are you? - Coming." What is thrown away cannot be brought back; what is kept can be hidden.
    world.at(0, ZOOM)
    world.at(17, FREE)
    world.at(70, FREE)
    assert world.log == ["start", "release", "stop 17"]
    assert len(world.store.calls(RECORDED)) == 1


def test_a_recording_that_would_not_start_is_tried_again_and_said_once(world, caplog):
    world.fail = True
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.at(0, ZOOM)
        world.at(1, ZOOM)
        world.at(2, ZOOM)
    assert caplog.text.count("is not being recorded") == 1
    assert world.store.calls() == [] and list(paths.calls_dir().iterdir()) == []
    world.fail = False
    world.at(3, ZOOM)                   # the microphone opens at last
    world.at(100, FREE)
    world.at(160, FREE)
    assert world.log == ["start", "release", "stop 97"]
    assert len(world.store.calls(RECORDED)) == 1


def test_a_recording_thrown_away_leaves_nothing_and_the_same_call_is_not_recorded_again(world):
    world.at(0, ZOOM)
    world.service.discard_current()
    assert world.log == ["start", "abort"]
    assert world.service.call is None and world.store.calls() == [] and list(paths.calls_dir().iterdir()) == []
    world.at(5, ZOOM)                   # the call goes on
    world.at(200, FREE)
    assert world.log == ["start", "abort"]
    world.at(261, FREE)                 # a minute of a free microphone
    world.at(262, ZOOM)                 # the next call
    assert world.log == ["start", "abort", "start"]
    world.service.discard_current()
    world.service.discard_current()     # nothing is going on: nothing to throw away


def test_a_recording_that_can_no_longer_be_written_is_ended_and_kept_and_not_begun_again(world):
    world.at(0, ZOOM)
    world.service._recorder.failed = OSError(28, "No space left on device")
    world.at(100, ZOOM)                 # the service sees it at its next look
    assert world.log == ["start", "stop 100"]
    assert [c.state for c in world.store.calls()] == [RECORDED]
    world.at(101, ZOOM)                 # the call goes on, the disk is still full
    world.at(150, ZOOM)
    assert world.log == ["start", "stop 100"] and world.service.call is None


def test_leaving_ends_the_conversation_that_is_going_on_and_keeps_it(world):
    world.at(0, ZOOM)
    world.now = 500.0 + 45
    world.service.close()
    assert world.log == ["start", "stop 45"]
    assert [c.state for c in world.store.calls()] == [RECORDED]
    world.service.close()               # twice is not an error


def test_the_loop_survives_a_look_that_failed_and_ends_the_conversation_when_told_to_stop(caplog):
    world = World(poll_seconds=0.01)
    looks = []

    def holders(microphone):
        looks.append(1)
        if len(looks) == 2:
            raise OSError("Windows did not answer")
        if len(looks) == 6:
            stop.set()
        world.now += 20
        return ZOOM

    world.service._holders = holders
    stop = threading.Event()
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.service.run(stop)
    assert "a look at the microphone failed" in caplog.text
    assert world.log[0] == "start" and world.log[-1].startswith("stop")
    assert len(world.store.calls(RECORDED)) == 1
    world.store.close()


def test_what_a_dead_process_left_is_mended_to_one_length(tmp_path):
    long, short = WavWriter(tmp_path / "line.wav"), WavWriter(tmp_path / "mic.wav")
    long.write(np.full(3 * RATE + 7, 0.5, dtype=np.float32))
    short.write(np.full(RATE, 0.5, dtype=np.float32))
    for writer in (long, short):
        writer._file.flush()            # the process dies here: the files are never closed
    assert mend(tmp_path) == pytest.approx(3 + 7 / RATE)
    mic = read(tmp_path / "mic.wav")
    assert len(mic) == len(read(tmp_path / "line.wav")) == 3 * RATE + 7
    assert mic[RATE - 1] > 0 and np.all(mic[RATE:] == 0)
    for writer in (long, short):
        writer._file.close()


def test_a_folder_with_one_recording_or_none(tmp_path):
    assert mend(tmp_path) == 0.0
    writer = WavWriter(tmp_path / "mic.wav")
    writer.write(np.zeros(RATE, dtype=np.float32))
    writer.close()
    assert mend(tmp_path) == 1.0 and not (tmp_path / "line.wav").exists()


# --- nothing between "a program took the microphone" and "the recording runs" may lose the conversation


def test_a_recorder_that_cannot_even_be_made_is_tried_again_and_leaves_nothing_behind(caplog):
    world = World()
    make, gone = world.service._make_recorder, [True]

    def sometimes():
        if gone[0]:
            raise OSError("the headset went away while we asked about it")
        return make()

    world.service._make_recorder = sometimes
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.at(0, ZOOM)
        world.at(1, ZOOM)
    assert world.store.calls() == []                         # nothing is left "being recorded"
    assert list(paths.calls_dir().iterdir()) == []           # and no folder
    assert len([r for r in caplog.records if "is not being recorded" in r.getMessage()]) == 1
    gone[0] = False
    world.at(2, ZOOM)                                        # the next look tries again, and now it works
    assert world.log == ["start"]
    assert world.service.call is not None


def test_a_database_that_would_not_take_the_conversation_is_tried_again_too():
    world = World()
    begin, full = world.store.begin, [True]

    def sometimes(*args, **kwargs):
        if full[0]:
            raise OSError("no space left on device")
        return begin(*args, **kwargs)

    world.store.begin = sometimes
    world.at(0, ZOOM)
    assert world.log == [] and world.service.call is None
    full[0] = False
    world.at(1, ZOOM)
    assert world.log == ["start"]


def test_the_service_says_that_its_last_recording_failed_until_one_begins_well(world):
    assert world.service.failed is False
    world.at(0, ZOOM)
    world.service._recorder.failed = OSError(28, "No space left on device")
    world.at(100, ZOOM)
    assert world.service.failed is True                      # the icon shows an error for as long as this says so
    world.at(200, FREE)
    world.at(300, FREE)                                      # the microphone was free for more than a minute
    assert world.service.failed is True
    world.at(400, ZOOM)                                      # the next conversation is recorded
    assert world.service.call is not None and world.service.failed is False


def test_the_service_says_that_a_conversation_is_not_recorded_until_one_begins_well(world):
    assert world.service.refused is False
    world.fail = True
    world.at(0, ZOOM)
    assert world.service.refused is True and world.service.call is None
    world.at(100, FREE)
    world.at(300, FREE)                                      # that conversation is over, and nothing of it is kept
    assert world.service.refused is True                     # the icon shows an error for as long as this says so
    world.fail = False
    world.at(400, ZOOM)
    assert world.service.call is not None and world.service.refused is False


def test_a_conversation_is_over_a_few_seconds_after_the_microphone_is_let_go_of(world):
    # The settings as they come. A meeting that was left is over; the next one, in the same
    # program a quarter of a minute later, is a conversation of its own.
    world.at(0, ZOOM)
    world.at(100, FREE)
    world.at(104, FREE)
    assert world.service.call is not None                    # a device that is opened anew is back within this
    world.at(105, FREE)
    assert world.service.call is None and len(world.store.calls(RECORDED)) == 1
    assert world.log == ["start", "release", "stop 100"]     # it ends where the microphone was let go of
    world.at(120, ZOOM)
    assert world.service.call is not None and world.log.count("start") == 2
