"""Where the program keeps its data - and how it takes over what it kept under its name of before."""
import os
from pathlib import Path

import pytest

from protocallist import paths


def folder_of_before(tmp_path: Path) -> Path:
    """A data folder as the program left it while it was called Protokolist in Latin letters."""
    old = tmp_path / ".protokolist"
    (old / "calls" / "2026-10-08_14-02-29_Zoom").mkdir(parents=True)
    (old / "logs").mkdir()
    for name, text in (("config.json", "{}"), ("panel.key", "key"), ("protokolist.db", "db"), ("protokolist.db-wal", "wal"),
                       ("protokolist.db-shm", "shm"), ("logs/protokolist.log", "log"), ("logs/startup.log", "start")):
        (old / name).write_text(text, encoding="utf-8")
    return old


def test_the_folder_of_the_name_of_before_becomes_the_folder_of_the_program(tmp_path):
    old, new = folder_of_before(tmp_path), tmp_path / ".protocallist"
    paths.adopt(old, new)
    assert not old.exists()
    assert sorted(p.name for p in new.iterdir()) == ["calls", "config.json", "logs", "panel.key", "protocallist.db",
                                                     "protocallist.db-shm", "protocallist.db-wal"]
    assert [(new / f"protocallist.db{end}").read_text(encoding="utf-8") for end in ("", "-wal", "-shm")] == ["db", "wal", "shm"]
    assert sorted(p.name for p in (new / "logs").iterdir()) == ["protocallist.log", "startup.log"]
    assert (new / "calls" / "2026-10-08_14-02-29_Zoom").is_dir() and (new / "panel.key").read_text(encoding="utf-8") == "key"
    paths.adopt(old, new)                                                # once is enough: again changes nothing
    assert (new / "protocallist.db").read_text(encoding="utf-8") == "db"


def test_a_folder_that_is_there_already_is_left_as_it_is_and_so_is_the_one_of_before(tmp_path):
    old, new = folder_of_before(tmp_path), tmp_path / ".protocallist"
    new.mkdir()
    (new / "protocallist.db").write_text("the new one", encoding="utf-8")
    paths.adopt(old, new)
    assert (old / "protokolist.db").read_text(encoding="utf-8") == "db"                 # nothing of the one is mixed into the other
    assert sorted(p.name for p in new.iterdir()) == ["protocallist.db"] and (new / "protocallist.db").read_text(encoding="utf-8") == "the new one"


def test_the_database_is_the_last_to_get_its_new_name(tmp_path, monkeypatch):
    """Its log of changes and its index go first: cut short anywhere, the database is never opened under
    the new name without them."""
    old, new = folder_of_before(tmp_path), tmp_path / ".protocallist"
    renamed, real = [], Path.rename
    monkeypatch.setattr(Path, "rename", lambda self, target: renamed.append(self.name) or real(self, target))
    paths.adopt(old, new)
    assert renamed[0] == ".protokolist"
    assert renamed.index("protokolist.db-wal") < renamed.index("protokolist.db") > renamed.index("protokolist.db-shm")


def test_a_taking_over_that_was_cut_short_is_finished_at_the_next_start(tmp_path):
    old, new = folder_of_before(tmp_path), tmp_path / ".protocallist"
    old.rename(new)
    (new / "protokolist.db-wal").rename(new / "protocallist.db-wal")     # the folder moved, one file renamed - and the power went
    paths.adopt(old, new)
    assert sorted(p.name for p in new.glob("proto*")) == ["protocallist.db", "protocallist.db-shm", "protocallist.db-wal"]
    assert (new / "logs" / "protocallist.log").is_file()


def test_a_folder_that_cannot_be_moved_stops_the_program_instead_of_starting_it_with_an_empty_archive(tmp_path, monkeypatch):
    old, new = folder_of_before(tmp_path), tmp_path / ".protocallist"

    def held(self, target):
        raise PermissionError(13, "the folder is in use")

    monkeypatch.setattr(Path, "rename", held)
    with pytest.raises(paths.Busy, match="still running") as stopped:
        paths.adopt(old, new)
    assert isinstance(stopped.value, OSError) and str(old) in str(stopped.value) and str(new) in str(stopped.value)
    assert not new.exists() and (old / "protokolist.db").is_file()


def test_nothing_of_before_is_nothing_to_take_over(tmp_path):
    paths.adopt(tmp_path / ".protokolist", tmp_path / ".protocallist")
    assert not (tmp_path / ".protokolist").exists() and not (tmp_path / ".protocallist").exists()


def test_the_folder_of_the_user_takes_over_the_one_of_before_once_in_the_life_of_a_process(tmp_path, monkeypatch):
    old = folder_of_before(tmp_path)
    monkeypatch.delenv(paths.ENV)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths, "_adopted", False)
    assert paths.home() == tmp_path / ".protocallist" and not old.exists()
    assert paths.database_file() == tmp_path / ".protocallist" / "protocallist.db" and paths.database_file().read_text(encoding="utf-8") == "db"
    old.mkdir()
    assert paths.home() == tmp_path / ".protocallist" and old.is_dir()   # not looked for again


def test_a_folder_named_by_the_environment_takes_nothing_over(tmp_path, monkeypatch):
    """As in every test: whatever the user keeps in their own folder is not touched."""
    old = folder_of_before(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths, "_adopted", False)
    assert paths.home() == Path(os.environ[paths.ENV]) and (old / "protokolist.db").is_file()
    assert not (tmp_path / ".protocallist").exists()
