"""The service behind the icon: what the icon is shown, and what the menu can ask for."""
import json
import logging
import sqlite3
import time

from protocallist import app as core
from protocallist import config, gateway, paths, transcriber
from protocallist.app import ERROR, MISSING, NO_MICROPHONE, OFF, RECORDING, WAITING, App, Glance

MIC = "Headset Microphone"


class FakeCall:
    def __init__(self, number: int, program: str) -> None:
        self.id, self.program = number, program


class FakeService:
    """Stands in for the recording service: asks who holds the microphone when told to look."""

    def __init__(self, cfg, store, holders, log) -> None:
        self.cfg, self.store, self.holders, self.log = cfg, store, holders, log
        self.call = None
        self.failed = False
        self.refused = False
        self.broken = False
        log.append(f"service for {cfg.microphone}")

    def tick(self) -> None:
        if self.broken:
            raise RuntimeError("the sound card threw something")
        self.holders(self.cfg.microphone)

    def discard_current(self) -> None:
        self.log.append("discard")
        self.call = None

    def close(self) -> None:
        self.log.append("close")


class FakeStore:
    def __init__(self, log) -> None:
        self.log = log
        self.states: dict[str, int] = {}                 # how many conversations there are in each state
        self.broken: Exception | None = None

    def recover(self, length_s, min_seconds) -> None:
        self.log.append(f"recover {min_seconds:g}")

    def counts(self) -> dict[str, int]:
        if self.broken is not None:
            raise self.broken
        return dict(self.states)

    def retry_failed(self) -> int:
        self.log.append("retry")
        return self.states.pop("failed", 0)

    def close(self) -> None:
        self.log.append("store closed")


class FakeTranscriber:
    """Stands in for recognition: remembers what it was made with and what it was told."""

    def __init__(self, cfg, store, busy, log) -> None:
        self.cfg, self.store, self.busy, self.log = cfg, store, busy, log
        self.status = transcriber.IDLE

    def start(self) -> None:
        self.log.append("recognition started")

    def stop(self) -> None:
        self.log.append("recognition stopped")


class FakeSummariser:
    """Stands in for what makes summaries: remembers what it was made with."""

    def __init__(self, cfg, store) -> None:
        self.cfg, self.store, self.halted = cfg, store, 0

    def halt(self) -> int:
        self.halted += 1
        return 0


class World:
    """An App with everything around it faked, turned by hand."""

    def __init__(self, recognition: bool = False, summaries: bool = False, **recording) -> None:
        if recording:
            paths.config_file().write_text(json.dumps({"recording": recording}), encoding="utf-8")
        self.now = 1000.0
        self.log: list[str] = []
        self.holding: list[str] | None = []
        self.names = ["Headset Microphone", "Webcam"]
        self.database_error: Exception | None = None
        self.changes = 0
        self.archive = False                             # is there an archive on disk already
        self.store: FakeStore | None = None
        self.recognition: FakeTranscriber | None = None
        self.summariser: FakeSummariser | None = None
        self.app = App(open_store=self._open, make_service=self._make, holders=lambda microphone: self.holding,
                       microphones=lambda: list(self.names), on_change=self._changed, clock=lambda: self.now,
                       make_transcriber=self._recognise if recognition else None, has_archive=lambda: self.archive,
                       make_summariser=self._summarise if summaries else None)
        self.app.load()

    def _open(self):
        if self.database_error is not None:
            raise self.database_error
        self.log.append("store opened")
        self.store = FakeStore(self.log)
        return self.store

    def _recognise(self, cfg, store, busy):
        self.recognition = FakeTranscriber(cfg, store, busy, self.log)
        return self.recognition

    def _summarise(self, cfg, store):
        self.summariser = FakeSummariser(cfg, store)
        return self.summariser

    def _make(self, cfg, store, holders):
        self.service = FakeService(cfg, store, holders, self.log)
        return self.service

    def _changed(self) -> None:
        self.changes += 1

    def turn(self, seconds: float = 0.0) -> Glance:
        self.now += seconds
        self.app.step()
        return self.app.glance()


def saved() -> dict:
    return json.loads(paths.config_file().read_text(encoding="utf-8"))["recording"]


def test_a_new_installation_records_nothing_and_says_so():
    world = World()
    glance = world.turn()
    assert glance.state == OFF and glance.enabled is False
    assert world.log == []                                   # no database, no service: nothing was touched


def test_the_icon_is_given_the_microphones_to_choose_from():
    world = World()
    assert world.turn().microphones == ("Headset Microphone", "Webcam")
    world.names.append("USB Microphone")
    assert len(world.turn(1.0).microphones) == 2             # the list is read anew every few seconds...
    assert len(world.turn(core.MICROPHONES_EVERY_S).microphones) == 3      # ...not at every look


def test_switched_on_without_a_microphone_it_waits_for_one_to_be_chosen():
    world = World(enabled=True)
    assert world.turn().state == NO_MICROPHONE
    assert world.log == []


def test_switched_on_with_a_microphone_it_watches_it():
    world = World(enabled=True, microphone=MIC)
    glance = world.turn()
    assert glance.state == WAITING and (glance.enabled, glance.microphone) == (True, MIC)
    assert world.log == ["store opened", "recover 5", f"service for {MIC}"]
    world.turn(1.0)
    assert world.log == ["store opened", "recover 5", f"service for {MIC}"]       # made once


def test_a_microphone_that_is_not_there_is_said_and_waited_for():
    world = World(enabled=True, microphone=MIC)
    world.holding = None                                     # unplugged
    assert world.turn().state == MISSING
    world.holding = []
    assert world.turn(1.0).state == WAITING


def test_a_conversation_is_shown_with_its_program_and_its_minutes():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.call = FakeCall(7, "Zoom")
    glance = world.turn(1.0)
    assert (glance.state, glance.program, glance.minutes) == (RECORDING, "Zoom", 0)
    assert world.turn(125.0).minutes == 2
    world.service.call = FakeCall(8, "Discord")              # the next one starts from nothing
    glance = world.turn(1.0)
    assert (glance.program, glance.minutes) == ("Discord", 0)
    world.service.call = None
    assert world.turn(1.0).state == WAITING


def test_the_icon_is_told_when_there_is_something_new_to_show_and_only_then():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    assert world.changes == 1
    world.turn(1.0)
    world.turn(1.0)
    assert world.changes == 1                                # nothing new
    world.service.call = FakeCall(7, "Zoom")
    world.turn(1.0)
    world.turn(30.0)                                         # the same minute
    assert world.changes == 2
    world.turn(40.0)                                         # the next one
    assert world.changes == 3


def test_recording_is_switched_on_from_the_menu_and_the_settings_keep_it():
    world = World(microphone=MIC)
    world.turn()
    world.app.set_enabled(True)
    assert world.turn().state == WAITING
    assert saved()["enabled"] is True and saved()["microphone"] == MIC
    assert config.load().problems == []


def test_switching_off_ends_the_conversation_that_is_going_on_and_keeps_it():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.call = FakeCall(7, "Zoom")
    world.app.set_enabled(False)
    assert world.turn().state == OFF
    assert world.log[-1] == "close"                          # ended, not thrown away
    assert saved()["enabled"] is False


def test_another_microphone_from_the_menu_is_watched_from_then_on():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.app.set_microphone("Webcam")
    glance = world.turn()
    assert glance.microphone == "Webcam" and saved()["microphone"] == "Webcam"
    assert world.log[-2:] == ["close", "service for Webcam"]


def test_choosing_the_same_microphone_again_disturbs_nothing():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.app.set_microphone(MIC)
    world.turn()
    assert "close" not in world.log


def test_the_recording_that_is_going_on_can_be_thrown_away():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.call = FakeCall(7, "Zoom")
    world.app.discard_current()
    assert world.turn().state == WAITING
    assert "discard" in world.log
    world.app.discard_current()                              # with nothing going on it is not an error
    world.turn()


def test_what_the_user_wrote_into_the_settings_by_hand_survives_a_change_from_the_menu():
    paths.config_file().write_text(json.dumps({"recording": {"ignore": ["notes.exe"], "linger_seconds": 90},
                                                "storage": {"keep_audio_days": 7}}), encoding="utf-8")
    world = World()
    world.app.set_microphone(MIC)
    world.turn()
    written = json.loads(paths.config_file().read_text(encoding="utf-8"))
    assert written["recording"]["ignore"] == ["notes.exe"] and written["recording"]["linger_seconds"] == 90
    assert written["storage"]["keep_audio_days"] == 7


def test_settings_that_cannot_be_read_record_nothing_and_are_never_written_over(caplog):
    broken = '{"recording": {"enabled": true,'
    paths.config_file().write_text(broken, encoding="utf-8")
    world = World()
    glance = world.turn()
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_SETTINGS)
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.app.set_enabled(True)
        world.turn()
    assert paths.config_file().read_text(encoding="utf-8") == broken
    assert "the change from the menu is not saved" in caplog.text
    assert world.log == []


def test_a_database_that_cannot_be_opened_is_an_error_said_once_and_tried_again(caplog):
    world = World(enabled=True, microphone=MIC)
    world.database_error = sqlite3.DatabaseError("file is not a database")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        glance = world.turn()
        world.turn(1.0)
        world.turn(core.RETRY_S)                             # tried again, and it is still broken
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_DATABASE)
    assert len([r for r in caplog.records if "the database cannot be opened" in r.getMessage()]) == 1
    world.database_error = None
    assert world.turn(1.0).state == ERROR                    # not asked again at every look...
    assert world.turn(core.RETRY_S).state == WAITING         # ...but after a while


def test_a_recording_that_stopped_by_itself_is_an_error_until_the_next_one_goes_well():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.failed = True
    glance = world.turn(1.0)
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_RECORDING)
    world.service.failed = False
    assert world.turn(1.0).state == WAITING


def test_a_conversation_that_is_not_recorded_is_an_error_until_one_goes_well():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.refused = True                             # the headset would not open
    glance = world.turn(1.0)
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_REFUSED)
    world.service.refused = False
    world.service.call = FakeCall(7, "Zoom")
    assert world.turn(1.0).state == RECORDING


def test_a_change_from_the_menu_that_cannot_be_saved_is_logged_and_changes_nothing(monkeypatch, caplog):
    world = World(enabled=True, microphone=MIC)
    world.turn()

    def full(cfg, path=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(core.config, "save", full)
    world.app.set_microphone("Webcam")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        glance = world.turn()
    assert "what the menu asked for could not be done" in caplog.text
    assert (glance.state, glance.microphone) == (WAITING, MIC)       # as it was: nothing is half-changed
    assert "close" not in world.log


def test_a_look_that_fails_is_logged_and_the_next_one_is_made(caplog):
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.broken = True
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.turn(1.0)
    assert "a look at the microphone failed" in caplog.text
    world.service.broken = False
    assert world.turn(1.0).state == WAITING


def test_a_look_that_keeps_failing_is_an_error_on_the_icon_and_is_said_once(caplog):
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.broken = True                              # Windows no longer answers about the microphone
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        glance = world.turn(1.0)
        world.turn(1.0)
        world.turn(1.0)
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_WATCHING)     # not "waiting for a conversation"
    assert caplog.text.count("a look at the microphone failed") == 1           # ...and not once a second
    world.service.broken = False
    assert world.turn(1.0).state == WAITING


def test_what_is_edited_by_hand_while_the_program_runs_waits_for_the_next_start():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.service.call = FakeCall(7, "Zoom")
    edited = json.loads(paths.config_file().read_text(encoding="utf-8"))
    edited["recording"]["linger_seconds"] = 90
    paths.config_file().write_text(json.dumps(edited), encoding="utf-8")     # Notepad, in the middle of a conversation
    world.app.set_microphone(MIC)                            # ...and then the same microphone, from the menu
    world.turn()
    assert "close" not in world.log                          # the conversation goes on
    assert world.service.cfg.linger_seconds == 5.0           # under the settings the program started with
    assert saved()["linger_seconds"] == 90                   # and the file keeps what was written into it
    world.app.set_microphone("Webcam")                       # a real change from the menu takes that one thing
    world.turn()
    assert world.service.cfg.microphone == "Webcam" and world.service.cfg.linger_seconds == 5.0
    assert saved() | {"microphone": "Webcam", "linger_seconds": 90} == saved()


def test_settings_mended_by_hand_are_taken_whole_at_the_next_click_in_the_menu():
    # The program started with a file it could not read: there are no settings of its own to keep.
    paths.config_file().write_text('{"recording": {', encoding="utf-8")
    world = World()
    assert world.turn().state == ERROR
    paths.config_file().write_text(json.dumps({"recording": {"microphone": MIC, "linger_seconds": 90}}),
                                   encoding="utf-8")
    world.app.set_enabled(True)
    glance = world.turn()
    assert (glance.state, glance.microphone) == (WAITING, MIC)
    assert world.service.cfg.linger_seconds == 90


def test_leaving_ends_the_conversation_and_lets_go_of_the_database():
    world = World(enabled=True, microphone=MIC)
    world.turn()
    world.app.close()
    assert world.log[-2:] == ["close", "store closed"]
    world.app.close()                                        # twice is not an error


def test_the_thread_of_its_own_runs_until_stopped_and_prepares_itself_for_windows():
    order = []
    paths.config_file().write_text(json.dumps({"recording": {"enabled": True, "microphone": MIC,
                                                              "poll_seconds": 0.2}}), encoding="utf-8")
    log: list[str] = []

    def make(cfg, store, holders):
        return FakeService(cfg, store, holders, log)

    app = App(open_store=lambda: FakeStore(log), make_service=make, holders=lambda microphone: [],
              microphones=lambda: [MIC], begin=lambda: order.append("begin"), end=lambda: order.append("end"))
    app.start()
    deadline = time.monotonic() + 5
    while app.glance().state != WAITING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.glance().state == WAITING
    app.set_enabled(False)                                   # a command wakes the thread at once
    deadline = time.monotonic() + 5
    while app.glance().state != OFF and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.glance().state == OFF
    app.stop()
    assert order == ["begin", "end"] and log[-1] == "store closed"


def test_a_service_that_fails_altogether_says_so_on_the_icon(caplog):
    paths.config_file().write_text(json.dumps({"recording": {"enabled": True, "microphone": MIC}}), encoding="utf-8")
    log: list[str] = []
    shown: list[Glance] = []

    def make(cfg, store, holders):
        raise RuntimeError("nothing of the kind was expected")

    app = App(open_store=lambda: FakeStore(log), make_service=make, holders=lambda microphone: [],
              microphones=lambda: [MIC], end=lambda: log.append("end"), on_change=lambda: shown.append(app.glance()))
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        app.start()
        app._thread.join(5)
    assert not app._thread.is_alive()
    glance = app.glance()
    assert (glance.state, glance.problem) == (ERROR, core.PROBLEM_FAILED)    # not "waiting for a conversation"
    assert (glance.enabled, glance.microphone) == (True, MIC) and shown[-1] == glance
    assert log[-2:] == ["store closed", "end"]
    assert "the service behind the icon failed" in caplog.text
    app.stop()                                               # after that, leaving is still not an error


def test_a_thread_that_cannot_prepare_itself_says_so_too(caplog):
    def no_com():
        raise OSError("CoInitialize failed")

    ended = []
    app = App(open_store=lambda: None, make_service=lambda *a: None, holders=lambda microphone: [],
              microphones=lambda: [], begin=no_com, end=lambda: ended.append(1))
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        app.start()
        app._thread.join(5)
    assert app.glance().problem == core.PROBLEM_FAILED
    assert ended == []                                       # what was never prepared is not released


# --- the archive is also where conversations wait to be recognised


def test_recognition_starts_with_the_archive_and_is_given_the_settings():
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    assert world.log == ["store opened", "recover 5", "recognition started", f"service for {MIC}"]
    assert world.recognition.store is world.store and world.recognition.cfg.storage.keep_audio_days == 30
    world.turn(1.0)
    assert world.log.count("recognition started") == 1               # once, not at every look


def test_an_archive_that_is_there_is_recognised_even_with_recording_switched_off():
    world = World(recognition=True)
    world.turn()
    assert world.log == []                                           # a new installation: nothing to open
    world.archive = True
    glance = world.turn(1.0)
    assert world.log == ["store opened", "recover 5", "recognition started"] and glance.state == OFF


def test_under_settings_that_cannot_be_read_the_archive_is_not_touched():
    # How long the sound is kept is one of the settings: defaults must not delete what the user meant to keep.
    paths.config_file().write_text('{"storage": {"keep_audio_days": -1},', encoding="utf-8")
    world = World(recognition=True)
    world.archive = True
    assert world.turn().problem == core.PROBLEM_SETTINGS
    assert world.log == [] and world.recognition is None


def test_recognition_is_told_when_a_conversation_is_being_recorded():
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    assert world.recognition.busy() is False
    world.service.call = FakeCall(7, "Zoom")
    assert world.recognition.busy() is True
    world.app.set_enabled(False)                                     # no service at all: nothing is being recorded
    world.turn()
    assert world.recognition.busy() is False


def test_the_icon_is_told_what_waits_what_is_at_work_and_what_failed():
    world = World(recognition=True, enabled=True, microphone=MIC)
    glance = world.turn()
    assert (glance.recognising, glance.waiting, glance.failed, glance.gateway) == (False, 0, 0, "")
    world.store.states = {"recorded": 2, "transcribing": 1, "failed": 3, "done": 9}
    world.recognition.status = transcriber.WORKING
    glance = world.turn(1.0)
    assert (glance.recognising, glance.waiting, glance.failed, glance.gateway) == (True, 2, 3, "")
    assert world.changes == 2
    world.recognition.status = gateway.DOWN                          # the one that was at work waits again
    glance = world.turn(1.0)
    assert (glance.recognising, glance.waiting, glance.gateway) == (False, 3, gateway.DOWN)
    for status in (gateway.OLD, gateway.LOADING):
        world.recognition.status = status
        assert world.turn(1.0).gateway == status
    world.recognition.status = transcriber.IDLE
    assert world.turn(1.0).gateway == ""


def test_failed_conversations_are_tried_anew_from_the_menu():
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    world.store.states = {"failed": 2}
    assert world.turn(1.0).failed == 2
    world.app.retry_failed()
    assert world.turn(1.0).failed == 0 and "retry" in world.log
    World().app.retry_failed()                                       # with no archive open it is not an error


def test_an_archive_that_cannot_be_asked_is_logged_and_the_last_answer_stands(caplog):
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    world.store.states = {"recorded": 4}
    assert world.turn(1.0).waiting == 4
    world.store.broken = sqlite3.OperationalError("database is locked")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        glance = world.turn(1.0)
    assert glance.waiting == 4 and glance.state == WAITING
    assert "could not be asked what waits" in caplog.text


def test_leaving_stops_recognition_before_the_archive_is_closed():
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    world.app.close()
    assert world.log[-3:] == ["close", "recognition stopped", "store closed"]


def test_with_the_archive_closed_nothing_is_said_about_recognition():
    world = World(recognition=True, enabled=True, microphone=MIC)
    world.turn()
    world.store.states = {"recorded": 4}
    world.turn(1.0)
    world.app.close()
    world.archive, world.database_error = False, None
    world.app.set_enabled(False)
    assert world.turn(1.0).waiting == 0


def test_the_panel_reaches_the_archive_the_summariser_and_the_settings_through_the_service():
    world = World(recognition=True, summaries=True, enabled=True, microphone=MIC)
    assert world.app.archive() is None and world.app.summariser() is None       # before the first look
    assert world.app.settings().recording.microphone == MIC
    world.turn()
    assert world.app.archive() is world.store and world.app.summariser() is world.summariser
    assert world.summariser.store is world.store and world.summariser.cfg.summary.model == "opus"
    world.app.close()
    assert world.app.archive() is None and world.app.summariser() is None
    assert world.summariser.halted == 1                                  # what waited for a summary is not begun any more
