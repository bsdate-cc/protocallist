import json
import os
import sys
from pathlib import Path

import pytest

from protocallist import __main__ as entry
from protocallist import VERSION, config, micholders, paths
from protocallist.service import Service


def configure(**recording) -> None:
    paths.config_file().write_text(json.dumps({"recording": recording}), encoding="utf-8")


def test_the_microphones_are_listed_for_the_settings(monkeypatch, capsys):
    monkeypatch.setattr(micholders, "microphones", lambda: ["Headset Microphone (Poly BT700)", "Webcam"])
    assert entry.main(["--microphones"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Headset Microphone (Poly BT700)", "Webcam"]


# --- in a console window: `python -m protocallist --console`


def test_a_first_start_in_the_console_writes_the_settings_and_records_nothing(capsys):
    assert entry.main(["--console"]) == 2
    said = capsys.readouterr().out
    assert "Recording is off" in said and str(paths.config_file()) in said
    assert json.loads(paths.config_file().read_text(encoding="utf-8"))["recording"]["enabled"] is False
    assert not paths.database_file().exists()


def test_switched_on_without_a_microphone_is_still_off(capsys):
    configure(enabled=True)
    assert entry.main(["--console"]) == 2


def test_only_one_program_runs(capsys):
    configure(enabled=True, microphone="Headset Microphone")
    held = entry.single_instance()
    try:
        assert entry.single_instance() is None
        assert entry.main(["--console"]) == 1
        assert "running already" in capsys.readouterr().out
    finally:
        held.close()


def test_a_database_that_cannot_be_opened_is_said_plainly_and_nothing_is_recorded(capsys):
    configure(enabled=True, microphone="Headset Microphone")
    paths.database_file().write_bytes(b"this is not a database " * 200)
    assert entry.main(["--console"]) == 3
    assert "cannot be opened" in capsys.readouterr().out
    assert "the database cannot be opened" in (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")
    held = entry.single_instance()      # the lock was let go of
    assert held is not None
    held.close()
    os.remove(paths.database_file())    # and so was the file


def test_the_service_runs_until_stopped_and_lets_go_of_everything(monkeypatch, capsys):
    configure(enabled=True, microphone="Headset Microphone", bogus=1)
    ran = []
    monkeypatch.setattr(Service, "run", lambda self, stop: ran.append(self._cfg.microphone))
    assert entry.main(["--console"]) == 0
    assert ran == ["Headset Microphone"]
    assert "watching the microphone" in capsys.readouterr().out
    log = (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")
    assert "recording.bogus" in log and "is watching the microphone Headset Microphone" in log
    held = entry.single_instance()      # the lock was let go of
    assert held is not None
    held.close()
    os.remove(paths.database_file())    # and so was the database


def test_the_service_in_a_console_says_which_process_it_is_too(monkeypatch, capsys):
    # The lock says "a program is running"; the file says which process - so that the launcher
    # window ends this one and not whatever an older file names.
    configure(enabled=True, microphone="Headset Microphone")
    paths.pid_file().write_text("4242", encoding="utf-8")     # left by a program that died without tidying up
    seen = []
    monkeypatch.setattr(Service, "run", lambda self, stop: seen.append(paths.pid_file().read_text(encoding="utf-8")))
    assert entry.main(["--console"]) == 0
    assert seen == [str(os.getpid())]
    assert not paths.pid_file().exists()
    os.remove(paths.database_file())


# --- Ctrl+C stops the service, whoever started it


def test_ctrl_c_is_switched_on_before_the_service_runs(monkeypatch, capsys):
    # A process started by one that ignores Ctrl+C - a launcher, a scheduler - inherits that,
    # and the key the console names as the way to stop would do nothing.
    configure(enabled=True, microphone="Headset Microphone")
    order = []
    monkeypatch.setattr(entry, "heed_ctrl_c", lambda: order.append("ctrl+c heeded"))
    monkeypatch.setattr(Service, "run", lambda self, stop: order.append("run"))
    assert entry.main(["--console"]) == 0
    assert order == ["ctrl+c heeded", "run"]
    os.remove(paths.database_file())


def test_windows_is_told_not_to_ignore_ctrl_c(monkeypatch):
    asked = []

    class Kernel32:
        @staticmethod
        def SetConsoleCtrlHandler(handler, add):
            asked.append((handler, add))
            return 1

    monkeypatch.setattr(entry.ctypes.windll, "kernel32", Kernel32)
    entry.heed_ctrl_c()
    assert asked == [(None, False)]          # no handler of ours, and "ignore" switched off


# --- without arguments: the icon in the tray, the service behind it


class FakeTray:
    """Stands in for the icon: remembers how it was made and what the world looked like while it ran."""
    made: list = []
    fail: Exception | None = None

    def __init__(self, make_app, root, interpreter, make_panel) -> None:
        self.make_app, self.root, self.interpreter, self.make_panel = make_app, root, interpreter, make_panel
        FakeTray.made.append(self)

    def run(self) -> None:
        self.pid_while_running = paths.pid_file().read_text(encoding="utf-8")
        self.second_start = entry.single_instance()
        if FakeTray.fail is not None:
            raise FakeTray.fail


@pytest.fixture
def icon(monkeypatch):
    from protocallist.tray import app as tray_app

    FakeTray.made, FakeTray.fail = [], None
    monkeypatch.setattr(tray_app, "TrayApp", FakeTray)
    return FakeTray


def log_text() -> str:
    return (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")


def test_without_arguments_the_icon_is_put_into_the_tray(icon, capsys):
    assert entry.main([]) == 0
    assert len(icon.made) == 1
    tray = icon.made[0]
    assert tray.root == Path(entry.__file__).resolve().parents[1]
    assert Path(tray.interpreter) == Path(sys.executable).with_name("pythonw.exe")
    assert capsys.readouterr().out == ""                      # there is no window to print to
    assert f"Protocallist {VERSION} starting" in log_text() and "Protocallist stopped" in log_text()


def test_while_the_icon_is_there_the_program_is_the_only_one_and_says_which_process_it_is(icon):
    entry.main([])
    tray = icon.made[0]
    assert tray.pid_while_running == str(os.getpid())
    assert tray.second_start is None                          # the lock was held
    assert not paths.pid_file().exists()                      # and afterwards both are let go of
    held = entry.single_instance()
    assert held is not None
    held.close()


def test_a_second_start_leaves_without_a_word(icon, capsys):
    held = entry.single_instance()
    try:
        assert entry.main([]) == 1
        assert icon.made == [] and capsys.readouterr().out == ""
    finally:
        held.close()


def test_the_successor_of_a_program_that_was_updated_puts_the_icon_into_the_tray(icon, monkeypatch):
    from protocallist.tray import relaunch, startup

    monkeypatch.setattr(startup, "running", lambda: False)    # the old program has left
    relaunch.main()
    assert len(icon.made) == 1 and icon.made[0].pid_while_running == str(os.getpid())


def test_the_entry_that_starts_the_program_with_windows_follows_the_program(icon, registry):
    from protocallist.tray import autostart

    entry.main([])
    assert registry.values == {}                              # not switched on: nothing is written
    autostart.enable(Path("C:/somewhere/else"), "C:/old/pythonw.exe")
    entry.main([])
    assert "somewhere" not in registry.values["Protocallist"]  # rewritten to where the program is now
    assert str(Path(entry.__file__).resolve().parents[1]).replace("\\", "\\\\") in registry.values["Protocallist"]


def test_the_service_behind_the_icon_asks_windows_from_a_thread_it_prepared(icon):
    entry.main([])
    app = icon.made[0].make_app(lambda: None)
    assert app._holders is micholders.holders and app._microphones is micholders.microphones
    assert app._begin is micholders.this_thread and app._end is micholders.leave_thread
    assert app._open_store is entry.open_store and app._make_service is entry.serve


def test_the_service_behind_the_icon_recognises_what_the_archive_holds(icon):
    from protocallist.summaries import Summariser
    from protocallist.transcriber import Transcriber

    entry.main([])
    app = icon.made[0].make_app(lambda: None)
    assert app._make_transcriber is entry.recognise
    assert app._has_archive() is False                              # a new installation has none
    store = entry.open_store()
    try:
        assert app._has_archive() is True
        cfg = config.Config()
        made = entry.recognise(cfg, store, lambda: True)
        assert isinstance(made, Transcriber) and made._store is store and made._cfg is cfg and made._busy() is True
        assert app._make_summariser is entry.summarise
        asked = entry.summarise(cfg, store)
        assert isinstance(asked, Summariser) and asked._store is store and asked._settings is cfg.summary
    finally:
        store.close()


def test_an_icon_that_fails_is_logged_and_everything_is_let_go_of(icon):
    icon.fail = RuntimeError("the shell is gone")
    with pytest.raises(RuntimeError):
        entry.main([])
    assert "Protocallist failed" in log_text() and "the shell is gone" in log_text()
    assert not paths.pid_file().exists()
    held = entry.single_instance()
    assert held is not None
    held.close()


def test_a_missing_library_is_said_in_the_log_with_what_to_do(monkeypatch):
    monkeypatch.setitem(sys.modules, "protocallist.tray.app", None)     # importing it fails, as without pystray
    with pytest.raises(ImportError):
        entry.main([])
    assert "run protocallist.cmd" in log_text()


def test_the_real_service_is_made_from_the_settings_it_is_given(monkeypatch):
    from protocallist.config import RecordingConfig
    from protocallist.store import Store

    store = Store(paths.database_file(), paths.calls_dir())
    try:
        asked = []
        service = entry.serve(RecordingConfig(enabled=True, microphone="Headset Microphone"), store,
                              lambda microphone: asked.append(microphone) or [])
        service.tick()
        assert asked == ["Headset Microphone"] and service.call is None
    finally:
        store.close()


def test_the_icon_is_given_a_panel_over_its_service_with_the_key_of_this_user(icon):
    from protocallist.panel.api import Api
    from protocallist.panel.server import Panel

    entry.main([])
    service = object()
    panel = icon.made[0].make_panel(service, 8791)
    assert isinstance(panel, Panel) and panel.port == 8791
    assert isinstance(panel.api, Api) and panel.api._app is service
    key = paths.key_file().read_text(encoding="ascii")
    assert paths.key_file() == paths.home() / "panel.key" and len(key) >= 32 and panel.secret == key
    again = icon.made[0].make_panel(service, 8791)
    assert again.secret == key                                         # made once, the same at every start
    assert key not in log_text()
