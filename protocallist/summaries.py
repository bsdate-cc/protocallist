"""The summaries of the conversations of an archive.

A summary is made on request - by Claude or by the user's own command, see summary.py - from
the text the archive holds, and is kept in the archive. An archive may also hold a summary of
the user's own, kept apart, which no summary made anew ever touches: nothing here makes one,
but one that is there is the one in force. The one in force is copied into a file beside the
sound of the conversation; the archive is the source, the file a copy.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from protocallist import i18n, paths, summary, topic, transcript
from protocallist.config import SummaryConfig
from protocallist.i18n import Text
from protocallist.store import DONE, MADE, Call, Store
from protocallist.summary import SummaryError

log = logging.getLogger(__name__)

_writing = threading.Lock()     # one file beside a sound is written at a time


def settle(store: Store, call: Call) -> Path:
    """After the summary in force may have changed: what the conversation is about, and the file
    beside its sound. The file is written whole or not at all, and only when it would differ."""
    topic.refresh(store, call.id)
    path = store.summary_file(call)
    with _writing:
        # Looked at only now, in its turn: whoever writes the file last writes what is in force by then.
        current = store.summary(call.id)
        if current is None:         # the conversation went meanwhile, and its files with it
            return path
        try:
            same = path.read_bytes() == current.text.encode("utf-8")      # byte for byte: line ends are part of it
        except OSError:
            same = False
        if not same:
            part = path.with_name(path.name + ".part")
            part.write_text(current.text, encoding="utf-8", newline="\n")
            os.replace(part, path)
            if store.get(call.id) is None:      # forgotten at this very moment: its file does not outlive it
                path.unlink(missing_ok=True)
    return path


class Refused(SummaryError):
    """Whoever was asked for the summary gave none: the next conversation would fare no better."""


@dataclass(frozen=True)
class Stopped:
    """The line that stood still when a summary failed."""
    reason: Text
    failed: int                     # the conversation it failed at - 0 if that one is gone since
    waiting: tuple[int, ...]        # those after it, which were not sent anywhere


@dataclass(frozen=True)
class Line:
    """How the summaries of an archive stand at one moment."""
    making: tuple[int, ...]         # the conversations whose summary is being made right now
    queued: tuple[int, ...]         # those that wait their turn, in its order
    stopped: Stopped | None
    failures: dict[int, Text]       # why the last attempt at a conversation came to nothing


class Summariser:
    """`ask` is summary.ask or what stands in for it; `settings` say which model or which command,
    and whose instructions.

    Conversations sent to be summarised stand in a line and are taken one at a time, by one
    thread: the line never has more than one request under way. The first summary that fails
    stops it - whatever made that one fail would fail the rest as well - and the line stands
    still until it is started again or forgotten. All of this lives in the program: after it
    is started again nothing waits."""

    def __init__(self, store: Store, settings: SummaryConfig, *, language: Callable[[], str] = i18n.language,
                 ask: Callable = summary.ask, today: Callable[[], datetime] = datetime.now,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._store, self._settings = store, settings
        self._language, self._ask, self._today, self._clock = language, ask, today, clock
        self._lock = threading.Lock()
        self._making: set[int] = set()
        self._line: list[tuple[int, bool]] = []     # waiting their turn: (the conversation, anew)
        self._going = False                         # the thread of the line is at work
        self._reason: Text | None = None            # why the line stands still, when it does...
        self._stood: list[tuple[int, bool]] = []    # ...what stood in it, the one it failed at first...
        self._failed_at = 0                         # ...and which one that is
        self._failed: dict[int, Text] = {}          # why the last attempt at a conversation came to nothing

    def line(self) -> Line:
        """How the summaries stand, all of it at one moment."""
        with self._lock:
            stopped = None
            if self._reason is not None:
                waiting = tuple(call_id for call_id, _ in self._stood if call_id != self._failed_at)
                stopped = Stopped(self._reason, self._failed_at, waiting)
            return Line(tuple(sorted(self._making)), tuple(call_id for call_id, _ in self._line), stopped, dict(self._failed))

    def target(self) -> tuple[str, str]:
        """Where the text of a conversation goes when its summary is asked for: ("command", its
        words) or ("claude", the model)."""
        command = self._settings.command
        return ("command", summary.name(command)) if command else ("claude", self._settings.model)

    def make(self, call_id: int, anew: bool = False) -> Path:
        """The file of the summary in force of a conversation. It is made first if the conversation
        has none - or `anew`, in place of the one that was made before. This is where the text of a
        conversation leaves the computer. SummaryError says why there is none."""
        self._begin(call_id)
        try:
            return self._work(call_id, anew)
        finally:
            self._end(call_id)

    # ---- the line

    def send(self, ids: Sequence[int], *, first: bool = False, anew: bool = False, old: str = "") -> list[int] | None:
        """Put conversations into the line: at its end, or `first` - next after the one being made.
        Gives those that were taken: one that is being made is not, nor one that waits already -
        unless it is asked for `first`, which moves it there. While the line stands still, `old`
        must say what becomes of it - "keep": it is started again, and these join it; "drop": it
        is forgotten, and these go alone. Without that nothing is done and None is given."""
        with self._lock:
            if self._reason is not None:
                if old not in ("keep", "drop"):
                    return None
                if old == "keep":
                    self._again()
                self._reason, self._stood, self._failed_at = None, [], 0
            waits = dict(self._line)
            taken = [call_id for call_id in dict.fromkeys(ids)
                     if call_id not in self._making and (first or call_id not in waits)]
            if first:
                self._line = [(call_id, anew or waits.get(call_id, False)) for call_id in taken] + \
                    [waiting for waiting in self._line if waiting[0] not in taken]
            else:
                self._line += [(call_id, anew) for call_id in taken]
            for call_id in taken:
                self._failed.pop(call_id, None)
            self._go()
            return taken

    def restart(self) -> bool:
        """Start the line that stands still again, from the conversation it failed at. False if none stands."""
        with self._lock:
            if self._reason is None:
                return False
            self._again()
            self._reason, self._stood, self._failed_at = None, [], 0
            self._go()
            return True

    def reset(self) -> bool:
        """Forget the line that stands still. False if none stands."""
        with self._lock:
            stood = self._reason is not None
            self._reason, self._stood, self._failed_at = None, [], 0
            return stood

    def halt(self) -> int:
        """Empty the line: the summary being made is finished, those that waited are not begun.
        How many waited."""
        with self._lock:
            waited, self._line = len(self._line), []
            return waited

    def drop(self, call_id: int) -> None:
        """A conversation is deleted: it waits for nothing any more."""
        with self._lock:
            self._line = [waiting for waiting in self._line if waiting[0] != call_id]
            self._stood = [waiting for waiting in self._stood if waiting[0] != call_id]
            self._failed.pop(call_id, None)
            if self._failed_at == call_id:
                self._failed_at = 0
            if not self._stood:
                self._reason = None

    def _again(self) -> None:
        """Under the lock: what stood still is in the line again, ahead of what is there."""
        for call_id, _ in self._stood:
            self._failed.pop(call_id, None)
        self._line = self._stood + self._line

    def _go(self) -> None:
        """Under the lock: see that the line is being worked through."""
        if self._line and not self._going:
            threading.Thread(target=self._run, name="summaries", daemon=True).start()
            self._going = True          # only once it has started: a thread that could not be is no thread at work

    def _run(self) -> None:
        """The thread of the line: one summary after another, until none waits or one fails."""
        done: tuple[int, bool] | None = None
        why: Text | None = None
        there = True
        while True:
            with self._lock:        # what came of the last one and which one is next: one moment for whoever looks
                if done is not None:
                    self._making.discard(done[0])
                    if why is not None:
                        log.warning("the line of summaries stands still: %d more were not sent", len(self._line))
                        if there:
                            self._failed[done[0]] = why
                        self._stood = [*([done] if there else []), *self._line]     # one that is gone stands in no line
                        self._failed_at = done[0] if there else 0
                        self._reason = why if self._stood else None
                        self._line = []
                while self._line and self._line[0][0] in self._making:      # being made directly: not a second time
                    self._line.pop(0)
                if not self._line:
                    self._going = False
                    return
                done = self._line.pop(0)
                self._making.add(done[0])
            why, there = self._attempt(*done)

    def _attempt(self, call_id: int, anew: bool) -> tuple[Text | None, bool]:
        """Make one summary of the line: (why the line stops here - None if it goes on, whether
        the conversation is still there). Whatever happens, the thread of the line lives on."""
        try:
            return self._tried(call_id, anew)
        except Exception:
            log.exception("summary of call %d: making it failed in a way nobody foresaw", call_id)
            return Text("выжимка не получилась — см. журнал"), True

    def _tried(self, call_id: int, anew: bool) -> tuple[Text | None, bool]:
        try:
            self._work(call_id, anew)
            return None, True
        except SummaryError as e:
            failed = e
        why = failed.args[0] if failed.args and isinstance(failed.args[0], Text) else Text(str(failed))
        try:
            there = self._store.get(call_id) is not None
        except sqlite3.Error:       # the archive is closed: the reason there is says so already
            there = True
        if not there and not isinstance(failed, Refused):
            return None, False      # deleted meanwhile, or never there - and nothing wrong with whoever makes the summaries
        log.warning("summary of call %d was not made: %s", call_id, i18n.english(why))
        return why, there

    # ---- one summary

    def _begin(self, call_id: int) -> None:
        with self._lock:
            if call_id in self._making:
                raise SummaryError(Text("выжимка этого разговора уже делается"))
            self._making.add(call_id)

    def _end(self, call_id: int) -> None:
        with self._lock:
            self._making.discard(call_id)

    def _instructions(self) -> Path | None:
        """The file of the user's own instructions, if the settings name one - beside the settings
        unless they say where."""
        named = self._settings.prompt.strip()
        if not named:
            return None
        path = Path(named).expanduser()
        return path if path.is_absolute() else paths.home() / path

    def _work(self, call_id: int, anew: bool) -> Path:
        # The moment of the text a summary is made of - taken before the text is read: the text may be
        # written anew at any time after, and then this summary is one of the text before.
        moment = self._today()
        try:
            call = self._store.get(call_id)
            phrases = self._store.phrases(call_id) if call is not None and call.state == DONE else []
            has = self._store.summary(call_id) is not None
        except sqlite3.Error as e:      # the archive is closed: the program is leaving
            raise SummaryError(Text("архив не открыт")) from e
        if not phrases:
            raise SummaryError(Text("у разговора нет текста"))
        if anew or not has:
            began = self._clock()
            try:
                made, maker = self._ask(transcript.render(call, phrases), self._settings.model, self._language(),
                                        command=list(self._settings.command), instructions=self._instructions(),
                                        where=self._store.root, label=call.folder)
            except SummaryError as e:
                raise Refused(*e.args) from e
            self._keep(call, MADE, made, maker, round(self._clock() - began, 1), moment)
        return self._settled(call)

    def _keep(self, call: Call, kind: str, text: str, maker: str | None, seconds: float | None, moment: datetime) -> None:
        try:
            self._store.save_summary(call.id, kind, text, moment, maker, seconds)
        except sqlite3.Error as e:      # the conversation was deleted meanwhile, or the archive is closed
            raise SummaryError(Text("выжимка не сохранена: {reason}", reason=type(e).__name__)) from e

    def _settled(self, call: Call) -> Path:
        try:
            return settle(self._store, call)
        except (OSError, sqlite3.Error) as e:
            log.warning("summary of %s: it is in the archive, but the file beside the sound was not written (%s)",
                        call.folder, type(e).__name__)
            raise SummaryError(Text("выжимка сохранена, но файл рядом со звуком не записан: {reason}",
                                    reason=type(e).__name__)) from e
