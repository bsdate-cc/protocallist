"""The tray icon: its tooltip, its picture, its menu - on a faked icon and a faked service."""
import json
import threading
import time
from pathlib import Path

import pytest

from protocallist import VERSION, gateway, paths
from protocallist import app as core
from protocallist.app import Glance
from protocallist.tray import app as tray_app
from protocallist.tray import icons


class FakeIcon:
    def __init__(self, name, image, title, menu=None):
        self.name, self.icon, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.menu_updates = 0
        self.stops = 0
        self.ran = threading.Event()

    def update_menu(self):
        self.menu_updates += 1

    def stop(self):
        self.stops += 1

    def run(self, setup=None):
        if setup is not None:
            setup(self)
        self.ran.set()


class FakeApp:
    """Stands in for the service behind the icon: shows the glance it is given, remembers what it was told."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.shown = Glance()
        self.told: list = []

    def glance(self):
        return self.shown

    def start(self):
        self.told.append("start")

    def stop(self):
        self.told.append("stop")

    def set_enabled(self, on):
        self.told.append(("enabled", on))

    def set_microphone(self, name):
        self.told.append(("microphone", name))

    def discard_current(self):
        self.told.append("discard")

    def retry_failed(self):
        self.told.append("retry")


@pytest.fixture
def quiet(monkeypatch):
    """No test runs git against whatever folder it happens to be started in (or its origin),
    reads the real program folder, or leaves a thread behind that watches for updates."""
    monkeypatch.setattr(tray_app.pystray, "Icon", FakeIcon)
    monkeypatch.setattr(tray_app.updater, "status", lambda root: tray_app.updater.Update("off"))
    monkeypatch.setattr(tray_app.updater, "disk_version", lambda root: VERSION)
    monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: "code-at-start")
    monkeypatch.setattr(tray_app.updater, "requirements_digest", lambda root: "digest-at-start")
    monkeypatch.setattr(tray_app.updater, "libraries_stale", lambda root: False)
    monkeypatch.setattr(tray_app.TrayApp, "_start_watching", lambda self: None)


@pytest.fixture
def tray(quiet):
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    made.run()                                   # the faked icon sets up and returns at once
    made._ready = True                           # ...and for the test the icon is still there
    made._stopping.clear()
    made._app.told.clear()
    return made


def rows(tray) -> list[str]:
    return [item.text for item in tray.icon.menu.items if item.visible]


def click(tray, text: str) -> None:
    """Choose a menu row and wait for its action, which runs in a thread of its own."""
    before = threading.active_count()
    item = next(item for item in tray.icon.menu.items if item.visible and item.text == text)
    item(tray.icon)
    deadline = time.monotonic() + 2
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.005)


def show(tray, **glance) -> None:
    tray._app.shown = Glance(**glance)
    tray.refresh()


def test_the_tooltip_names_the_program_its_version_and_what_it_is_doing(tray):
    assert tray.icon.title == f"Протоколист {VERSION}: запись выключена"
    show(tray, state=core.NO_MICROPHONE, enabled=True)
    assert tray.icon.title.endswith(": микрофон не выбран")
    show(tray, state=core.MISSING, enabled=True, microphone="Headset")
    assert tray.icon.title.endswith(": микрофон не найден")
    show(tray, state=core.WAITING, enabled=True, microphone="Headset")
    assert tray.icon.title.endswith(": ждёт разговора")
    show(tray, state=core.RECORDING, program="Zoom", minutes=12, enabled=True, microphone="Headset")
    assert tray.icon.title.endswith(": идёт запись: Zoom, 12 мин")
    show(tray, state=core.ERROR, problem=core.PROBLEM_DATABASE, enabled=True, microphone="Headset")
    assert tray.icon.title.endswith(": ошибка: база не открывается")


def test_the_picture_follows_the_state(tray, monkeypatch):
    drawn = []
    monkeypatch.setattr(tray_app.icons, "image", lambda state: drawn.append(state) or state)
    for state in (core.WAITING, core.RECORDING, core.RECORDING, core.ERROR, core.MISSING, core.NO_MICROPHONE,
                  core.OFF):
        show(tray, state=state, problem=core.PROBLEM_DATABASE)
    # one picture for "nothing is watched", whatever the reason; and none drawn twice in a row
    assert drawn == [icons.WAITING, icons.RECORDING, icons.ERROR, icons.OFF]


def test_the_menu_as_it_is_while_nothing_is_recorded(tray):
    assert rows(tray) == ["Записывать разговоры", "Микрофон для разговоров", "Открыть папку разговоров", "- - - -",
                          "Открыть файл настроек", "Открыть журнал", "Запускать вместе с Windows", "- - - -", "Выход"]


def test_recording_is_switched_on_and_off_from_the_menu(tray):
    click(tray, "Записывать разговоры")
    assert tray._app.told == [("enabled", True)]
    show(tray, state=core.WAITING, enabled=True, microphone="Headset")
    assert next(item for item in tray.icon.menu.items if item.text == "Записывать разговоры").checked is True
    click(tray, "Записывать разговоры")
    assert tray._app.told == [("enabled", True), ("enabled", False)]


def test_a_recording_can_be_thrown_away_only_while_there_is_one(tray):
    assert "Остановить и удалить эту запись" not in rows(tray)
    show(tray, state=core.RECORDING, program="Zoom", enabled=True, microphone="Headset")
    click(tray, "Остановить и удалить эту запись")
    assert tray._app.told == ["discard"]


def test_the_microphones_of_this_computer_are_offered_and_the_chosen_one_is_marked(tray):
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", microphones=("Webcam", "Headset"))
    offered = list(tray._microphones())
    assert [row.text for row in offered] == ["Webcam", "Headset"]
    assert [row.text for row in offered if row.checked] == ["Headset"]
    offered[0](tray.icon)
    deadline = time.monotonic() + 2
    while not tray._app.told and time.monotonic() < deadline:
        time.sleep(0.005)
    assert tray._app.told == [("microphone", "Webcam")]


def test_a_chosen_microphone_that_is_unplugged_stays_in_the_list(tray):
    show(tray, state=core.MISSING, enabled=True, microphone="Headset", microphones=("Webcam",))
    offered = list(tray._microphones())
    assert [row.text for row in offered] == ["Headset", "Webcam"]
    assert [row.text for row in offered if row.checked] == ["Headset"]


def test_a_computer_without_microphones_gets_a_row_that_says_so(tray):
    offered = list(tray._microphones())
    assert [(row.text, row.enabled) for row in offered] == [("микрофонов нет", False)]


def test_the_folders_and_the_settings_file_are_opened_from_the_menu(tray, monkeypatch):
    opened, started = [], []
    monkeypatch.setattr(tray_app.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(tray_app.subprocess, "Popen", started.append)
    click(tray, "Открыть папку разговоров")
    click(tray, "Открыть журнал")
    assert opened == [paths.calls_dir(), paths.logs_dir()]
    assert not paths.config_file().exists()
    click(tray, "Открыть файл настроек")
    assert started == [["notepad.exe", str(paths.config_file())]]
    assert json.loads(paths.config_file().read_text(encoding="utf-8"))["recording"]["enabled"] is False


def test_starting_with_windows_is_switched_from_the_menu(tray, registry):
    click(tray, "Запускать вместе с Windows")
    assert registry.values["Protocallist"].startswith('"C:/python/pythonw.exe" -c ')
    assert "protocallist" in registry.values["Protocallist"]
    assert next(item for item in tray.icon.menu.items if item.text == "Запускать вместе с Windows").checked is True
    click(tray, "Запускать вместе с Windows")
    assert registry.values == {}
    assert next(item for item in tray.icon.menu.items if item.text == "Запускать вместе с Windows").checked is False


def test_the_menu_is_rebuilt_only_when_what_it_shows_changes(tray):
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", microphones=("Headset",))
    before = tray.icon.menu_updates
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", microphones=("Headset",))
    show(tray, state=core.MISSING, enabled=True, microphone="Headset", microphones=("Headset",))
    assert tray.icon.menu_updates == before                   # the same rows, the same marks
    show(tray, state=core.RECORDING, program="Zoom", enabled=True, microphone="Headset", microphones=("Headset",))
    assert tray.icon.menu_updates == before + 1               # the row "stop and delete" came
    show(tray, state=core.RECORDING, program="Zoom", minutes=3, enabled=True, microphone="Headset",
         microphones=("Headset",))
    assert tray.icon.menu_updates == before + 1               # a minute passed: the tooltip changed, the menu did not
    show(tray, state=core.RECORDING, program="Zoom", minutes=3, enabled=True, microphone="Headset",
         microphones=("Headset", "Webcam"))
    assert tray.icon.menu_updates == before + 2               # a microphone was plugged in


def test_the_service_is_started_with_the_icon_and_stopped_when_the_icon_goes(quiet):
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    assert made._app.told == []
    made.run()
    assert made.icon.visible is True
    assert made._app.told == ["start", "stop"]                # a conversation that is going on ends and is kept


def test_exit_stops_the_icon(tray):
    click(tray, "Выход")
    assert tray.icon.stops == 1


def test_a_menu_action_that_fails_is_logged_and_the_icon_lives_on(tray, monkeypatch, caplog):
    def broken(path):
        raise OSError("no such folder")

    monkeypatch.setattr(tray_app.os, "startfile", broken, raising=False)
    with caplog.at_level("ERROR", logger="protocallist"):
        click(tray, "Открыть журнал")
    assert "tray menu action failed" in caplog.text


def test_nothing_changes_on_the_icon_before_it_is_up(quiet):
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    made._app.shown = Glance(state=core.RECORDING, program="Zoom")
    made.refresh()                                            # the service may speak before the icon exists
    assert made.icon.title.endswith("запись выключена") and made.icon.menu_updates == 0


def test_the_tray_speaks_english_when_the_settings_say_so(quiet):
    paths.config_file().write_text(json.dumps({"language": "en"}), encoding="utf-8")
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    made.run()
    made._ready = True
    assert made.icon.title == f"Protocallist {VERSION}: recording is off"
    assert rows(made)[0] == "Record conversations" and rows(made)[-1] == "Exit"
    show(made, state=core.RECORDING, program="Zoom", minutes=2, enabled=True, microphone="Headset")
    assert made.icon.title.endswith(": recording: Zoom, 2 min")
    show(made, state=core.ERROR, problem=core.PROBLEM_DATABASE)
    assert made.icon.title.endswith(": error: the database cannot be opened")


# --- recognition on the icon


def test_the_tooltip_says_what_waits_to_be_recognised_and_why(tray):
    waiting = dict(state=core.WAITING, enabled=True, microphone="Headset")
    show(tray, **waiting, recognising=True, waiting=2)
    assert tray.icon.title.endswith(": ждёт разговора; распознаётся: 1, ждут: 2")
    show(tray, **waiting, waiting=3)
    assert tray.icon.title.endswith(": ждёт разговора; ждут распознавания: 3")
    for kind in (gateway.DOWN, gateway.LOADING):
        show(tray, **waiting, waiting=3, gateway=kind)
        assert tray.icon.title.endswith(": ждёт разговора; ждёт шлюз: 3")
    show(tray, **waiting, waiting=3, gateway=gateway.OLD)
    assert tray.icon.title.endswith(": ждёт разговора; нужен Толмач 0.8.0 или новее, ждут: 3")
    show(tray, **waiting, waiting=1, failed=2)
    assert tray.icon.title.endswith(": ждёт разговора; ждут распознавания: 1; не распознано: 2")
    show(tray, **waiting, failed=2, gateway=gateway.DOWN)             # nothing waits: the gateway is nobody's trouble
    assert tray.icon.title.endswith(": ждёт разговора; не распознано: 2")
    show(tray, **waiting)
    assert tray.icon.title.endswith(": ждёт разговора")


def test_what_waits_is_said_beside_a_recording_and_with_recording_switched_off_too(tray):
    show(tray, state=core.RECORDING, program="Zoom", minutes=12, enabled=True, microphone="Headset", waiting=4)
    assert tray.icon.title.endswith(": идёт запись: Zoom, 12 мин; ждут распознавания: 4")
    show(tray, state=core.OFF, recognising=True, waiting=0)
    assert tray.icon.title.endswith(": запись выключена; распознаётся: 1, ждут: 0")


def test_a_tooltip_is_never_longer_than_windows_takes(tray):
    show(tray, state=core.RECORDING, program="A" * 200, minutes=3, enabled=True, microphone="Headset")
    assert len(tray.icon.title) == tray_app.TOOLTIP


def test_the_icon_wears_dots_while_a_conversation_is_being_recognised(tray, monkeypatch):
    drawn = []
    monkeypatch.setattr(tray_app.icons, "image", lambda state: drawn.append(state) or state)
    show(tray, state=core.WAITING, recognising=True)
    show(tray, state=core.OFF, recognising=True)                     # recording switched off: recognition goes on
    show(tray, state=core.RECORDING, program="Zoom", recognising=True)        # a recording comes first...
    show(tray, state=core.ERROR, problem=core.PROBLEM_DATABASE, recognising=True)     # ...and so does an error
    show(tray, state=core.WAITING)
    assert drawn == [icons.TRANSCRIBING, icons.RECORDING, icons.ERROR, icons.WAITING]


def test_conversations_that_failed_can_be_tried_again_from_the_menu(tray):
    show(tray, state=core.WAITING, enabled=True, microphone="Headset")
    assert "Повторить распознавание" not in rows(tray)
    before = tray.icon.menu_updates
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", failed=2)
    assert tray.icon.menu_updates == before + 1                      # the row came
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", failed=3)
    assert tray.icon.menu_updates == before + 1                      # one more failed: the row is there already
    click(tray, "Повторить распознавание")
    assert tray._app.told == ["retry"]


def test_the_tooltip_of_recognition_follows_the_language(tray):
    from protocallist import i18n

    i18n.set_language("en")
    show(tray, state=core.WAITING, enabled=True, microphone="Headset", waiting=3, gateway=gateway.OLD, failed=1)
    assert tray.icon.title.endswith("; Tolmach 0.8.0 or newer is needed, waiting: 3; not recognised: 1")


def test_an_ampersand_in_the_name_of_a_microphone_is_shown_as_it_is_and_the_name_is_kept(tray):
    show(tray, state=core.WAITING, enabled=True, microphone="Bang & Olufsen", microphones=("Webcam", "Bang & Olufsen"))
    offered = list(tray._microphones())
    assert [(row.text, row.checked) for row in offered] == [("Webcam", False), ("Bang && Olufsen", True)]
    before = threading.active_count()
    offered[1](tray.icon)
    deadline = time.monotonic() + 2
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.005)
    assert tray._app.told == [("microphone", "Bang & Olufsen")]         # the setting gets the name itself
