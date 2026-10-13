"""The icon and the panel: one row of the menu, and a plain click on the icon, open it in the browser - whatever
it takes: a panel that is not served is started first, and only when that cannot be done is the user told, in a
window that answers their click."""
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from protocallist import paths
from protocallist.tray import app as tray_app

from .test_app import FakeApp, quiet, rows  # noqa: F401  (the fixture)

SECRET = "the-secret-of-this-test-0123456789abcdef"
ROW = "Открыть панель"
OWN = [40701, 41701, 42701, 43701, 44701]       # the ports of the account of these tests


class FakePanel:
    """Stands in for the server of the panel: remembers what was done with it, and in which order."""

    def __init__(self, app, port: int, world: dict) -> None:
        self.app, self.port, self.world = app, port, world

    def start(self) -> None:
        self.world["tried"].append(self.port)
        self.world["starting"]()
        if self.port in self.world["taken"]:
            raise OSError(10048, "Only one usage of each socket address is normally permitted")
        self.world["did"].append("panel started")

    def stop(self) -> None:
        self.world["did"].append("panel stopped")
        self.world["told at the stop"] = list(self.app.told)

    def link(self) -> str:
        return f"http://127.0.0.1:{self.port}/?key={SECRET}"


@pytest.fixture
def world(quiet, monkeypatch):  # noqa: F811
    """What the browser was asked to open, which ports are taken, and what the user was asked and answered."""
    seen = {"did": [], "tried": [], "taken": set(), "opened": [], "asked": [], "answers": [], "broken": None,
            "starting": lambda: None}

    def make_panel(app, port):
        if seen["broken"] is not None:
            raise seen["broken"]
        return FakePanel(app, port, seen)

    def retry(text: str) -> bool:
        seen["asked"].append(text)
        return seen["answers"].pop(0) if seen["answers"] else False

    seen["make_panel"], seen["retry"] = make_panel, retry
    monkeypatch.setattr(tray_app.webbrowser, "open", seen["opened"].append)
    monkeypatch.setattr(tray_app.ports, "candidates", lambda: list(OWN))
    monkeypatch.setattr(tray_app.ports, "holder", lambda port: "node.exe")
    return seen


def up(world, settings: str | None = None):
    """The program started: the icon is there, and so is whatever of the panel could be started."""
    if settings is not None:
        paths.config_file().write_text(settings, encoding="utf-8")
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe", world["make_panel"], retry=world["retry"])
    made._setup(made.icon)                       # what the icon does once it exists
    return made


def click_the_icon(tray) -> None:
    """A plain click: the icon library calls its menu, and the menu the row marked as the main one."""
    before = threading.active_count()
    tray.icon.menu(tray.icon)
    deadline = time.monotonic() + 2
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.005)


def port_in_the_settings() -> int | None:
    if not paths.config_file().exists():
        return None
    return json.loads(paths.config_file().read_text(encoding="utf-8")).get("panel", {}).get("port")


def test_the_first_row_of_the_menu_and_a_plain_click_open_the_panel_in_the_browser(world):
    tray = up(world)
    assert rows(tray)[:2] == [ROW, "Записывать разговоры"]
    assert [item.text for item in tray.icon.menu.items if item.default] == [ROW]
    click_the_icon(tray)
    assert world["opened"] == [f"http://127.0.0.1:40701/?key={SECRET}"] and world["asked"] == []
    assert world["tried"] == [40701]                                     # the panel that is served is the one opened


def test_at_the_first_start_the_account_takes_its_own_port_and_it_is_written_into_the_settings(world):
    tray = up(world)
    assert world["tried"] == [40701] and port_in_the_settings() == 40701
    assert tray._app.told == ["start"]
    again = up(world)                                                    # the next start: nothing is chosen any more
    assert world["tried"] == [40701, 40701] and again._port == 40701


def test_the_port_is_written_into_the_settings_without_touching_anything_else_there(world):
    up(world, '{"storage": {"keep_audio_days": "7"}, "note": "mine"}')    # a slip of the user's, and a note of their own
    assert json.loads(paths.config_file().read_text(encoding="utf-8")) == {
        "storage": {"keep_audio_days": "7"}, "note": "mine", "panel": {"port": 40701}}


def test_a_port_that_could_not_be_written_down_is_no_reason_to_say_the_panel_did_not_start(world, monkeypatch, caplog):
    def held(*what):
        raise PermissionError(13, "the settings are held by another program")

    monkeypatch.setattr(tray_app.config, "put", held)
    with caplog.at_level(logging.INFO, logger="protocallist"):
        tray = up(world)
        click_the_icon(tray)
    assert world["tried"] == [40701] and world["asked"] == [] and world["opened"] == [f"http://127.0.0.1:40701/?key={SECRET}"]
    assert "it could not be written into the settings (PermissionError)" in caplog.text
    assert "it is written into the settings" not in caplog.text and "could not be started" not in caplog.text


def test_if_its_own_port_is_taken_at_the_first_start_the_same_one_a_thousand_higher_is_tried(world, caplog):
    world["taken"] = {40701, 41701}
    with caplog.at_level(logging.INFO, logger="protocallist"):
        up(world)
    assert world["tried"] == [40701, 41701, 42701] and port_in_the_settings() == 42701
    assert "the panel's port is 42701" in caplog.text and world["asked"] == []         # nobody is asked anything at a start


def test_a_port_named_in_the_settings_is_the_only_one_that_is_tried(world):
    world["taken"] = {9123}
    tray = up(world, '{"panel": {"port": 9123}}')
    assert world["tried"] == [9123] and port_in_the_settings() == 9123 and world["asked"] == []
    assert rows(tray)[0] == ROW                                          # the row is there all the same
    assert tray._app.told == ["start"]                                   # and recording goes on without the panel


def test_a_click_starts_a_panel_that_is_not_served_and_then_opens_it(world):
    world["taken"] = {9123}
    tray = up(world, '{"panel": {"port": 9123}}')
    world["taken"] = set()                                               # what held the port has let go of it
    click_the_icon(tray)
    assert world["tried"] == [9123, 9123] and world["asked"] == []       # the user learns nothing of the hitch
    assert world["opened"] == [f"http://127.0.0.1:9123/?key={SECRET}"]


def test_only_when_the_panel_cannot_be_started_is_the_user_told_and_asked_whether_to_try_again(world, caplog):
    world["taken"] = {9123}
    tray = up(world, '{"panel": {"port": 9123}}')
    world["answers"] = [True, False]                                     # "Повтор", then "Отмена"
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        click_the_icon(tray)
    assert world["tried"] == [9123, 9123, 9123] and world["opened"] == []
    assert "tray menu action failed" not in caplog.text                  # "Отмена" is an answer, not a failure
    assert len(world["asked"]) == 2 and world["asked"][0].startswith(
        "Панель не запустилась: порт 9123 занят программой node.exe. Закройте её и нажмите «Повтор».")
    assert str(paths.logs_dir()) in world["asked"][0]
    assert "the panel could not be started on port 9123 (OSError" in caplog.text and "node.exe" in caplog.text
    world["taken"], world["answers"] = set(), [True]
    click_the_icon(tray)                                                 # asked once more - and by then the port is free
    assert world["opened"] == [f"http://127.0.0.1:9123/?key={SECRET}"]


def test_when_every_port_of_the_account_is_taken_its_own_one_is_named(world):
    world["taken"] = set(OWN)
    tray = up(world)
    assert world["tried"] == OWN and port_in_the_settings() is None      # nothing is chosen: the next start tries again
    click_the_icon(tray)
    assert world["tried"] == OWN + OWN
    assert world["asked"][0].startswith("Панель не запустилась: порт 40701 занят программой node.exe.")


def test_a_port_whose_holder_windows_does_not_name_is_said_without_a_name(world, monkeypatch):
    monkeypatch.setattr(tray_app.ports, "holder", lambda port: "")
    world["taken"] = {9123}
    tray = up(world, '{"panel": {"port": 9123}}')
    click_the_icon(tray)
    assert world["asked"][0].startswith("Панель не запустилась: порт 9123 занят. Освободите его и нажмите «Повтор».")


def test_a_panel_that_cannot_even_be_made_is_said_too(world, caplog):
    world["broken"] = PermissionError(13, "the key of the panel cannot be written")
    with caplog.at_level(logging.INFO, logger="protocallist"):
        tray = up(world, '{"panel": {"port": 9123}}')
        click_the_icon(tray)
    assert world["asked"] == [f"Панель не запустилась.\n\nПодробности — в журнале: {paths.logs_dir()}"]
    assert "the panel could not be started" in caplog.text and "Traceback" in caplog.text and tray._app.told == ["start"]


def test_what_is_said_is_about_the_last_attempt(world):
    world["taken"] = {9123}
    tray = up(world, '{"panel": {"port": 9123}}')                        # the port was taken at the start...
    world["broken"] = PermissionError(13, "the key of the panel cannot be written")
    click_the_icon(tray)                                                 # ...and by the click something else is wrong
    assert world["asked"] == [f"Панель не запустилась.\n\nПодробности — в журнале: {paths.logs_dir()}"]


def test_the_window_follows_the_language(world):
    world["taken"] = {9123}
    tray = up(world, '{"language": "en", "panel": {"port": 9123}}')
    assert rows(tray)[0] == "Open the panel"
    click_the_icon(tray)
    assert world["asked"][0].startswith("The panel did not start: port 9123 is taken by node.exe. Close it and press Retry.")


def test_settings_that_cannot_be_read_are_not_written_over_with_a_port(world):
    broken = '{"recording": {'
    tray = up(world, broken)
    assert world["tried"] == [40701] and paths.config_file().read_text(encoding="utf-8") == broken
    click_the_icon(tray)
    assert world["opened"] == [f"http://127.0.0.1:40701/?key={SECRET}"]


def test_a_click_while_the_panel_is_being_started_starts_no_second_one(world):
    begun, go_on = threading.Event(), threading.Event()
    world["starting"] = lambda: (begun.set(), go_on.wait(5))
    tray = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe", world["make_panel"], retry=world["retry"])
    start = threading.Thread(target=tray._setup, args=(tray.icon,))
    start.start()
    assert begun.wait(5)
    tray.icon.menu(tray.icon)                                            # the user is quicker than the program
    assert not go_on.wait(0.3)
    go_on.set()
    start.join(5)
    assert world["tried"] == [40701] and world["opened"] == [] and world["asked"] == []
    click_the_icon(tray)                                                 # and once it is there, a click opens it
    assert world["tried"] == [40701] and world["opened"] == [f"http://127.0.0.1:40701/?key={SECRET}"]


def test_an_icon_made_without_a_panel_has_no_row_for_it(quiet):  # noqa: F811
    tray = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    tray._setup(tray.icon)
    assert rows(tray)[0] == "Записывать разговоры"
    click_the_icon(tray)                                                 # and a click on it does nothing


def test_the_panel_is_stopped_with_the_icon_before_the_service_is(world):
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe", world["make_panel"], retry=world["retry"])
    made.run()                                                           # the faked icon sets up and returns at once
    assert world["did"] == ["panel started", "panel stopped"]
    assert world["told at the stop"] == ["start"] and made._app.told == ["start", "stop"]   # the archive was still open


def test_the_key_of_the_panel_never_goes_to_the_log(world, caplog):
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        tray = up(world)
        click_the_icon(tray)
    assert world["opened"] and SECRET not in caplog.text


def test_the_window_is_one_of_windows_own_with_retry_and_cancel(quiet, monkeypatch):  # noqa: F811
    shown, pressed = [], [4, 2]                                           # what Windows answers: "Повтор", then "Отмена"

    def box(owner, text, title, kind):
        shown.append((owner, text, title, kind))
        return pressed.pop(0)

    monkeypatch.setattr(tray_app.ctypes.windll.user32, "MessageBoxW", box)
    assert tray_app.retry_or_cancel("Панель не запустилась.") is True
    assert tray_app.retry_or_cancel("Панель не запустилась.") is False
    owner, text, title, kind = shown[0]
    assert (owner, text, title) == (None, "Панель не запустилась.", "Протоколист")
    assert kind & 0xF == 0x5 and kind & 0x10000 and kind & 0x40000        # "Повтор" and "Отмена"; in front, and it stays there
