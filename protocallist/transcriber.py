"""Recognition: conversations that wait, the earliest first, one at a time.

Each side of a conversation is cut into pieces of at most ten minutes; every piece goes to
the gateway, and the phrases that come back are put on the one clock of the conversation.
They are kept in the archive - the panel shows them, and gives them as a file when asked.
Then the two recordings become one small file, which lies beside the working folder under
the name of the conversation; the folder then goes.

Nothing here is in a hurry. No piece is asked for while a conversation is being recorded;
a gateway that is not there is waited for; and whatever goes wrong here never touches a
recording that is going on. What was said is never logged.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from protocallist import audio, gateway, opus, summaries, topic
from protocallist.config import Config
from protocallist.store import DONE, FAILED, MADE, RECORDED, Call, Phrase, Store

log = logging.getLogger(__name__)

PIECE_S = 600               # no piece is longer: about 19 MB, far below what the gateway takes
SEARCH_S = 30               # the cut is looked for in the last seconds of a piece...
WINDOW_S, HOP_S = 0.5, 0.1  # ...at its quietest half second
RETRY_S = 60.0              # how soon a gateway that was not there is asked again
SWEEP_S = 24 * 3600.0       # how often sound that outlived its time is looked for
TRACKS = (("mic.wav", "me"), ("line.wav", "them"))
# Earlier versions of the program wrote the text of a conversation for reading into a file: beside the
# sound under this ending, and before that inside the folder of the conversation under this name.
OLD_TEXT, OLDER_TEXT = ".transcript.md", "transcript.md"
IDLE, WORKING = "idle", "working"       # what it is doing; or gateway.DOWN / LOADING / OLD - what it waits for
# Why a conversation could not be recognised anew, where the reason is one the page puts into words of its own.
NO_MODEL, NO_SOUND, NO_CHOICE = "the model is not installed", "the sound is not there", "the gateway has no choice of models"
NO_LOAD, NO_WORDS = "the gateway could not load the model", "the model heard no words"
NO_ANSWER, BAD_SOUND = "the gateway did not answer in time", "the sound cannot be read"


def cuts(total: int, read: Callable[[int, int], np.ndarray]) -> Iterator[tuple[int, int]]:
    """(first frame, number of frames) of every piece of a recording of `total` frames, which
    `read(first, count)` gives a part of. A piece ends at the quietest half second of its last
    half minute - between two phrases, as a rule - and the next begins there: nothing is lost
    and nothing is said twice."""
    start = 0
    piece, search = int(PIECE_S * audio.RATE), int(SEARCH_S * audio.RATE)
    window, hop = int(WINDOW_S * audio.RATE), int(HOP_S * audio.RATE)
    while total - start > piece:
        first = start + piece - search
        tail = read(first, search).astype(np.float64)
        energy = np.concatenate(([0.0], np.cumsum(tail * tail)))
        starts = np.arange(0, len(tail) - window + 1, hop)
        quietest = int(starts[np.argmin(energy[starts + window] - energy[starts])])
        end = first + quietest + window // 2
        yield start, end - start
        start = end
    if total > start:
        yield start, total - start


class _GaveWay(Exception):
    """A conversation began, or the program is leaving: recognition steps aside."""


class Transcriber:
    """`connect()` gives a gateway that was found and checked, or raises gateway.Unavailable;
    `busy()` says that a conversation is being recorded now; `compress(mic, line, out)`
    makes the small file or raises."""

    def __init__(self, store: Store, cfg: Config, *, connect: Callable[[], gateway.Gateway] | None = None,
                 busy: Callable[[], bool] = lambda: False,
                 compress: Callable[[Path, Path, Path], None] = opus.compress,
                 clock: Callable[[], float] = time.monotonic, today: Callable[[], datetime] = datetime.now) -> None:
        self._store, self._cfg = store, cfg
        self._connect = connect or (lambda: gateway.connect(cfg.gateway))
        self._busy, self._compress, self._clock, self._today = busy, compress, clock, today
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recognition", daemon=True)
        self._retry_at = 0.0
        self._sweep_at = 0.0
        self._before: deque[Call] | None = None     # recognised before this start: their files are looked at once
        self._since = today()       # a half file older than this start is nobody's work in progress
        self._complained = ""       # the trouble with the gateway that was logged last: said once, not every minute
        # The pieces read before recognition had to step aside, not asked for twice - ((the conversation,
        # the model), its pieces). Those of a conversation that is read anew (True) are kept apart from
        # those of one that waits for its first text (False): a new conversation in between does not undo them.
        self._kept: dict[bool, tuple[object, dict]] = {False: (0, {}), True: (0, {})}
        self.status = IDLE

    # ---- the thread of its own

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Leave after the piece that is being recognised now - but do not wait for it long:
        a conversation left half-recognised goes back to wait at the next start."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2.0)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.step()
            except Exception:
                if not self._stop.is_set():     # at the very end the archive may be closed under us
                    log.exception("a turn of recognition failed")

    # ---- one turn

    def step(self) -> None:
        """Delete sound that outlived its time, when that is due; finish the files a start before
        this one left half done; then recognise the earliest conversation that waits - unless one
        is being recorded, or the gateway was just found missing."""
        now = self._clock()
        if now >= self._sweep_at:
            self._sweep_at = now + SWEEP_S
            self._sweep()
        if self._busy() or self._finish() or now < self._retry_at:
            return
        call = self._store.take()
        if call is None:
            self._anew()                # nothing waits for its first text: one may wait to be recognised anew
            return
        try:
            gate = self._connect()      # until it is found the conversation waits, and the icon says that it waits
            self.status = WORKING
            phrases = self._recognise(call, gate)
            self._store.done(call.id, phrases, gate.model, self._today())
        except gateway.Unavailable as e:
            self._store.put_back(call.id)
            self._waits(e)
            return
        except _GaveWay:
            self._store.put_back(call.id)
            self.status = IDLE
            return
        except (gateway.Refused, OSError) as e:
            after = self._store.missed(call.id, str(e))
            log.warning("call %d: attempt %d to recognise it failed (%s)%s", call.id, after.attempts, e,
                        " - it is failed for good" if after.state == FAILED else "")
            self.status = IDLE
            return
        except Exception as e:      # what nobody foresaw is an attempt too: no conversation is left half recognised
            after = self._store.missed(call.id, type(e).__name__)
            log.exception("call %d: attempt %d to recognise it failed in a way nobody foresaw%s", call.id, after.attempts,
                          " - it is failed for good" if after.state == FAILED else "")
            self.status = IDLE
            return
        self._complained = ""
        self._kept[False] = (0, {})
        log.info("call %d (%s, %.0f s) is recognised: %d phrases", call.id, call.program, call.duration_s or 0.0, len(phrases))
        topic.refresh(self._store, call.id)
        self._sound(call)
        self.status = IDLE

    def _waits(self, e: gateway.Unavailable) -> None:
        """There is nobody to ask: the gateway is asked again in a minute, and the trouble is said once."""
        self._retry_at = self._clock() + RETRY_S
        if self._complained != e.kind:
            self._complained = e.kind
            log.warning("recognition waits: the gateway is %s (%s)", e.kind, e)
        self.status = e.kind

    def _anew(self) -> None:
        """Recognise anew the earliest conversation that was asked for, with the model that was
        asked for. It has a text and keeps it until the new one is whole; its sound is read from
        the small file. One attempt: there is a text already, and asking again is a click."""
        call = self._store.take_redo()
        if call is None:
            self.status = IDLE
            return
        model, file = call.redo_model, self._store.sound(call)
        said = ""       # what the gateway or the library said of a failure the page has words of its own for: for the log
        try:
            if not file.is_file():
                raise FileNotFoundError(NO_SOUND)
            gate = self._connect()
            # A gateway reads with its main model whatever name it does not know - and the text
            # would be the old one over again, under a new name: so it is asked what it has.
            known = gate.models()
            if known is None:
                raise gateway.Refused(NO_CHOICE)
            if not any(m.name == model and m.installed for m in known):
                raise gateway.NoModel(model)
            self.status = WORKING
            sides = [(name, speaker, side.frames(), side.read)
                     for channel, (name, speaker) in enumerate(TRACKS) for side in [opus.Side(file, channel)]]
            phrases = self._pieces(call, gate, sides, model, lambda: self._wanted(call.id, model))
            if not phrases:     # a model that heard nothing does not wipe out a text that is there
                self._kept[True] = (0, {})      # ...and asked once more, it is read once more
                raise gateway.Refused(NO_WORDS)
            written = self._store.redone(call.id, phrases, model, self._today())
        except gateway.Unavailable as e:
            self._waits(e)
            return
        except _GaveWay:
            self.status = IDLE
            return
        except gateway.NoModel:
            why = NO_MODEL
        except gateway.ModelFailed as e:
            why, said = NO_LOAD, str(e)
        except gateway.TimedOut as e:
            why, said = NO_ANSWER, str(e)
        except FileNotFoundError:
            why = NO_SOUND
        except OSError as e:        # the small file is there and is not sound that can be read
            why, said = BAD_SOUND, str(e)
        except gateway.Refused as e:
            why = str(e)
        except Exception as e:      # what nobody foresaw: the text there is stays, and that is said
            if self._stop.is_set():     # the program is leaving, the archive may be closed under us: the request stays
                raise
            why = type(e).__name__
            log.exception("call %d: recognising it anew failed in a way nobody foresaw", call.id)
        else:
            self._complained, self._kept[True], self.status = "", (0, {}), IDLE     # asked once more, it is read once more
            if written:
                log.info("call %d (%s, %.0f s) is recognised anew with %s: %d phrases", call.id, call.program,
                         call.duration_s or 0.0, model, len(phrases))
                topic.refresh(self._store, call.id)
            return
        self.status = IDLE
        # Nothing is said if it was taken back or deleted meanwhile: then it is nobody's failure.
        if self._store.redo_failed(call.id, model, why):
            log.warning("call %d: recognising it anew with %s failed (%s): it keeps the text it had", call.id, model, said or why)

    def _wanted(self, call_id: int, model: str) -> bool:
        """Is this conversation still to be read anew with this model - not deleted, the request
        not taken back - and does none wait for its first text? If not, the reading stops here."""
        now = self._store.get(call_id)
        return now is not None and now.redo_model == model and not self._store.counts().get(RECORDED)

    def _recognise(self, call: Call, gate: gateway.Gateway) -> list[Phrase]:
        """The first text of a conversation, from the recordings of its working folder."""
        folder = self._store.folder(call)
        # recorded without one side: the other one is the conversation
        sides = [(name, speaker, audio.frames(path), lambda start, count, path=path: audio.read_frames(path, start, count))
                 for name, speaker in TRACKS for path in [folder / name] if path.is_file()]
        if not sides:
            raise gateway.Refused("the conversation has no recordings")
        return self._pieces(call, gate, sides)

    def _pieces(self, call: Call, gate: gateway.Gateway, sides: list[tuple], model: str = "",
                go_on: Callable[[], bool] = lambda: True) -> list[Phrase]:
        """Every side cut into pieces, each piece read by the gateway - by `model`, if one is
        named - and the phrases of both put on one clock. A piece that was read before this
        thread had to step aside is not asked for again - by the same model: what one model
        read is nothing to another. `go_on()` is asked before every piece: no - and the thread
        steps aside as it does for a conversation that began."""
        anew = bool(model)
        if self._kept[anew][0] != (call.id, model):
            self._kept[anew] = ((call.id, model), {})
        kept, phrases = self._kept[anew][1], []
        for name, speaker, total, read in sides:
            for index, (start, count) in enumerate(cuts(total, read)):
                if (name, index) not in kept:
                    if self._stop.is_set() or self._busy() or not go_on():
                        raise _GaveWay()
                    kept[name, index] = gate.transcribe(audio.wav(read(start, count)), model)
                offset = start / audio.RATE
                phrases += [Phrase(speaker, round(s.start + offset, 3), round(s.end + offset, 3), s.text)
                            for s in kept[name, index]]
        return sorted(phrases, key=lambda phrase: (phrase.start_s, phrase.speaker))

    def _finish(self) -> bool:
        """A conversation recognised before this start may have been left with its recordings not
        made small: the program was closed at that very moment, or the disk was full. And an
        earlier version of the program may have left its files where they no longer lie. Each
        is looked at once after a start, and the recordings of one are made small in a turn -
        a conversation that begins is not kept waiting. True if that was done."""
        if self._before is None:
            self._before = deque(self._store.calls(DONE))
            self._clear()
        while self._before:
            call = self._before.popleft()
            if call.topic is None:      # recognised by a version that did not yet say what a conversation is about
                topic.refresh(self._store, call.id)
            folder = self._store.folder(call)
            if folder.is_dir():
                self._move(call, folder)
            self._forget_text(call, self._store.root / f"{call.folder}{OLD_TEXT}")
            if call.audio_deleted_at is None and any((folder / name).is_file() for name, _ in TRACKS):
                log.info("call %d: its files are finished: a start before this one left them half done", call.id)
                self._sound(call)
                return True
        return False

    def _forget_text(self, call: Call, path: Path) -> None:
        """The text of a recognised conversation is in the archive. The copy for reading that an
        earlier version of the program wrote goes: left there, it would be taken for the text."""
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("call %d: the file of its text could not be deleted (%s): tried again at the next start",
                        call.id, type(e).__name__)

    def _move(self, call: Call, folder: Path) -> None:
        """An older version of the program kept the sound, the text and the summary of a
        conversation inside its folder. The sound is moved to where it lies now; the summary
        goes into the archive - unless the archive has one of its own already; the text is in
        the archive, and its file goes."""
        for bring in (self._bring_summary, self._bring_sound):     # one that cannot be brought does not hold the other back
            try:
                bring(call, folder)
            except (OSError, UnicodeDecodeError, sqlite3.Error) as e:
                log.warning("call %d: its files could not be moved to where they lie now (%s): tried again at the next start",
                            call.id, type(e).__name__)
        self._forget_text(call, folder / OLDER_TEXT)
        self._tidy(folder)

    def _bring_summary(self, call: Call, folder: Path) -> None:
        summary = folder / "summary.md"
        if summary.is_file() and not self._store.summaries(call.id):
            self._store.save_summary(call.id, MADE, summary.read_text(encoding="utf-8-sig"), self._today())
        if self._store.summaries(call.id):
            summaries.settle(self._store, call)     # the file beside the sound - also one that could not be written before
        summary.unlink(missing_ok=True)             # last: until the copy is there, this one is

    def _bring_sound(self, call: Call, folder: Path) -> None:
        if (folder / "audio.opus").is_file():
            os.replace(folder / "audio.opus", self._store.sound(call))

    def _clear(self) -> None:
        """Half files a program that was killed left behind - of the sound, of a summary -
        beside the working folders and inside them."""
        root = self._store.root
        for half in [*root.glob("*.part"), *root.glob("*/*.part")]:
            try:
                if datetime.fromtimestamp(half.stat().st_mtime) < self._since:
                    half.unlink()
            except OSError as e:
                log.warning("%s could not be deleted (%s)", half.name, e)

    def _sound(self, call: Call) -> None:
        folder = self._store.folder(call)
        mic, line = folder / "mic.wav", folder / "line.wav"
        if self._cfg.storage.keep_audio_days == 0:      # the settings say: no sound once there is the text
            if self._delete(call, mic, line):
                self._store.audio_deleted(call.id, self._today())
                self._tidy(folder)
            return
        try:
            self._compress(mic, line, self._store.sound(call))
        except Exception as e:      # whatever the library throws
            log.error("call %d: the sound could not be compressed (%s: %s): the recordings stay as they are",
                      call.id, type(e).__name__, e)
            return
        if self._store.get(call.id) is None:        # forgotten while its sound was being made small: none of it stays
            self._delete(call, self._store.sound(call), mic, line)
            self._tidy(folder)
            return
        if self._delete(call, mic, line):
            self._tidy(folder)

    def _tidy(self, folder: Path) -> None:
        """The working folder goes when its recordings are gone - unless something else lies in
        it: only what the program made is the program's to remove."""
        try:
            folder.rmdir()
        except OSError:
            pass

    def _delete(self, call: Call, *paths: Path) -> bool:
        """True when none of them is left. One that is held open - by a player, say - stays, and
        is not said to be deleted: the sweep of the next day tries again."""
        gone = True
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                gone = False
                log.warning("call %d: %s could not be deleted (%s)", call.id, path.name, e)
        return gone

    def _sweep(self) -> None:
        """The sound of a recognised conversation is kept for so many days; its text stays."""
        days = self._cfg.storage.keep_audio_days
        if days < 0:
            return
        today = self._today()
        for call in self._store.expired(today - timedelta(days=days)):
            folder = self._store.folder(call)
            # ...wherever it lies: beside the folder, still as two recordings, or where an older version kept it
            if self._delete(call, self._store.sound(call), folder / "mic.wav", folder / "line.wav", folder / "audio.opus"):
                self._store.audio_deleted(call.id, today)
                self._tidy(folder)
                log.info("call %d: its sound is deleted after %d days; the text stays", call.id, days)
