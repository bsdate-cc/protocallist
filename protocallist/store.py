"""The archive: a database of conversations and a folder for the files of each.

The database knows what there is and in what state, and holds what was said and the
summaries. In the folder a conversation has a working folder while its two recordings are
there; what is kept of it lies beside that, under its name: the sound and a copy of the
summary in force. Several threads of the program use it - the one that records, the one that
recognises, those that make summaries, those of the panel - through one connection, so every
question and every change waits its turn; other programs may read the database at any time.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# One list of statements per version of the schema; a database is brought up to date by
# running the lists it has not seen yet.
_SCHEMA = [
    [
        """CREATE TABLE calls (
               id               INTEGER PRIMARY KEY,
               started_at       TEXT NOT NULL,
               ended_at         TEXT,
               duration_s       REAL,
               program          TEXT NOT NULL,
               title            TEXT,
               folder           TEXT NOT NULL,
               state            TEXT NOT NULL,
               error            TEXT,
               attempts         INTEGER NOT NULL DEFAULT 0,
               model            TEXT,
               audio_deleted_at TEXT
           )""",
        """CREATE TABLE phrases (
               id       INTEGER PRIMARY KEY,
               call_id  INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
               speaker  TEXT NOT NULL,
               voice    INTEGER,
               start_s  REAL NOT NULL,
               end_s    REAL NOT NULL,
               text     TEXT NOT NULL
           )""",
        "CREATE INDEX phrases_by_call ON phrases (call_id, start_s)",
    ],
    [   # 2: the summaries of a conversation live here, and what it is about - for a list of conversations;
        # and the last number given to a conversation, so that none is ever given to another
        """CREATE TABLE summaries (
               call_id   INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
               kind      TEXT NOT NULL CHECK (kind IN ('made', 'mine')),
               text      TEXT NOT NULL,
               saved_at  TEXT NOT NULL,
               maker     TEXT,
               seconds   REAL,
               PRIMARY KEY (call_id, kind)
           )""",
        "ALTER TABLE calls ADD COLUMN topic TEXT",
        "CREATE TABLE numbers (last INTEGER NOT NULL)",
        "INSERT INTO numbers VALUES (0)",      # begin() goes on from the numbers the archive holds
    ],
    [   # 3: a recognised conversation can be recognised anew, with another model - it keeps its text until
        # the new one is there; and a text knows when it was written, so that a summary can be told to be older
        "ALTER TABLE calls ADD COLUMN redo_model TEXT",
        "ALTER TABLE calls ADD COLUMN redo_error TEXT",
        "ALTER TABLE calls ADD COLUMN text_at TEXT",
        "UPDATE calls SET text_at = ended_at WHERE state = 'done'",
    ],
]

RECORDING, RECORDED, TRANSCRIBING, DONE, FAILED = "recording", "recorded", "transcribing", "done", "failed"
MAX_ATTEMPTS = 3            # so many times a conversation is tried before it is failed for good
MADE, MINE = "made", "mine"     # the two summaries a conversation may have
_UNSAFE = re.compile(r"[^\w.-]+")
# What the program itself puts under the name of a conversation: beside its working folder, and inside
# it (the last three names are where an older version kept the sound, the text and the summary).
_BESIDE = (".opus", ".md", ".transcript.md")
_INSIDE = ("mic.wav", "line.wav", "audio.opus", "transcript.md", "summary.md")


@dataclass(frozen=True)
class Call:
    id: int
    started_at: str             # local time with its offset, ISO 8601
    ended_at: str | None
    duration_s: float | None
    program: str
    title: str | None
    folder: str                 # its name: of its working folder, and of its files beside that
    state: str
    error: str | None
    attempts: int
    model: str | None
    audio_deleted_at: str | None
    topic: str | None           # what it is about, in one line
    redo_model: str | None      # the model it is to be recognised anew with; None - nobody asked
    redo_error: str | None      # why the last attempt to recognise it anew came to nothing
    text_at: str | None         # when its text was written, ISO 8601


@dataclass(frozen=True)
class Phrase:
    speaker: str                # "me" - the microphone; "them" - what was heard
    start_s: float              # from the beginning of the conversation
    end_s: float
    text: str


@dataclass(frozen=True)
class Summary:
    kind: str                   # MADE - by a model or a command; MINE - corrected by the user
    text: str
    saved_at: str               # local time with its offset, ISO 8601
    maker: str | None           # of one that was made: the model, or the words of the user's command
    seconds: float | None       # of one that was made: how long it took


def _lower(text):
    """Lower case for the whole of Unicode: SQLite itself folds only the Latin letters."""
    return text.lower() if isinstance(text, str) else text


def _stamp(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, database: Path, calls: Path) -> None:
        self._calls = calls
        self._lock = threading.RLock()      # one connection, several threads: one statement at a time
        self._db = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode = WAL")     # readers do not wait for the writer
            self._db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
            # A long look through everything that was said has a connection of its own: the
            # thread that records does not wait for a search.
            self._looking, self._look = threading.Lock(), sqlite3.connect(database, check_same_thread=False)
            self._look.create_function("lower_u", 1, _lower, deterministic=True)
        except sqlite3.Error:
            self._db.close()        # a file that is not a database: let go of it before saying so
            raise

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for number, statements in enumerate(_SCHEMA[version:], version + 1):
            with self._lock, self._db:
                self._db.execute("BEGIN")
                for statement in statements:
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {number}")

    def close(self) -> None:
        with self._lock:        # after the statement of the other thread, never inside it
            self._db.close()
        with self._looking:
            self._look.close()

    # ---- reading

    def _rows(self, sql: str, values: tuple = ()) -> list[sqlite3.Row]:
        """Every row of a question, asked in its turn."""
        with self._lock:
            return self._db.execute(sql, values).fetchall()

    def get(self, call_id: int) -> Call | None:
        rows = self._rows("SELECT * FROM calls WHERE id = ?", (call_id,))
        return Call(**dict(rows[0])) if rows else None

    def calls(self, state: str | None = None) -> list[Call]:
        """Conversations, the earliest first; only those in `state` if one is given."""
        where, values = ("WHERE state = ?", (state,)) if state else ("", ())
        return [Call(**dict(row)) for row in self._rows(f"SELECT * FROM calls {where} ORDER BY started_at, id", values)]

    @property
    def root(self) -> Path:
        """The folder of all conversations."""
        return self._calls

    def folder(self, call: Call) -> Path:
        """The working folder: the two recordings, while they are there."""
        return self._calls / call.folder

    def sound(self, call: Call) -> Path:
        """Both sides in one small file."""
        return self._calls / f"{call.folder}.opus"

    def summary_file(self, call: Call) -> Path:
        """Where the copy of the summary in force lies: beside the sound, under the name of the conversation."""
        return self._calls / f"{call.folder}.md"

    def phrases(self, call_id: int) -> list[Phrase]:
        """What was said in a conversation, in the order it was said."""
        rows = self._rows("SELECT speaker, start_s, end_s, text FROM phrases WHERE call_id = ? ORDER BY start_s, id",
                          (call_id,))
        return [Phrase(*row) for row in rows]

    def summaries(self, call_id: int) -> dict[str, Summary]:
        """The summaries of a conversation by their kind - whichever of the two there are."""
        rows = self._rows("SELECT kind, text, saved_at, maker, seconds FROM summaries WHERE call_id = ?", (call_id,))
        return {row[0]: Summary(*row) for row in rows}

    def summary(self, call_id: int) -> Summary | None:
        """The summary in force: the user's own if there is one, else the one that was made."""
        both = self.summaries(call_id)
        return both.get(MINE) or both.get(MADE)

    def kinds(self) -> dict[int, str]:
        """Of every conversation that has a summary, the kind of the one in force."""
        return {row[0]: row[1] for row in self._rows("SELECT call_id, MAX(kind) FROM summaries GROUP BY call_id")}

    def remarks(self, call_id: int) -> int:
        """How many remarks a conversation has: phrases of one side that follow one another are one."""
        rows = self._rows("SELECT SUM(turn) FROM (SELECT speaker IS NOT LAG(speaker) OVER (ORDER BY start_s, id) AS turn "
                          "FROM phrases WHERE call_id = ?)", (call_id,))
        return rows[0][0] or 0

    def search(self, words: str) -> set[int]:
        """The conversations in whose text or summaries these words stand, whatever the case of
        the letters. Every phrase of the archive is looked at: seconds, for years of talk."""
        needle = words.strip().lower()
        if not needle:
            return set()
        with self._looking:
            return {row[0] for row in self._look.execute(
                "SELECT call_id FROM phrases WHERE instr(lower_u(text), ?) "
                "UNION SELECT call_id FROM summaries WHERE instr(lower_u(text), ?)", (needle, needle))}

    def counts(self) -> dict[str, int]:
        """How many conversations there are in each state."""
        return {row[0]: row[1] for row in self._rows("SELECT state, COUNT(*) FROM calls GROUP BY state")}

    # ---- the life of a conversation

    def begin(self, started: datetime, program: str, title: str = "") -> Call:
        """A conversation that is being recorded, with a folder of its own."""
        base = f"{started.strftime('%Y-%m-%d_%H-%M-%S')}_{_UNSAFE.sub('_', program) or 'call'}"
        name, n = base, 1
        while self._taken(name):
            n += 1
            name = f"{base}_{n}"
        (self._calls / name).mkdir(parents=True)
        with self._lock, self._db:
            # SQLite would give the number of the last conversation again once that one is deleted - and
            # a summary on its way, or a text half recognised, would land in a conversation it is not of.
            self._db.execute("BEGIN")
            self._db.execute("UPDATE numbers SET last = MAX(last, (SELECT COALESCE(MAX(id), 0) FROM calls)) + 1")
            number = self._db.execute("SELECT last FROM numbers").fetchone()[0]
            self._db.execute(
                "INSERT INTO calls (id, started_at, program, title, folder, state) VALUES (?, ?, ?, ?, ?, ?)",
                (number, _stamp(started), program, title or None, name, RECORDING))
        return self.get(number)

    def _taken(self, name: str) -> bool:
        """Is anything left under this name: a record, a working folder, a file beside it."""
        return bool(self._rows("SELECT 1 FROM calls WHERE folder = ?", (name,))) or (self._calls / name).exists() \
            or any(self._calls.glob(f"{glob.escape(name)}.*"))

    def finish(self, call_id: int, duration_s: float) -> Call:
        """The recording is over and whole: it waits to be recognised."""
        call = self.get(call_id)
        ended = datetime.fromisoformat(call.started_at) + timedelta(seconds=duration_s)
        with self._lock:
            self._db.execute("UPDATE calls SET ended_at = ?, duration_s = ?, state = ? WHERE id = ?",
                             (_stamp(ended), round(duration_s, 3), RECORDED, call_id))
        return self.get(call_id)

    def discard(self, call_id: int) -> None:
        """Forget a conversation altogether: its record and its files."""
        call = self.get(call_id)
        if call is None:
            return
        with self._lock:
            self._db.execute("DELETE FROM calls WHERE id = ?", (call_id,))
        self._remove(call)

    def forget(self, call_id: int) -> bool:
        """Forget a conversation altogether, at the user's word - unless it is being recorded or
        recognised right now: then nothing of it is touched, and the answer is False.

        All of its files go, or none. Each is first moved aside under another name - which cannot
        be done to a file that some program has open - and only when all have moved are they
        deleted, and the record with them. A file that cannot be moved is an OSError: those moved
        before it are moved back, and the conversation stays whole, to be forgotten when the file
        is let go of. Only what the program made goes: anything else under the name, and a working
        folder that holds anything else, stays."""
        with self._lock:        # nobody takes the conversation to recognise it in the middle of this
            call = self.get(call_id)
            if call is None:
                return True
            if call.state in (RECORDING, TRANSCRIBING):
                return False
            aside: list[tuple[Path, Path]] = []
            try:
                for path in self._own(call):
                    away = path.with_name(path.name + ".going")
                    os.replace(path, away)
                    aside.append((path, away))
            except OSError:
                for path, away in reversed(aside):
                    os.replace(away, path)
                raise
            self._db.execute("DELETE FROM calls WHERE id = ?", (call_id,))
            for _, away in aside:
                try:
                    away.unlink()
                except OSError as e:
                    log.warning("%s could not be deleted (%s)", away.name, e)
            try:
                self.folder(call).rmdir()       # gone if nothing else lies in it
            except OSError:
                pass
        return True

    def _own(self, call: Call) -> list[Path]:
        """The files of a conversation that the program made, whole or half written."""
        folder = self.folder(call)
        made = [self._calls / f"{call.folder}{ending}" for ending in _BESIDE] + [folder / name for name in _INSIDE]
        return [path for whole in made for path in (whole, whole.with_name(whole.name + ".part")) if path.is_file()]

    def _remove(self, call: Call) -> None:
        """The files of a conversation that the program made go, as far as they can be deleted;
        anything else under its name, and a working folder that holds anything else, stays."""
        for path in self._own(call):
            try:
                path.unlink()
            except OSError as e:
                log.warning("%s could not be deleted (%s)", path.name, e)
        try:
            self.folder(call).rmdir()
        except OSError:
            pass

    # ---- recognition

    def take(self) -> Call | None:
        """The earliest conversation that waits to be recognised, marked as being recognised now."""
        with self._lock:
            row = self._db.execute("SELECT id FROM calls WHERE state = ? ORDER BY started_at, id LIMIT 1",
                                   (RECORDED,)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE calls SET state = ? WHERE id = ?", (TRANSCRIBING, row[0]))
        return self.get(row[0])

    def put_back(self, call_id: int) -> None:
        """Recognition could not even be tried - the gateway is not there, or a conversation
        began: wait again, and count no attempt."""
        with self._lock:
            self._db.execute("UPDATE calls SET state = ? WHERE id = ? AND state = ?", (RECORDED, call_id, TRANSCRIBING))

    def missed(self, call_id: int, error: str) -> Call:
        """An attempt that failed. The conversation waits for the next one - or, after the
        last, is failed for good with the reason. Its sound stays either way."""
        with self._lock:
            self._db.execute("UPDATE calls SET attempts = attempts + 1, error = ?, "
                             "state = CASE WHEN attempts + 1 >= ? THEN ? ELSE ? END WHERE id = ?",
                             (error, MAX_ATTEMPTS, FAILED, RECORDED, call_id))
        return self.get(call_id)

    def done(self, call_id: int, phrases: list[Phrase], model: str, moment: datetime | None = None) -> Call:
        """The text of a conversation, whole or not at all: its phrases and the new state in
        one transaction. `moment` is when it was written; now, if none is named."""
        with self._lock, self._db:
            self._db.execute("BEGIN")
            self._write(call_id, phrases)
            self._db.execute("UPDATE calls SET state = ?, model = ?, error = NULL, text_at = ? WHERE id = ?",
                             (DONE, model, _stamp(moment or datetime.now()), call_id))
        return self.get(call_id)

    def _write(self, call_id: int, phrases: list[Phrase]) -> None:
        """Inside a transaction: these phrases in place of whatever the conversation had."""
        self._db.execute("DELETE FROM phrases WHERE call_id = ?", (call_id,))
        self._db.executemany("INSERT INTO phrases (call_id, speaker, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
                             [(call_id, p.speaker, p.start_s, p.end_s, p.text) for p in phrases])

    # ---- recognition anew: the conversation has a text and keeps it until the new one is there

    def redo(self, call_id: int, model: str) -> bool:
        """Ask for a recognised conversation to be recognised anew with this model. False if it
        is not a recognised one, or is asked for already."""
        with self._lock:
            return bool(self._db.execute("UPDATE calls SET redo_model = ?, redo_error = NULL "
                                         "WHERE id = ? AND state = ? AND redo_model IS NULL", (model, call_id, DONE)).rowcount)

    def take_redo(self) -> Call | None:
        """The earliest conversation that is to be recognised anew. It stays asked for until
        redone() or redo_failed(): a program closed in the middle begins it again."""
        rows = self._rows("SELECT * FROM calls WHERE redo_model IS NOT NULL AND state = ? ORDER BY started_at, id LIMIT 1",
                          (DONE,))
        return Call(**dict(rows[0])) if rows else None

    def redone(self, call_id: int, phrases: list[Phrase], model: str, moment: datetime) -> bool:
        """The new text in place of the old, whole or not at all. False, and nothing written, if
        it is not asked for with this model any more: the conversation is gone, the request was
        taken back - or made again with another model, which is another attempt."""
        with self._lock, self._db:
            self._db.execute("BEGIN")
            if not self._db.execute("UPDATE calls SET model = ?, text_at = ?, redo_model = NULL, redo_error = NULL "
                                    "WHERE id = ? AND redo_model = ?", (model, _stamp(moment), call_id, model)).rowcount:
                return False
            self._write(call_id, phrases)
        return True

    def cancel_redo(self, call_id: int) -> bool:
        """The request is taken back: the conversation keeps the text it has, and what was being
        read for it is written nowhere. False if it was not asked for."""
        with self._lock:
            return bool(self._db.execute("UPDATE calls SET redo_model = NULL WHERE id = ? AND redo_model IS NOT NULL",
                                         (call_id,)).rowcount)

    def redo_failed(self, call_id: int, model: str, error: str) -> bool:
        """The attempt with this model came to nothing: the conversation keeps the text it had, and
        says why. False, and nothing said, if it is not asked for with this model any more."""
        with self._lock:
            return bool(self._db.execute("UPDATE calls SET redo_model = NULL, redo_error = ? WHERE id = ? AND redo_model = ?",
                                         (error, call_id, model)).rowcount)

    def retry(self, call_id: int) -> bool:
        """A failed conversation waits to be recognised again, its attempts forgotten. False if it is not a failed one."""
        with self._lock:
            return bool(self._db.execute("UPDATE calls SET state = ?, attempts = 0, error = NULL WHERE id = ? AND state = ?",
                                         (RECORDED, call_id, FAILED)).rowcount)

    def retry_failed(self) -> int:
        """Every failed conversation waits to be recognised again, its attempts forgotten; how many there were."""
        with self._lock:
            return self._db.execute("UPDATE calls SET state = ?, attempts = 0, error = NULL WHERE state = ?",
                                    (RECORDED, FAILED)).rowcount

    # ---- summaries

    def save_summary(self, call_id: int, kind: str, text: str, moment: datetime, maker: str | None = None,
                     seconds: float | None = None) -> None:
        """Keep a summary in place of the one of its kind - and of that kind only: one that is
        made anew never touches the user's own."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO summaries (call_id, kind, text, saved_at, maker, seconds) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (call_id, kind, text, _stamp(moment), maker, seconds))

    def set_topic(self, call_id: int, topic: str) -> None:
        with self._lock:
            self._db.execute("UPDATE calls SET topic = ? WHERE id = ?", (topic, call_id))

    # ---- how long the sound is kept

    def expired(self, before: datetime) -> list[Call]:
        """Recognised conversations that ended before `before` and still have their sound."""
        rows = self._rows("SELECT * FROM calls WHERE state = ? AND audio_deleted_at IS NULL AND ended_at IS NOT NULL "
                          "ORDER BY started_at, id", (DONE,))
        calls = [Call(**dict(row)) for row in rows]
        return [call for call in calls if datetime.fromisoformat(call.ended_at) < before.astimezone()]

    def audio_deleted(self, call_id: int, moment: datetime) -> None:
        with self._lock:
            self._db.execute("UPDATE calls SET audio_deleted_at = ? WHERE id = ?", (_stamp(moment), call_id))

    # ---- after a crash

    def recover(self, length_s: Callable[[Path], float], min_seconds: float) -> None:
        """Put in order what a dead process left. `length_s` repairs the files of a folder
        and says how many seconds of sound it holds."""
        for call in self.calls(RECORDING):
            folder = self.folder(call)
            try:
                seconds = length_s(folder) if folder.is_dir() else 0.0
            except OSError as e:        # another program holds a file, or it cannot be written to
                log.warning("recovery: a recording by %s could not be put in order (%s): left for the next start",
                            call.program, e)
                continue
            if seconds < min_seconds:
                log.info("recovery: a recording of %.0f s by %s is dropped", seconds, call.program)
                self.discard(call.id)
            else:
                log.info("recovery: a recording of %.0f s by %s was cut short, kept", seconds, call.program)
                self.finish(call.id, seconds)
        for call in self.calls(TRANSCRIBING):
            log.info("recovery: call %d goes back to wait for recognition", call.id)
            with self._lock:
                self._db.execute("UPDATE calls SET state = ? WHERE id = ?", (RECORDED, call.id))
        known = {call.folder for call in self.calls()}
        if self._calls.is_dir():
            for folder in sorted(p.name for p in self._calls.iterdir() if p.is_dir() and p.name not in known):
                log.warning("recovery: the folder %s belongs to no conversation, left as it is", folder)
        for call in self.calls():
            if call.state != DONE and not self.folder(call).is_dir():     # one that is recognised needs none
                log.warning("recovery: call %d has no folder, left as it is", call.id)
