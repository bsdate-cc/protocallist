"""What stands behind the tray icon: the settings, the archive and the recording, all kept by
one thread. The icon looks at it through one glance and tells it things through a few commands.

One thread on purpose: everything that asks Windows about microphones must come from the
same thread, and so must the recorder's streams; and the menu, which lives on another
thread, must never wait for a sound device.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from protocallist import config, gateway, transcriber
from protocallist.i18n import N_
from protocallist.service import mend
from protocallist.store import FAILED, RECORDED, TRANSCRIBING

log = logging.getLogger(__name__)

OFF, NO_MICROPHONE, MISSING, WAITING, RECORDING, ERROR = (
    "off", "no microphone", "missing", "waiting", "recording", "error")

PROBLEM_SETTINGS = N_("настройки не читаются")
PROBLEM_DATABASE = N_("база не открывается")
PROBLEM_RECORDING = N_("запись остановилась, см. журнал")
PROBLEM_REFUSED = N_("разговор не записан, см. журнал")
PROBLEM_WATCHING = N_("наблюдение не работает, см. журнал")
PROBLEM_FAILED = N_("сбой программы, перезапустите её")

MICROPHONES_EVERY_S = 5.0   # how often the list of microphones for the menu is read anew
RETRY_S = 30.0              # how soon a database that would not open is tried again


@dataclass(frozen=True)
class Glance:
    """All the icon needs to show. Whole minutes, so that a glance changes when what is
    shown changes, and not every second."""
    state: str = OFF
    program: str = ""           # RECORDING: whose conversation
    minutes: int = 0            # RECORDING: for how long
    problem: str = ""           # ERROR: what is wrong, a text for the tooltip
    enabled: bool = False
    microphone: str = ""
    microphones: tuple[str, ...] = ()
    recognising: bool = False   # a conversation is being turned into text right now
    waiting: int = 0            # so many wait for that
    failed: int = 0             # so many could not be recognised
    gateway: str = ""           # why they wait, if it is the gateway: gateway.DOWN, LOADING or OLD


NOTHING = dict(recognising=False, waiting=0, failed=0, gateway="")


class App:
    """`open_store()` gives the archive or raises; `make_service(cfg, store, holders)` gives
    the service that records with these settings; `holders(microphone)` and `microphones()`
    ask Windows; `begin()` and `end()` prepare and release the thread for that.
    `make_transcriber(cfg, store, busy)` gives what recognises the conversations of the
    archive - it has a thread of its own, started and stopped from here; `has_archive()`
    says whether there is an archive already, with conversations that may wait.
    `make_summariser(cfg, store)` gives what makes the summaries of the archive: the panel asks it."""

    def __init__(self, *, open_store: Callable, make_service: Callable,
                 holders: Callable[[str], list[str] | None], microphones: Callable[[], list[str]],
                 begin: Callable[[], None] = lambda: None, end: Callable[[], None] = lambda: None,
                 on_change: Callable[[], None] = lambda: None,
                 make_transcriber: Callable | None = None, has_archive: Callable[[], bool] = lambda: False,
                 make_summariser: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._open_store, self._make_service = open_store, make_service
        self._holders, self._microphones = holders, microphones
        self._begin, self._end, self._on_change, self._clock = begin, end, on_change, clock
        self._make_transcriber, self._has_archive = make_transcriber, has_archive
        self._make_summariser = make_summariser
        self._transcriber = None
        self._summariser = None
        self._queue: dict = dict(NOTHING)       # what the glance says about recognition
        self._commands: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="protocallist", daemon=True)
        self._cfg = config.Config()
        self._unreadable = False        # config.json is there and cannot be read: nothing is recorded
        self._store = None
        self._store_due = 0.0           # when a database that would not open is tried again
        self._no_database = False
        self._service = None
        self._present = True            # the chosen microphone is there, as far as the last look says
        self._broken = False            # the last look at the microphone failed: nothing is being watched
        self._since: tuple[int, float] | None = None    # the conversation being recorded, and when it was first seen
        self._names: tuple[str, ...] = ()
        self._names_due = 0.0
        self._glance = Glance()

    # ---- for the icon, from its own threads

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """A conversation that is going on ends here and is kept."""
        self._stopping = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join()

    def glance(self) -> Glance:
        return self._glance

    def set_enabled(self, on: bool) -> None:
        self._tell(("enabled", on))

    def set_microphone(self, name: str) -> None:
        self._tell(("microphone", name))

    def discard_current(self) -> None:
        """Stop the recording that is going on and throw it away."""
        self._tell(("discard", None))

    def archive(self):
        """The archive, or None while there is none open. It bears being asked from any thread."""
        return self._store

    def summariser(self):
        """What makes the summaries of the archive, or None while there is no archive open."""
        return self._summariser

    def settings(self) -> config.Config:
        """The settings the program runs on."""
        return self._cfg

    def retry_failed(self) -> None:
        """Try once more to recognise the conversations that could not be."""
        self._tell(("retry", None))

    def _tell(self, command: tuple) -> None:
        self._commands.put(command)
        self._wake.set()

    # ---- the one thread

    def _run(self) -> None:
        try:
            self._begin()
            try:
                self.load()
                while not self._stopping:
                    self.step()
                    self._wake.wait(self._cfg.recording.poll_seconds)
                    self._wake.clear()
            finally:
                self.close()
                self._end()
        except Exception:
            # nothing is watched from here on, and the icon must not go on saying "waiting"
            log.exception("the service behind the icon failed")
            self._show(Glance(ERROR, problem=PROBLEM_FAILED, **self._known()))

    def load(self) -> None:
        """Read the settings as the program starts. What is changed in the file by hand
        later is taken at the next start."""
        loaded = config.load()
        for problem in loaded.problems:
            log.warning("config: %s: %s", problem.where, problem.message)
        self._unreadable = config.unparsed(loaded)
        self._cfg = loaded.config

    def step(self) -> None:
        """One turn: do what the menu asked, have the service there exactly when the
        settings want a recording, look at the microphone, and say what there is to show."""
        while True:
            try:
                what, value = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                self._do(what, value)
            except Exception:
                log.exception("what the menu asked for could not be done")
        now = self._clock()
        if now >= self._names_due:
            self._names_due = now + MICROPHONES_EVERY_S
            try:
                self._names = tuple(self._microphones())
            except Exception:
                log.exception("the microphones could not be listed")
        self._archive(now)
        self._match(now)
        if self._service is not None:
            try:
                self._service.tick()
            except Exception:
                if not self._broken:    # said once, not every second
                    log.exception("a look at the microphone failed")
                self._broken = True
            else:
                if self._broken:
                    log.info("the microphone is being looked at again")
                self._broken = False
        self._count()
        self._show(self._look(now))

    def _show(self, glance: Glance) -> None:
        if glance == self._glance:
            return
        self._glance = glance
        try:
            self._on_change()
        except Exception:
            log.exception("the icon could not be told of a change")

    def close(self) -> None:
        self._drop()
        if self._summariser is not None:
            self._summariser.halt()     # no new summary is begun; one that is being made finds the archive closed
            self._summariser = None
        if self._transcriber is not None:
            self._transcriber.stop()    # before the archive goes: it writes there
            self._transcriber = None
        if self._store is not None:
            self._store.close()
            self._store = None

    def _do(self, what: str, value) -> None:
        if what == "discard":
            if self._service is not None:
                self._service.discard_current()
            return
        if what == "retry":
            if self._store is not None:
                log.info("recognition is tried anew for %d conversations", self._store.retry_failed())
            return
        loaded = config.load()
        if config.unparsed(loaded):
            # written back, the user's file - whatever is wrong with it - would be lost to defaults
            log.warning("config.json cannot be read: the change from the menu is not saved")
            return
        # The file gets this one change on top of whatever is in it now. The program takes this
        # one change and nothing else: what was edited by hand since it started waits for the
        # next start - it must not end a conversation at a click that asked for something else.
        # Only a program that started without settings it could read takes the whole file.
        config.save(replace(loaded.config, recording=replace(loaded.config.recording, **{what: value})))
        running = loaded.config if self._unreadable else self._cfg
        recording = replace(running.recording, **{what: value})
        if recording != self._cfg.recording:
            self._drop()        # a conversation that is going on ends here and is kept
        self._cfg, self._unreadable = replace(running, recording=recording), False

    def _drop(self) -> None:
        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                log.exception("the recording could not be ended cleanly")
            self._service = None
        self._present, self._since, self._broken = True, None, False

    def _wanted(self) -> bool:
        recording = self._cfg.recording
        return bool(recording.enabled and recording.microphone)

    def _archive(self, now: float) -> None:
        """Have the archive open when there is something to do with it: conversations to
        record - or, in an archive that is there already, to recognise. Never under settings
        that could not be read: how long the sound is kept is one of them."""
        if self._store is not None or self._unreadable or now < self._store_due:
            return
        if not (self._wanted() or self._has_archive()):
            return
        try:
            self._store = self._open_store()
            self._store.recover(mend, self._cfg.recording.min_seconds)
            self._no_database = False
        except (sqlite3.Error, OSError) as e:
            if not self._no_database:
                log.error("the database cannot be opened (%s: %s): nothing is recorded", type(e).__name__, e)
            self._no_database, self._store_due = True, now + RETRY_S
            if self._store is not None:     # opened, but what a dead process left could not be put in order
                self._store.close()
                self._store = None
            return
        if self._make_summariser is not None:
            self._summariser = self._make_summariser(self._cfg, self._store)
        if self._make_transcriber is not None:
            self._transcriber = self._make_transcriber(self._cfg, self._store, self._recording_now)
            self._transcriber.start()

    def _recording_now(self) -> bool:
        """Asked by recognition, from its thread: it does not work while a conversation is being recorded."""
        service = self._service
        return service is not None and service.call is not None

    def _match(self, now: float) -> None:
        if not self._wanted():
            self._drop()
            return
        if self._service is not None or self._store is None:
            return
        self._service = self._make_service(self._cfg.recording, self._store, self._ask)
        log.info("watching the microphone %s", self._cfg.recording.microphone)

    def _count(self) -> None:
        """What waits to be recognised and what could not be - for the tooltip, the menu and the panel."""
        if self._store is None:
            self._queue = dict(NOTHING)
            return
        try:
            counts = self._store.counts()
        except (sqlite3.Error, OSError):
            log.exception("the archive could not be asked what waits in it")
            return
        status = self._transcriber.status if self._transcriber is not None else transcriber.IDLE
        working = status == transcriber.WORKING
        self._queue.update(
            recognising=working, failed=counts.get(FAILED, 0),
            waiting=counts.get(RECORDED, 0) + (0 if working else counts.get(TRANSCRIBING, 0)),
            gateway=status if status in (gateway.DOWN, gateway.LOADING, gateway.OLD) else "")

    def _ask(self, microphone: str) -> list[str] | None:
        found = self._holders(microphone)
        self._present = found is not None
        return found

    def _known(self) -> dict:
        """What the menu shows whatever the state is."""
        recording = self._cfg.recording
        return dict(enabled=recording.enabled, microphone=recording.microphone, microphones=self._names, **self._queue)

    def _look(self, now: float) -> Glance:
        recording, known = self._cfg.recording, self._known()
        if self._unreadable:
            return Glance(ERROR, problem=PROBLEM_SETTINGS, **known)
        if not recording.enabled:
            return Glance(OFF, **known)
        if not recording.microphone:
            return Glance(NO_MICROPHONE, **known)
        if self._service is None:
            return Glance(ERROR, problem=PROBLEM_DATABASE, **known)
        if self._broken:
            return Glance(ERROR, problem=PROBLEM_WATCHING, **known)
        call = self._service.call
        if call is None:
            if self._service.failed:
                return Glance(ERROR, problem=PROBLEM_RECORDING, **known)
            if self._service.refused:
                return Glance(ERROR, problem=PROBLEM_REFUSED, **known)
            return Glance(WAITING if self._present else MISSING, **known)
        if self._since is None or self._since[0] != call.id:
            self._since = (call.id, now)
        return Glance(RECORDING, program=call.program, minutes=int((now - self._since[1]) // 60), **known)
