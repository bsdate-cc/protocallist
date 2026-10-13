"""Where Protocallist keeps its data: one folder per user, overridable for tests."""
from __future__ import annotations

import os
from pathlib import Path

ENV = "PROTOCALLIST_HOME"
NAME = "protocallist"
DEFAULT_FOLDER = "." + NAME
# Until October 2026 the program was "Protokolist" in Latin letters, and so were its folder and four files in it.
BEFORE = "protokolist"
_adopted = False        # the folder of before was looked for: once in the life of a process


class Busy(OSError):
    """The folder the program kept under its name of before cannot be moved."""


def home() -> Path:
    global _adopted
    base = os.environ.get(ENV)
    p = Path(base) if base else Path.home() / DEFAULT_FOLDER
    if not base and not _adopted:       # a folder named by the environment is nobody's folder of before
        adopt(Path.home() / ("." + BEFORE), p)
        _adopted = True
    p.mkdir(parents=True, exist_ok=True)
    return p


def adopt(old: Path, new: Path) -> None:
    """Once: the folder the program kept under its name of before becomes its folder, and the
    files in it that bore that name get the new one. A folder that is there already is left
    as it is, and so is the one of before. Raises Busy if the folder cannot be moved - the
    program of before is still running: better not to start than to start with an empty
    archive beside the full one."""
    if old.is_dir() and not new.exists():
        try:
            old.rename(new)
        except OSError as e:
            raise Busy(f"the data of the program is in {old}, under its name of before, and could not be moved to {new} "
                       f"({type(e).__name__}): the program is still running under that name - end it "
                       f"(Exit in the menu of its icon) and start this one again") from e
    # The log of changes and the index of the database first, the database last: cut short anywhere,
    # the database is never opened under its new name without them.
    for folder, end in (("", ".db-wal"), ("", ".db-shm"), ("", ".db"), ("logs", ".log")):
        was, now = new / folder / (BEFORE + end), new / folder / (NAME + end)
        if was.is_file() and not now.exists():
            was.rename(now)


def _folder(name: str) -> Path:
    p = home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    return home() / "config.json"


def database_file() -> Path:
    return home() / (NAME + ".db")


def lock_file() -> Path:
    return home() / "lock"


def pid_file() -> Path:
    return home() / "pid"


def key_file() -> Path:
    """The secret a browser is let into the panel by. Whoever can read it can read the archive."""
    return home() / "panel.key"


def calls_dir() -> Path:
    return _folder("calls")


def logs_dir() -> Path:
    return _folder("logs")
