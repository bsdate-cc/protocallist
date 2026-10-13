"""Handing the program over to a new process: the successor waits for the old one to leave."""
import sys
from pathlib import Path

from protocallist import paths
from protocallist.tray import relaunch, startup


def test_the_successor_waits_until_the_old_program_is_gone():
    answers = [True, True, False]
    slept = []
    assert relaunch.wait_until_free(lambda: answers.pop(0), slept.append, timeout=20.0) is True
    assert slept == [relaunch.STEP_S, relaunch.STEP_S]


def test_the_successor_gives_up_on_an_old_program_that_never_leaves():
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    assert relaunch.wait_until_free(lambda: True, sleep, timeout=5.0, clock=lambda: clock[0]) is False
    assert 5.0 <= clock[0] < 6.0


def test_the_successor_becomes_the_program_once_the_lock_is_free(monkeypatch):
    started = []
    monkeypatch.setattr(startup, "running", lambda: False)
    relaunch.main(start=lambda: started.append(1))
    assert started == [1]


def test_code_on_disk_that_cannot_be_imported_is_said_in_the_log(monkeypatch):
    # After a merge that left a mistake in the code the old program is gone and the new one
    # cannot even be imported: under pythonw that would be no icon and no word anywhere.
    monkeypatch.setattr(startup, "running", lambda: False)
    monkeypatch.setitem(sys.modules, "protocallist.__main__", None)      # importing it fails
    relaunch.main()                                                     # ...and that is not left to a traceback
    said = (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")
    assert "the code on disk cannot be imported" in said and "Traceback" in said


def test_an_old_program_that_hangs_while_leaving_is_ended_and_the_successor_starts(monkeypatch, caplog):
    # A sound device that will not close: the icon is gone already, the lock is still held.
    world = {"alive": True, "ended": []}

    def end(pid):
        world["ended"].append(pid)
        world["alive"] = False

    paths.pid_file().write_text("4242", encoding="utf-8")
    monkeypatch.setattr(startup, "running", lambda: world["alive"])
    monkeypatch.setattr(startup, "image_of", lambda pid: "pythonw.exe")
    monkeypatch.setattr(startup, "_terminate", end)
    monkeypatch.setattr(relaunch, "WAIT_S", 0.3)
    started = []
    with caplog.at_level("WARNING", logger="protocallist"):
        relaunch.main(start=lambda: started.append(1))
    assert world["ended"] == [4242] and started == [1]
    assert "did not exit" in caplog.text                       # and the log says what was done to it


def test_an_old_program_that_cannot_be_ended_is_logged_and_no_second_one_is_started(monkeypatch, caplog):
    # It never said which process it is: nothing is ended by guess, and two programs never run at once.
    started = []
    monkeypatch.setattr(startup, "running", lambda: True)
    monkeypatch.setattr(relaunch, "WAIT_S", 0.3)
    with caplog.at_level("ERROR", logger="protocallist"):
        relaunch.main(start=lambda: started.append(1))
    assert started == []
    assert "could not be ended; no new one was started" in caplog.text


def test_the_two_successors_are_started_the_way_they_are_meant_to_run(monkeypatch, tmp_path,
                                                                     nothing_real_is_shown_or_started):
    calls = []
    monkeypatch.setattr(relaunch.subprocess, "Popen", lambda argv, **how: calls.append((argv, how)))
    # The program runs under pythonw.exe; the launcher window needs the python.exe next to it.
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    for name in ("pythonw.exe", "python.exe"):
        (scripts / name).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "pythonw.exe"))
    nothing_real_is_shown_or_started.successor(tmp_path)
    nothing_real_is_shown_or_started.launcher_window(tmp_path)
    (quiet, quiet_how), (window, window_how) = calls
    assert quiet == [str(scripts / "pythonw.exe"), "-m", "protocallist.tray.relaunch"]
    assert quiet_how["creationflags"] == relaunch.subprocess.CREATE_NO_WINDOW
    assert quiet_how["stdout"] == relaunch.subprocess.DEVNULL       # nothing of the old program is held on to
    # ...but what a windowless Python says before it dies must land somewhere
    assert Path(quiet_how["stderr"].name) == paths.logs_dir() / "stderr.log" and quiet_how["stderr"].closed
    assert window == [str(scripts / "python.exe"), "-m", "protocallist.tray.startup", "--install-requirements"]
    assert window_how["creationflags"] == relaunch.subprocess.CREATE_NEW_CONSOLE
    assert window_how.get("stdout") is None and window_how.get("stderr") is None     # the window is where pip is read
    for how in (quiet_how, window_how):
        assert how["cwd"] == str(tmp_path) and how["env"]["PYTHONPATH"] == str(tmp_path)


def test_without_a_console_interpreter_next_to_it_the_window_runs_the_one_there_is(monkeypatch, tmp_path,
                                                                                  nothing_real_is_shown_or_started):
    calls = []
    monkeypatch.setattr(relaunch.subprocess, "Popen", lambda argv, **how: calls.append(argv))
    alone = tmp_path / "pythonw.exe"
    alone.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(alone))
    nothing_real_is_shown_or_started.launcher_window(tmp_path)
    assert calls[0][0] == str(alone)
