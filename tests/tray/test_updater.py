"""Updates through git: only ever a fast-forward of main from origin, only for a clean install.
The git here is real, in folders made for the test: an origin, a developer's clone, an install."""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from protocallist.i18n import Text
from protocallist.tray import updater

REAL_STAMP_FILE = updater.stamp_file       # taken before any test: conftest puts a temp file in its place

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), env=ENV, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


def write_version(repo: Path, version: str) -> None:
    (repo / "protocallist").mkdir(exist_ok=True)
    (repo / "protocallist" / "__init__.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Built once: origin (bare), a developer's clone that pushes to it, and an installed clone."""
    base = tmp_path_factory.mktemp("world")
    origin, dev, home = base / "origin.git", base / "dev", base / "home"
    git(base, "init", "-q", "--bare", "-b", "main", str(origin))
    git(base, "clone", "-q", str(origin), str(dev))
    write_version(dev, "0.3.1")
    (dev / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    commit(dev, "first")
    git(dev, "push", "-q", "origin", "HEAD:main")
    git(base, "clone", "-q", str(origin), str(home))
    return base


@pytest.fixture
def world(template, tmp_path):
    """A private copy of the template for one test: the developer's clone and the install under test."""
    shutil.copytree(template, tmp_path / "w")
    origin, dev, home = tmp_path / "w" / "origin.git", tmp_path / "w" / "dev", tmp_path / "w" / "home"
    for clone in (dev, home):
        git(clone, "remote", "set-url", "origin", str(origin))
    return dev, home


def release(dev: Path, version: str, requirements: str | None = None) -> None:
    write_version(dev, version)
    if requirements is not None:
        (dev / "requirements.txt").write_text(requirements, encoding="utf-8")
    commit(dev, f"release {version}")
    git(dev, "push", "-q", "origin", "HEAD:main")


# --- asking origin


def test_an_install_level_with_origin_has_nothing_to_do(world):
    dev, home = world
    assert updater.status(home) == updater.Update("none")


def test_a_newer_main_on_origin_is_offered_with_its_version(world):
    dev, home = world
    release(dev, "0.4.0")
    update = updater.status(home)
    assert (update.kind, update.version) == ("behind", "0.4.0")
    assert update.commit == git(dev, "rev-parse", "HEAD")       # the very commit the click will agree to


def test_a_folder_that_is_not_a_clone_gets_no_updates_and_no_remark(tmp_path):
    assert updater.status(tmp_path) == updater.Update("off")


def test_a_clone_without_origin_gets_no_updates_and_no_remark(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main", str(tmp_path))
    write_version(tmp_path, "0.3.1")
    commit(tmp_path, "first")
    assert updater.status(tmp_path) == updater.Update("off")


def test_a_clone_on_another_branch_is_a_developers_and_is_told_why(world):
    dev, home = world
    release(dev, "0.4.0")
    git(home, "checkout", "-q", "-b", "feature")
    assert updater.status(home) == updater.Update("off", note=Text("ветка {branch} — обновления выключены",
                                                                    branch="feature"))
    git(home, "checkout", "-q", "--detach")
    assert updater.status(home) == updater.Update("off", note=Text("без ветки — обновления выключены"))


def test_a_clone_with_commits_of_its_own_is_never_offered_anything(world):
    dev, home = world
    (home / "note.txt").write_text("mine", encoding="utf-8")
    commit(home, "local work")                     # ahead of origin, nothing new there
    update = updater.status(home)
    assert update.kind == "off" and "свои коммиты" in str(update.note)


def test_a_clone_whose_history_parted_from_origin_is_never_offered_anything(world):
    dev, home = world
    release(dev, "0.4.0")
    (home / "note.txt").write_text("mine", encoding="utf-8")
    commit(home, "local work")
    update = updater.status(home)
    assert update.kind == "off" and "разошлась" in str(update.note)


def test_an_origin_that_cannot_be_reached_means_do_not_know_never_behind(world):
    dev, home = world
    release(dev, "0.4.0")
    git(home, "remote", "set-url", "origin", str(home.parent / "gone.git"))
    assert updater.status(home) == updater.Update("unknown")


def test_a_folder_inside_somebody_elses_repository_is_not_an_install(world):
    dev, home = world
    inner = home / "vendor" / "copy"
    inner.mkdir(parents=True)
    assert updater.status(inner) == updater.Update("off")


# --- git itself


def alive(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259                                          # STILL_ACTIVE


def test_a_command_whose_child_keeps_running_still_times_out_and_takes_the_child_with_it(tmp_path):
    # `git fetch` leaves git-remote-https (or the credential manager) behind. With pipes,
    # subprocess.run waits for every holder of the pipe - long after its own timeout.
    pidfile = tmp_path / "grandchild.pid"
    grandchild = f"import os, time; open(r'{pidfile}', 'w').write(str(os.getpid())); time.sleep(60)"
    child = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        updater._run([sys.executable, "-c", child], tmp_path, timeout=2.0)
    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 5
    while alive(int(pidfile.read_text())) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(int(pidfile.read_text()))


def test_git_never_asks_for_credentials_by_itself(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    env = updater._environment()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"].lower() == "never"
    # ssh is left exactly as the user has it: GIT_SSH_COMMAND in the environment would
    # override their core.sshCommand / GIT_SSH (an agent, plink) and break a fetch that works.
    assert "GIT_SSH_COMMAND" not in env


def test_a_git_that_is_not_there_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    def no_git(args, cwd, timeout=120.0):
        raise FileNotFoundError("git")

    monkeypatch.setattr(updater, "_run", no_git)
    assert updater._git(tmp_path, "status") == (False, "")
    assert updater.status(tmp_path) == updater.Update("off")


# --- taking an update


def test_pull_fast_forwards_and_says_what_changed(world):
    dev, home = world
    release(dev, "0.4.0")
    old = git(home, "rev-parse", "HEAD")
    pulled = updater.pull(home, updater.status(home))
    assert pulled.old == old and pulled.new == git(home, "rev-parse", "HEAD") != old
    assert pulled.requirements_changed is False
    assert updater.disk_version(home) == "0.4.0"
    assert updater.status(home).kind == "none"


def test_pull_reports_changed_requirements(world):
    dev, home = world
    release(dev, "0.4.0", requirements="numpy\nsoundfile\n")
    assert updater.pull(home, updater.status(home)).requirements_changed is True


def test_pull_refuses_a_clone_with_edited_files_and_leaves_it_alone(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    (home / "requirements.txt").write_text("edited by hand\n", encoding="utf-8")
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError, match="изменённые файлы"):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "HEAD") == head
    assert (home / "requirements.txt").read_text(encoding="utf-8") == "edited by hand\n"


# The menu line may be hours old: everything it was based on is checked again at the click.

def test_pull_refuses_a_clone_that_left_main_since_the_check(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    assert seen.kind == "behind"
    git(home, "checkout", "-q", "-b", "hotfix")
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError, match="hotfix"):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "hotfix") == head


def test_pull_refuses_a_detached_head(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    git(home, "checkout", "-q", "--detach")
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "HEAD") == head


def test_pull_refuses_a_version_other_than_the_one_the_user_clicked_on(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    release(dev, "0.5.0")
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError, match="0.5.0"):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "HEAD") == head


def test_pull_refuses_another_commit_even_under_the_same_version_number(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    (dev / "extra.py").write_text("print('slipped in')\n", encoding="utf-8")
    commit(dev, "more work, version untouched")
    git(dev, "push", "-q", "origin", "HEAD:main")
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "HEAD") == head


def test_pull_with_nothing_to_take_is_refused(world):
    dev, home = world
    stale_line = updater.Update("behind", "0.3.1", commit=git(home, "rev-parse", "HEAD"))
    with pytest.raises(updater.UpdateError, match="обновлять нечего"):
        updater.pull(home, stale_line)


def test_pull_without_a_reachable_origin_is_refused(world):
    dev, home = world
    release(dev, "0.4.0")
    seen = updater.status(home)
    git(home, "remote", "set-url", "origin", str(home.parent / "gone.git"))
    head = git(home, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError, match="origin"):
        updater.pull(home, seen)
    assert git(home, "rev-parse", "HEAD") == head


def test_files_git_does_not_track_do_not_stand_in_the_way(world):
    dev, home = world
    release(dev, "0.4.0")
    (home / "notes.txt").write_text("mine", encoding="utf-8")
    updater.pull(home, updater.status(home))
    assert updater.disk_version(home) == "0.4.0"
    assert (home / "notes.txt").exists()


def test_an_update_error_is_worded_in_the_language_of_the_moment_it_is_shown():
    from protocallist import i18n

    error = updater.UpdateError(Text("git merge не выполнен: {reason}", reason="exit 1"))
    assert str(error) == "git merge не выполнен: exit 1"
    i18n.set_language("en")
    assert str(error) == "git merge failed: exit 1"


# --- the code on disk and the code that runs


def test_the_version_on_disk_is_read_from_the_file_not_from_the_running_program(world):
    dev, home = world
    assert updater.disk_version(home) == "0.3.1"
    write_version(home, "0.9.9")
    assert updater.disk_version(home) == "0.9.9"
    assert updater.disk_version(home / "nowhere") is None


def test_the_code_on_disk_has_a_fingerprint_that_follows_every_change_of_it(world):
    dev, home = world
    before = updater.code_digest(home)
    assert before and before == updater.code_digest(home)
    (home / "protocallist" / "tray").mkdir()
    (home / "protocallist" / "tray" / "app.py").write_text("x = 1\n", encoding="utf-8")
    added = updater.code_digest(home)
    assert added != before                                        # a new module, in a folder of its own
    (home / "protocallist" / "tray" / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert updater.code_digest(home) != added                     # a changed line, the version untouched
    (home / "protocallist" / "tray" / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert updater.code_digest(home) == added                     # put back: the same code again
    (home / "protocallist" / "tray" / "app.py").rename(home / "protocallist" / "tray" / "other.py")
    assert updater.code_digest(home) != added                     # the same lines under another name


def test_what_is_not_the_programs_code_does_not_touch_the_fingerprint(world):
    dev, home = world
    before = updater.code_digest(home)
    (home / "notes.txt").write_text("mine", encoding="utf-8")
    (home / "tests").mkdir()
    (home / "tests" / "test_it.py").write_text("x = 1\n", encoding="utf-8")
    (home / "protocallist" / "__pycache__").mkdir()
    (home / "protocallist" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0")
    assert updater.code_digest(home) == before
    assert updater.code_digest(home / "nowhere") is None


def test_a_fingerprint_that_cannot_be_taken_is_none(world, monkeypatch):
    dev, home = world

    def locked(self):
        raise PermissionError(13, "the file is being written")

    monkeypatch.setattr(Path, "read_bytes", locked)
    assert updater.code_digest(home) is None


# --- which requirements.txt the libraries were installed from


def test_the_requirements_file_has_a_fingerprint(world):
    dev, home = world
    before = updater.requirements_digest(home)
    assert before and before == updater.requirements_digest(home)
    (home / "requirements.txt").write_text("numpy\nsoundfile\n", encoding="utf-8")
    assert updater.requirements_digest(home) != before
    assert updater.requirements_digest(home / "nowhere") is None


def test_an_install_without_a_record_is_not_called_stale(world, libraries_record):
    dev, home = world
    assert updater.installed_digest() is None
    assert updater.libraries_stale(home) is False


def test_after_an_install_the_record_matches_and_a_changed_file_makes_it_stale(world, libraries_record):
    dev, home = world
    updater.mark_installed(home)
    assert libraries_record.exists() and updater.installed_digest() == updater.requirements_digest(home)
    assert updater.libraries_stale(home) is False
    (home / "requirements.txt").write_text("numpy\nsoundfile\n", encoding="utf-8")
    assert updater.libraries_stale(home) is True
    updater.mark_installed(home)
    assert updater.libraries_stale(home) is False


def test_a_record_that_cannot_be_written_is_not_an_error(world, monkeypatch, tmp_path):
    dev, home = world
    monkeypatch.setattr(updater, "stamp_file", lambda: tmp_path / "no" / "such" / "folder" / "stamp")
    updater.mark_installed(home)
    assert updater.installed_digest() is None
    updater.mark_installed(home / "nowhere")                      # no requirements.txt: nothing to record


def test_the_record_lives_inside_the_environment_it_describes():
    assert REAL_STAMP_FILE() == Path(sys.prefix) / "protocallist-requirements.sha256"


# --- what the menu says


def test_the_menu_offers_the_update_by_its_version():
    assert updater.menu_label(updater.Update("behind", "0.4.0"), running="0.3.1", on_disk="0.3.1") == "Обновить до 0.4.0"
    assert updater.menu_label(updater.Update("behind"), running="0.3.1", on_disk="0.3.1") == "Обновить"


def test_code_on_disk_newer_than_the_running_program_asks_for_a_restart():
    label = updater.menu_label(updater.Update("none"), running="0.3.1", on_disk="0.4.0")
    assert label == "Перезапустить: на диске версия 0.4.0"


def test_code_that_changed_under_the_same_version_asks_for_a_restart_too():
    label = updater.menu_label(updater.Update("off"), running="0.3.1", on_disk="0.3.1", code_changed=True)
    assert label == "Перезапустить: код на диске изменился"
    label = updater.menu_label(updater.Update("off"), running="0.3.1", on_disk="0.4.0", code_changed=True)
    assert label == "Перезапустить: на диске версия 0.4.0"       # a version says more than "changed"


def test_an_update_from_origin_comes_before_a_restart():
    label = updater.menu_label(updater.Update("behind", "0.5.0"), running="0.3.1", on_disk="0.4.0", code_changed=True)
    assert label == "Обновить до 0.5.0"


def test_libraries_older_than_requirements_ask_to_be_installed():
    label = updater.menu_label(updater.Update("none"), running="0.4.0", on_disk="0.4.0", stale_libraries=True)
    assert label == "Поставить библиотеки: requirements.txt изменился"


def test_a_restart_for_newer_code_comes_before_the_libraries_line():
    label = updater.menu_label(updater.Update("none"), running="0.3.1", on_disk="0.3.1", code_changed=True,
                               stale_libraries=True)
    assert label == "Перезапустить: код на диске изменился"


def test_nothing_to_do_means_no_menu_line():
    assert updater.menu_label(updater.Update("none"), running="0.3.1", on_disk="0.3.1") is None
    assert updater.menu_label(updater.Update("off"), running="0.3.1", on_disk=None) is None
    assert updater.menu_label(updater.Update("unknown"), running="0.3.1", on_disk="0.3.1") is None
