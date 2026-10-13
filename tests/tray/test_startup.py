"""The window behind protocallist.cmd: the checks one by one, then the whole walk with the world faked."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from protocallist import VERSION, paths
from protocallist import __main__ as entry
from protocallist.tray import startup, updater
from protocallist.tray.startup import Check


# --- the checks


def test_a_python_that_is_new_enough_is_fine_and_an_old_one_is_not():
    assert startup.check_python((3, 12, 4), 2**63 - 1) == Check("Python", "OK", "3.12.4, 64-bit")
    old = startup.check_python((3, 11, 9), 2**63 - 1)
    assert old.status == "FAIL" and "3.12 or newer" in old.details[0]
    assert startup.check_python().status == "OK"              # the one that runs these tests


def test_missing_libraries_are_named_the_way_pip_knows_them():
    def importer(name):
        if name in ("pystray", "PIL"):
            raise ImportError(name)

    missing = startup.check_libraries(importer)
    assert (missing.status, missing.summary) == ("FAIL", "missing: pystray, Pillow")
    assert "pip install -r requirements.txt" in missing.details[0]
    assert startup.check_libraries(lambda name: None) == Check("Libraries", "OK", "all in place")
    assert startup.check_libraries().status == "OK"           # the real ones are installed here


def test_libraries_installed_from_an_older_requirements_file_fail_the_check():
    behind = startup.check_libraries(lambda name: None, stale=lambda: True)
    assert (behind.status, behind.summary) == ("FAIL", "requirements.txt changed since the last install")
    assert "pip install -r requirements.txt" in behind.details[0]
    assert startup.check_libraries(lambda name: None, stale=lambda: False).status == "OK"

    def importer(name):
        raise ImportError(name)

    assert startup.check_libraries(importer, stale=lambda: True).summary.startswith("missing: ")   # the nearer trouble


def test_every_library_the_program_imports_is_checked_and_listed_for_pip():
    root = Path(__file__).resolve().parents[2]
    wanted = {line.split(">=")[0].strip().lower() for line in (root / "requirements.txt").read_text().split()
              if line.strip()}
    checked = {package.lower() for _, package in startup.LIBRARIES}
    assert checked == wanted - {"pytest"}                     # pytest is for the tests, not for the program


def test_the_data_folder_must_take_a_file(monkeypatch):
    assert startup.check_data() == Check("Data folder", "OK", str(paths.home()))
    assert list(paths.home().glob(".startup-probe")) == []

    def no_home():
        raise PermissionError(13, "the disk is read-only")

    monkeypatch.setattr(startup.paths, "home", no_home)
    refused = startup.check_data()
    assert refused.status == "FAIL" and "read-only" in refused.details[0]


def test_the_settings_line_says_what_the_program_is_going_to_do():
    fresh = startup.check_settings()
    assert fresh.status == "OK" and fresh.summary.startswith("no file yet - recording is off")
    paths.config_file().write_text(json.dumps({"recording": {"enabled": True}}), encoding="utf-8")
    assert "no microphone chosen" in startup.check_settings().summary
    paths.config_file().write_text(json.dumps({"recording": {"enabled": True, "microphone": "Headset"}}),
                                   encoding="utf-8")
    assert startup.check_settings() == Check("Settings", "OK", "recording conversations on Headset")


def test_settings_with_a_mistake_are_a_warning_that_names_it():
    paths.config_file().write_text(json.dumps({"recording": {"poll_seconds": 99}}), encoding="utf-8")
    bad = startup.check_settings()
    assert bad.status == "WARN" and bad.details == ("recording.poll_seconds: must be 0.2..10, default kept",)
    paths.config_file().write_text("{ not json", encoding="utf-8")
    unreadable = startup.check_settings()
    assert unreadable.status == "WARN" and "nothing will be recorded" in unreadable.summary


def test_a_line_of_the_window():
    assert startup.format_line(2, Check("Libraries", "OK", "all in place")) == \
        "[2/5] Libraries ......... OK    all in place"
    lines = startup.format_line(4, Check("Settings", "WARN", "x", ("first", "second"))).split("\n")
    assert lines[1:] == ["        first", "        second"]


# --- the program that may be running already


def test_the_program_is_running_while_something_holds_its_lock():
    assert startup.running() is False
    held = entry.single_instance()
    try:
        assert startup.running() is True
    finally:
        held.close()
    assert startup.running() is False
    assert entry.single_instance() is not None                # asking did not take the lock away


def test_the_process_number_is_believed_only_if_it_is_one():
    assert startup.read_pid() is None
    for text, pid in (("4242", 4242), (" 17\n", 17), ("", None), ("zero", None), ("0", None), ("-5", None)):
        paths.pid_file().write_text(text, encoding="utf-8")
        assert startup.read_pid() == pid, text


def stopping(alive_for: int, pid, image: str = "pythonw.exe"):
    """A world for stop_running(): the lock stays held for `alive_for` more looks after the kill;
    `image` is the program that runs under the number now."""
    world = SimpleNamespace(said=[], killed=[], looks=0, now=0.0)

    def is_running():
        world.looks += 1
        return not world.killed or world.looks <= alive_for

    def sleep(seconds):
        world.now += seconds

    world.run = lambda: startup.stop_running(world.said.append, is_running=is_running, pid=lambda: pid,
                                             terminate=world.killed.append, image=lambda number: image,
                                             sleep=sleep, clock=lambda: world.now)
    return world


def test_the_running_program_is_ended_and_the_free_lock_is_the_proof():
    world = stopping(alive_for=3, pid=4242)
    assert world.run() is True
    assert world.killed == [4242] and world.said == ["  stopped"]


def test_a_program_that_never_said_which_process_it_is_is_not_guessed_at():
    world = stopping(alive_for=0, pid=None)
    assert world.run() is False
    assert world.killed == [] and "did not say which process" in world.said[0]


def test_a_process_that_is_not_python_any_more_is_never_ended():
    # The program died without taking its file along, another one holds the lock now without
    # having named itself, and Windows gave the old number to something else.
    for image in ("notepad.exe", ""):                          # "" - the process cannot even be asked
        world = stopping(alive_for=10**9, pid=4242, image=image)
        assert world.run() is False
        assert world.killed == [] and "is not this program" in world.said[0]
    for image in ("pythonw.exe", "Python.exe"):
        assert stopping(alive_for=0, pid=4242, image=image).run() is True


def test_the_program_under_a_number_is_asked_of_windows():
    import os

    assert startup.image_of(os.getpid()).lower() in ("python.exe", "pythonw.exe")
    assert startup.image_of(0) == ""


def test_a_program_that_will_not_die_is_said_so():
    world = stopping(alive_for=10**9, pid=4242)
    assert world.run() is False
    assert world.said == ["  the running program did not stop"]
    assert world.now >= 5.0                                    # it was given its five seconds


def test_with_nothing_running_there_is_nothing_to_stop():
    said, killed = [], []
    assert startup.stop_running(said.append, is_running=lambda: False, pid=lambda: 4242,
                                terminate=killed.append) is True
    assert said == [] and killed == []


# --- the questions


def test_a_question_is_answered_no_when_nobody_is_there_to_answer():
    assert startup.ask_yes("? ", isatty=False) is False
    assert startup.ask_yes("? ", default=False, isatty=False) is False


def test_enter_means_yes_only_where_yes_ends_nothing():
    assert startup.ask_yes("? ", input_fn=lambda prompt: "", isatty=True) is True
    assert startup.ask_yes("? ", default=False, input_fn=lambda prompt: "", isatty=True) is False
    for word in ("y", "YES", " д ", "да"):
        assert startup.ask_yes("? ", default=False, input_fn=lambda prompt, word=word: word, isatty=True) is True
    assert startup.ask_yes("? ", input_fn=lambda prompt: "n", isatty=True) is False


# --- the update door: python -m protocallist.tray.startup --install-requirements


class Door:
    """A world for install_requirements(): the program that opened the window leaves after
    `leaves_after` looks at the lock - or only when it is stopped."""

    def __init__(self, leaves_after=0, stops=True, pip_ok=True) -> None:
        self.looks_left, self.stops, self.pip_ok = leaves_after, stops, pip_ok
        self.now = 0.0
        self.calls: list[str] = []
        self.said: list[str] = []

    def running(self) -> bool:
        if self.looks_left > 0:
            self.looks_left -= 1
            return True
        return False

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def stop(self, say) -> bool:
        self.calls.append("stop")
        self.looks_left = 0 if self.stops else self.looks_left
        return self.stops

    def pip(self, root) -> bool:
        self.calls.append("pip")
        return self.pip_ok

    def run(self) -> bool:
        return startup.install_requirements(Path("."), self.said.append, is_running=self.running, stop=self.stop,
                                            pip=self.pip, mark=lambda root: self.calls.append("record"),
                                            sleep=self.sleep, clock=lambda: self.now)


def test_the_libraries_are_installed_only_after_the_program_has_left():
    door = Door(leaves_after=3)
    assert door.run() is True
    assert door.calls == ["pip", "record"] and door.said == ["Installing the libraries from requirements.txt..."]
    assert 0 < door.now < 10.0                                 # it left by itself and was waited for


def test_a_program_that_does_not_leave_by_itself_is_stopped_before_pip():
    door = Door(leaves_after=10**6)
    assert door.run() is True
    assert door.calls == ["stop", "pip", "record"]
    assert door.now >= 10.0                                    # it was given its ten seconds first


def test_pip_is_not_run_over_a_program_that_cannot_be_stopped():
    door = Door(leaves_after=10**6, stops=False)
    assert door.run() is False
    assert door.calls == ["stop"]


def test_a_failed_install_leaves_no_record_so_that_it_is_offered_again():
    door = Door(pip_ok=False)
    assert door.run() is False
    assert door.calls == ["pip"]


def test_the_door_installs_with_the_real_parts_when_none_are_given(monkeypatch, libraries_record):
    done = []
    monkeypatch.setattr(startup, "running", lambda: False)
    monkeypatch.setattr(startup, "pip_install", lambda root: done.append("pip") or True)
    root = Path(__file__).resolve().parents[2]
    assert startup.install_requirements(root, done.append) is True
    assert done == ["Installing the libraries from requirements.txt...", "pip"]
    assert updater.installed_digest() == updater.requirements_digest(root)      # the record was made


# --- the start itself


def test_the_program_is_started_without_a_window_and_let_go_of(monkeypatch, nothing_real_is_shown_or_started):
    started = []
    monkeypatch.setattr(startup.subprocess, "Popen", lambda command, **how: started.append((command, how)))
    nothing_real_is_shown_or_started.spawn(Path("C:/protocallist"))
    (command, how), = started
    assert Path(command[0]).name == "pythonw.exe" and command[1:] == ["-m", "protocallist"]
    assert Path(command[0]).parent == Path(sys.executable).parent       # the Python of this installation
    assert how["cwd"] == str(Path("C:/protocallist")) and how["env"]["PYTHONPATH"] == str(Path("C:/protocallist"))
    assert how["creationflags"] == subprocess.CREATE_NO_WINDOW
    # nothing of this window is held on to: the program lives on when the window closes
    assert how["stdin"] == how["stdout"] == subprocess.DEVNULL


def test_the_libraries_are_installed_by_the_pip_of_this_python(monkeypatch, nothing_real_is_shown_or_started):
    ran = []

    def run(command, **how):
        ran.append((command, how))
        return SimpleNamespace(returncode=len(ran) - 1)        # the first run is fine, the second is not

    monkeypatch.setattr(startup.subprocess, "run", run)
    root = Path("C:/protocallist")
    assert nothing_real_is_shown_or_started.pip_install(root) is True
    assert nothing_real_is_shown_or_started.pip_install(root) is False
    command, how = ran[0]
    assert command == [sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt")]
    assert how == {"cwd": str(root)}                           # its output is left on the screen, not caught

    def no_pip(command, **how):
        raise OSError("no such program")

    monkeypatch.setattr(startup.subprocess, "run", no_pip)
    assert nothing_real_is_shown_or_started.pip_install(root) is False


# --- the whole walk


class Walk:
    """main() with everything outside it faked: what the window printed and what was done."""

    def __init__(self, monkeypatch, capsys, *, running=(False, True), answers=(), libraries=None, pip=True,
                 stopped=True, argv=(), door=True) -> None:
        self.capsys, self.done, self.enter_means = capsys, [], []
        states, replies = list(running), list(answers)
        monkeypatch.setattr(startup, "running", lambda: states.pop(0) if len(states) > 1 else states[0])
        monkeypatch.setattr(startup, "spawn", lambda root: self.done.append("spawn"))
        monkeypatch.setattr(startup, "pip_install", lambda root: self.done.append("pip") or pip)
        monkeypatch.setattr(startup, "stop_running", lambda say: self.done.append("stop") or stopped)
        monkeypatch.setattr(startup, "install_requirements", lambda root, say: self.done.append("door") or door)

        def ask(prompt, default=True):
            self.done.append("ask")
            self.enter_means.append(default)
            return replies.pop(0)

        monkeypatch.setattr(startup, "ask_yes", ask)
        monkeypatch.setattr(startup, "hold", lambda reason: self.done.append(f"hold: {reason}"))
        monkeypatch.setattr(startup, "START_WAIT_S", 0.0)     # the faked program is up at once, or never
        if libraries is not None:
            results = list(libraries)
            monkeypatch.setattr(startup, "check_libraries", lambda stale=None: results.pop(0))
        self.code = startup.main(list(argv))
        self.out = capsys.readouterr().out


def test_a_healthy_start_prints_five_lines_starts_the_program_and_closes(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys)
    assert walk.code == 0 and walk.done == ["spawn"]
    lines = walk.out.splitlines()
    assert lines[0] == f"Protocallist {VERSION}"
    assert [line[:5] for line in lines[1:]] == ["[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]"]
    assert "started - look for its icon in the tray" in lines[-1]
    log = (paths.logs_dir() / "startup.log").read_text(encoding="utf-8")
    assert "=== protocallist.cmd" in log and lines[-1] in log   # the same lines, kept


def test_missing_libraries_are_installed_on_a_yes_and_checked_again(monkeypatch, capsys):
    missing, fine = Check("Libraries", "FAIL", "missing: pystray"), Check("Libraries", "OK", "all in place")
    walk = Walk(monkeypatch, capsys, libraries=[missing, fine], answers=[True])
    assert walk.code == 0 and walk.done == ["ask", "pip", "spawn"]
    assert walk.enter_means == [True]                          # Enter installs: that is what the window is for
    assert updater.installed_digest() is not None              # which requirements.txt they came from is recorded
    assert "Installing the libraries" in walk.out


def test_without_the_libraries_nothing_is_started_and_the_window_stays(monkeypatch, capsys):
    missing = Check("Libraries", "FAIL", "missing: pystray")
    walk = Walk(monkeypatch, capsys, libraries=[missing], answers=[False])
    assert walk.code == 1
    assert walk.done == ["ask", "hold: Protocallist was NOT started"]
    assert updater.installed_digest() is None                  # a walk that failed records nothing


def test_an_install_that_failed_is_not_recorded_and_nothing_is_started(monkeypatch, capsys):
    missing = Check("Libraries", "FAIL", "missing: pystray")
    walk = Walk(monkeypatch, capsys, libraries=[missing], answers=[True], pip=False)
    assert walk.code == 1 and walk.done == ["ask", "pip", "hold: Protocallist was NOT started"]
    assert updater.installed_digest() is None


def test_an_install_without_a_record_gets_one_after_a_clean_walk(monkeypatch, capsys, libraries_record):
    assert updater.installed_digest() is None
    Walk(monkeypatch, capsys)
    assert libraries_record.exists()
    assert updater.installed_digest() == updater.requirements_digest(Path(__file__).resolve().parents[2])


def test_a_record_that_is_there_is_left_as_it_is_by_a_clean_walk(monkeypatch, capsys, libraries_record):
    # Libraries that are behind requirements.txt are the business of the check, not of a
    # walk that happens to pass: writing the record here would hide them for ever.
    libraries_record.write_text("the digest of some older requirements.txt\n", encoding="ascii")
    fine = Check("Libraries", "OK", "all in place")
    Walk(monkeypatch, capsys, libraries=[fine])
    assert updater.installed_digest() == "the digest of some older requirements.txt"


def test_libraries_behind_requirements_are_offered_for_install_like_missing_ones(monkeypatch, capsys,
                                                                                 libraries_record):
    libraries_record.write_text("the digest of some older requirements.txt\n", encoding="ascii")
    monkeypatch.setattr(startup.importlib, "import_module", lambda name: None)
    walk = Walk(monkeypatch, capsys, answers=[True])
    assert walk.code == 0 and walk.done == ["ask", "pip", "spawn"]
    assert "requirements.txt changed since the last install" in walk.out
    assert updater.libraries_stale(Path(__file__).resolve().parents[2]) is False       # recorded anew


def test_the_update_door_installs_first_and_asks_nothing(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, argv=["--install-requirements"])
    assert walk.code == 0 and walk.done == ["door", "spawn"]
    assert "Update: installing the libraries" in walk.out


def test_a_door_that_could_not_install_starts_nothing_and_keeps_the_window(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, argv=["--install-requirements"], door=False)
    assert walk.code == 1 and len(walk.done) == 2 and walk.done[0] == "door"
    assert walk.done[1].startswith("hold: The libraries were not installed - Protocallist was NOT started")


def test_without_the_door_nothing_is_installed_unasked(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys)
    assert "door" not in walk.done and "pip" not in walk.done


def test_a_program_that_runs_already_is_left_alone_unless_told_otherwise(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, running=(True,), answers=[False])
    assert walk.code == 0 and walk.done == ["ask"]
    assert "running already" in walk.out and "left running" in walk.out
    assert walk.enter_means == [False]                         # Enter must not cut a conversation in two


def test_on_a_yes_the_running_program_is_stopped_and_started_anew(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, running=(True, False, True), answers=[True])
    assert walk.code == 0 and walk.done == ["ask", "stop", "spawn"]


def test_a_program_that_would_not_stop_is_not_started_twice(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, running=(True,), answers=[True], stopped=False)
    assert walk.code == 1 and walk.done == ["ask", "stop", "hold: Protocallist was NOT restarted"]


def test_a_program_that_did_not_come_up_is_said_with_where_to_look(monkeypatch, capsys):
    walk = Walk(monkeypatch, capsys, running=(False,))
    assert walk.code == 1 and walk.done == ["spawn", "hold: Protocallist was NOT started"]
    assert "did not start" in walk.out and "protocallist.log" in walk.out


def test_what_a_program_said_before_it_died_is_shown_in_the_window(monkeypatch, capsys):
    # A windowless Python has nowhere to say "SyntaxError": what it says goes to a file, and
    # the window reads the end of it.
    (paths.logs_dir() / "stderr.log").write_text(
        "Traceback (most recent call last):\n" + "  File ...\n" * 20 + "SyntaxError: invalid syntax\n", encoding="utf-8")
    walk = Walk(monkeypatch, capsys, running=(False,))
    assert walk.code == 1
    assert "SyntaxError: invalid syntax" in walk.out and "stderr.log" in walk.out
    assert walk.out.count("File ...") < 20                     # the end of it, not all of it


def test_the_program_is_started_with_a_file_to_say_its_last_words_into(monkeypatch, nothing_real_is_shown_or_started):
    (paths.logs_dir() / "stderr.log").write_text("what an earlier start said\n", encoding="utf-8")
    started = []
    monkeypatch.setattr(startup.subprocess, "Popen", lambda command, **how: started.append(how))
    nothing_real_is_shown_or_started.spawn(Path("C:/protocallist"))
    assert Path(started[0]["stderr"].name) == paths.logs_dir() / "stderr.log"
    assert started[0]["stderr"].closed                         # the window keeps no hand on it
    assert (paths.logs_dir() / "stderr.log").read_text(encoding="utf-8") == ""  # this start, not an earlier one


def test_a_warning_keeps_the_window_open_after_the_start(monkeypatch, capsys):
    paths.config_file().write_text("{ not json", encoding="utf-8")
    walk = Walk(monkeypatch, capsys)
    assert walk.code == 0 and walk.done == ["spawn", "hold: There are warnings above"]


def test_data_held_by_the_program_of_before_is_said_in_the_window_and_nothing_is_started(monkeypatch, capsys, tmp_path):
    """protocallist.cmd clicked while the program still runs under its name of before: its folder
    cannot be moved, and the settings in it are not looked for - there is no folder to look in."""
    old = tmp_path / ".protokolist"
    old.mkdir()
    (old / "protokolist.db").write_text("db", encoding="utf-8")

    def held(self, target):
        raise PermissionError(13, "the folder is in use")

    monkeypatch.delenv(paths.ENV)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths, "_adopted", False)
    monkeypatch.setattr(Path, "rename", held)
    walk = Walk(monkeypatch, capsys)
    assert walk.code == 1 and walk.done == ["hold: Protocallist was NOT started"]
    lines = walk.out.splitlines()
    assert [line[:5] for line in lines[1:4]] == ["[1/5]", "[2/5]", "[3/5]"] and len(lines) == 5
    assert lines[3] == "[3/5] Data folder ....... FAIL  held by the program under its name of before"
    assert str(old) in lines[4] and "end it (Exit in the menu of its icon) and start this one again" in lines[4]
    assert not (tmp_path / ".protocallist").exists() and (old / "protokolist.db").read_text(encoding="utf-8") == "db"


def test_a_data_folder_that_takes_no_file_ends_the_walk_at_its_line(monkeypatch, capsys):
    def no_home():
        raise PermissionError(13, "the disk is read-only")

    monkeypatch.setattr(startup.paths, "home", no_home)
    walk = Walk(monkeypatch, capsys)
    assert walk.code == 1 and walk.done == ["hold: Protocallist was NOT started"]
    assert walk.out.splitlines()[3] == "[3/5] Data folder ....... FAIL  cannot be written to"
    assert "[4/5]" not in walk.out and "read-only" in walk.out


def test_the_window_holds_itself_so_protocallist_cmd_has_nothing_to_hold(monkeypatch):
    # protocallist.cmd says `|| pause`: that is for a Python that blew up. A walk that failed has
    # asked for its Enter already - a code other than 0 would make the window ask a second time.
    walked = []
    monkeypatch.setattr(startup, "main", lambda argv=None: walked.append(argv) or 1)
    assert startup.run(["--install-requirements"]) == 0
    assert walked == [["--install-requirements"]]
