"""The tray's side of updates: one menu line, a click, a hand-over to a new process."""
import threading
from pathlib import Path

import pytest

from protocallist import VERSION
from protocallist import app as core
from protocallist.app import Glance
from protocallist.i18n import Text
from protocallist.tray import app as tray_app
from protocallist.tray import updater

from .test_app import FakeApp, quiet, tray  # noqa: F401  (the fixtures)

BEHIND = updater.Update("behind", "9.9.9", commit="c" * 40)
SAME_LIBRARIES = updater.Pulled("a" * 40, "b" * 40, False)
NEW_LIBRARIES = updater.Pulled("a" * 40, "b" * 40, True)


def rows(tray):  # noqa: F811
    return [item.text for item in tray.icon.menu.items if item.visible]


def row(tray, start: str):  # noqa: F811
    return next(item for item in tray.icon.menu.items if item.visible and item.text.startswith(start))


@pytest.fixture
def acts(tray, monkeypatch):  # noqa: F811
    """Everything an update touches, recorded instead of done."""
    log = {"pulled": [], "checks": 0, "quiet": 0, "window": 0, "pull": SAME_LIBRARIES, "spawn_error": None,
           "status": updater.Update("none")}

    def pull(root, expected):
        log["pulled"].append(expected)
        if isinstance(log["pull"], Exception):
            raise log["pull"]
        return log["pull"]

    def status(root):
        log["checks"] += 1
        return log["status"]

    def spawn(kind):
        def run(root):
            if log["spawn_error"] is not None:
                raise log["spawn_error"]
            log[kind] += 1
        return run

    monkeypatch.setattr(tray_app.updater, "pull", pull)
    monkeypatch.setattr(tray_app.updater, "status", status)
    monkeypatch.setattr(tray_app.relaunch, "spawn", spawn("quiet"))
    monkeypatch.setattr(tray_app.relaunch, "spawn_console", spawn("window"))
    return log


def handed_over(tray, log, how):  # noqa: F811
    """The program gave way to exactly one successor of the given kind and stopped its icon."""
    other = "window" if how == "quiet" else "quiet"
    return (log[how], log[other], tray.icon.stops) == (1, 0, 1)


def still_running(tray, log):  # noqa: F811
    return (log["quiet"], log["window"], tray.icon.stops) == (0, 0, 0)


# --- the menu


def test_no_update_line_when_there_is_nothing_to_do(tray):  # noqa: F811
    assert not [text for text in rows(tray) if "Обновить" in text or "Перезапустить" in text or "Обновления" in text]
    assert "Проверить обновления" not in rows(tray)          # updates are off until origin has been seen


def test_an_update_on_origin_shows_one_line_with_its_version(tray):  # noqa: F811
    tray._update = BEHIND
    assert "Обновить до 9.9.9" in rows(tray)
    assert "Проверить обновления" in rows(tray)


def test_newer_code_on_disk_shows_a_restart_line(tray):  # noqa: F811
    tray._on_disk = "9.9.9"
    assert "Перезапустить: на диске версия 9.9.9" in rows(tray)


def test_a_developers_clone_is_told_why_it_gets_nothing(tray):  # noqa: F811
    tray._update = updater.Update("off", note=Text("ветка {branch} — обновления выключены", branch="feature"))
    assert "Обновления: ветка feature — обновления выключены" in rows(tray)
    assert row(tray, "Обновления").enabled is False            # a remark, not something to click


def test_the_menu_is_rebuilt_when_an_update_appears(tray):  # noqa: F811
    tray.refresh()
    before = tray.icon.menu_updates
    tray._update = BEHIND
    tray.refresh()
    assert tray.icon.menu_updates == before + 1


def test_the_menu_is_rebuilt_whenever_one_of_the_update_rows_changes(tray):  # noqa: F811
    def rebuilt() -> bool:
        before = tray.icon.menu_updates
        tray.refresh()
        return tray.icon.menu_updates == before + 1

    tray.refresh()
    assert not rebuilt()                                       # nothing changed: nothing is rebuilt
    tray._code_changed = True
    assert rebuilt()                                           # the restart line came
    tray._failure = Text("не удалось запустить новую копию — см. журнал")
    assert rebuilt()                                           # the remark came
    tray._update, tray._checked = updater.Update("none"), None
    assert rebuilt()                                           # the row "check for updates" came
    tray._checked = ("12:05", "none")
    assert rebuilt()                                           # ...and now says when it was checked
    assert not rebuilt()


def test_the_running_version_and_code_are_what_the_disk_is_compared_with(tray):  # noqa: F811
    assert tray._on_disk == VERSION and tray._code == "code-at-start" and tray._code_changed is False


def test_the_update_lines_follow_the_language(tray):  # noqa: F811
    from protocallist import i18n

    tray._update = BEHIND
    tray._failure = Text("нет связи с origin — обновление не выполнено")
    i18n.set_language("en")
    assert "Update to 9.9.9" in rows(tray) and "Check for updates" in rows(tray)
    assert "Updates: no answer from origin — the update was not taken" in rows(tray)


# --- the code on disk is watched


def test_code_that_changed_on_disk_under_the_same_version_shows_a_restart_line(tray, monkeypatch):  # noqa: F811
    tray._look_at_disk()
    assert tray._update_line() is None                         # the code is the one the program started with
    monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: "code-after-the-merge")
    tray._look_at_disk()
    assert "Перезапустить: код на диске изменился" in rows(tray)
    monkeypatch.setattr(tray_app.updater, "disk_version", lambda root: "9.9.9")
    tray._look_at_disk()
    assert "Перезапустить: на диске версия 9.9.9" in rows(tray)


def test_code_that_cannot_be_read_right_now_is_not_called_changed(tray, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: None)    # git is writing the files
    monkeypatch.setattr(tray_app.updater, "disk_version", lambda root: None)
    tray._look_at_disk()
    assert tray._update_line() is None and tray._on_disk == VERSION


def test_libraries_that_fell_behind_are_noticed_while_the_program_runs(tray, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tray_app.updater, "libraries_stale", lambda root: True)
    tray._look_at_disk()
    assert "Поставить библиотеки: requirements.txt изменился" in rows(tray)


def test_the_disk_is_looked_at_again_and_again_and_origin_now_and_then(tray, acts, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tray_app, "WATCH_S", 0.001)
    monkeypatch.setattr(tray_app, "UPDATE_EVERY", 2)
    looks, asked_on = [], []
    origin_answers = threading.Event()

    def look():
        looks.append(1)
        if len(looks) >= 5:
            tray._stopping.set()

    def status(root):
        asked_on.append(threading.current_thread().name)
        origin_answers.wait(5)                                 # an origin that takes its time
        return updater.Update("none")

    monkeypatch.setattr(tray, "_look_at_disk", look)
    monkeypatch.setattr(tray_app.updater, "status", status)
    watch = threading.Thread(target=tray._watch, name="updates")
    watch.start()
    watch.join(3)
    still_watching = watch.is_alive()
    origin_answers.set()
    watch.join(3)
    assert not still_watching and len(looks) == 5              # the disk was watched while origin was silent
    assert asked_on and "updates" not in asked_on              # origin is asked from a thread of its own


def test_a_look_that_fails_is_logged_and_the_next_one_is_made(tray, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(tray_app, "WATCH_S", 0.001)
    looks = []

    def look():
        looks.append(1)
        if len(looks) >= 2:
            tray._stopping.set()
        raise OSError("the disk went away")

    monkeypatch.setattr(tray, "_look_at_disk", look)
    with caplog.at_level("ERROR", logger="protocallist"):
        tray._watch()
    assert len(looks) == 2 and "could not look for updates" in caplog.text


def test_the_watching_starts_with_the_icon_and_ends_with_it(monkeypatch):
    from .test_app import FakeIcon

    monkeypatch.setattr(tray_app.pystray, "Icon", FakeIcon)
    monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: "code-at-start")
    monkeypatch.setattr(tray_app.updater, "requirements_digest", lambda root: "digest-at-start")
    watched, asked = threading.Event(), threading.Event()
    monkeypatch.setattr(tray_app.TrayApp, "_watch", lambda self: watched.set())
    monkeypatch.setattr(tray_app.updater, "status", lambda root: asked.set() or updater.Update("off"))
    made = tray_app.TrayApp(FakeApp, Path("C:/protocallist"), "C:/python/pythonw.exe")
    assert not made._stopping.is_set()
    made.run()                                                 # the faked icon sets up and returns at once
    assert watched.wait(5)                                     # the disk is watched from the moment the icon is up
    assert asked.wait(5)                                       # ...and origin is asked once, right away
    assert made._stopping.is_set()                             # the icon is gone: the watching is told to end


# --- taking an update


def test_a_click_takes_the_update_and_a_new_process_takes_over(tray, acts):  # noqa: F811
    tray._update = BEHIND
    tray._apply_update(tray.icon, None)
    assert acts["pulled"] == [BEHIND]                         # exactly what the click was on
    assert handed_over(tray, acts, "quiet")


def test_nothing_is_updated_or_restarted_while_a_conversation_is_being_recorded(tray, acts):  # noqa: F811
    tray._update = BEHIND
    tray._app.shown = Glance(state=core.RECORDING, program="Zoom", enabled=True, microphone="Headset")
    assert row(tray, "Обновить").enabled is False              # the line is there, greyed
    tray._apply_update(tray.icon, None)                        # ...and a click that got through does nothing
    assert acts["pulled"] == [] and still_running(tray, acts)
    tray._app.shown = Glance(state=core.WAITING, enabled=True, microphone="Headset")
    assert row(tray, "Обновить").enabled is True


def test_a_conversation_that_began_while_the_update_was_being_taken_is_not_cut_by_a_restart(tray, acts, monkeypatch):  # noqa: F811
    tray._update = BEHIND

    def pull(root, expected):
        # origin took its time, and meanwhile a program took the microphone
        tray._app.shown = Glance(state=core.RECORDING, program="Zoom", enabled=True, microphone="Headset")
        monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: "code-after-the-update")
        return SAME_LIBRARIES

    monkeypatch.setattr(tray_app.updater, "pull", pull)
    tray._apply_update(tray.icon, None)
    assert still_running(tray, acts)                           # the update is on disk; the restart waits
    line = row(tray, "Перезапустить")
    assert line.text == "Перезапустить: код на диске изменился" and line.enabled is False
    tray._app.shown = Glance(state=core.WAITING, enabled=True, microphone="Headset")
    tray._apply_update(tray.icon, None)                        # the conversation is over: now the click restarts
    assert handed_over(tray, acts, "quiet")


def test_an_update_that_was_taken_is_not_offered_again_when_the_restart_could_not_be_made(tray, acts, monkeypatch):  # noqa: F811
    tray._update = BEHIND
    acts["spawn_error"] = OSError("no such file")

    def pull(root, expected):
        monkeypatch.setattr(tray_app.updater, "code_digest", lambda root: "code-after-the-update")
        return SAME_LIBRARIES

    monkeypatch.setattr(tray_app.updater, "pull", pull)
    tray._apply_update(tray.icon, None)
    assert not [text for text in rows(tray) if text.startswith("Обновить")]     # it is on disk already
    assert "Перезапустить: код на диске изменился" in rows(tray)


def test_a_pull_that_fails_is_said_in_the_menu_and_origin_is_asked_again(tray, acts, caplog):  # noqa: F811
    tray._update = BEHIND
    acts["pull"] = updater.UpdateError(Text("git merge не выполнен: {reason}", reason="exit 1"))
    acts["status"] = updater.Update("behind", "10.0.0")
    with caplog.at_level("WARNING", logger="protocallist"):
        tray._apply_update(tray.icon, None)
    assert still_running(tray, acts)
    assert tray._update == updater.Update("behind", "10.0.0")  # the menu line is fresh for the next click
    assert "Обновления: git merge не выполнен: exit 1" in rows(tray)       # no window pops up: the menu says it
    assert "update not taken: git merge failed: exit 1" in caplog.text     # the log is always English


def test_the_complaint_goes_when_origin_is_looked_at_again_or_the_line_is_clicked_again(tray, acts):  # noqa: F811
    tray._update = BEHIND
    acts["pull"] = updater.UpdateError(Text("нет связи с origin — обновление не выполнено"))
    acts["status"] = BEHIND
    tray._apply_update(tray.icon, None)
    assert [text for text in rows(tray) if text.startswith("Обновления")]
    tray._check_update()
    assert not [text for text in rows(tray) if text.startswith("Обновления")]
    tray._apply_update(tray.icon, None)
    acts["pull"] = SAME_LIBRARIES
    tray._apply_update(tray.icon, None)
    assert tray._failure is None and handed_over(tray, acts, "quiet")


def test_changed_libraries_are_installed_by_the_launcher_window_not_by_the_program(tray, acts):  # noqa: F811
    # pip needs the program gone (it holds the files pip replaces) and a window that shows
    # what went wrong: that is the launcher window, not a process under pythonw.
    tray._update = BEHIND
    acts["pull"] = NEW_LIBRARIES
    tray._apply_update(tray.icon, None)
    assert handed_over(tray, acts, "window")


# --- newer code on disk


def test_newer_code_on_disk_restarts_without_pulling(tray, acts):  # noqa: F811
    tray._code_changed = True
    tray._apply_update(tray.icon, None)
    assert acts["pulled"] == [] and acts["checks"] == 0
    assert handed_over(tray, acts, "quiet")


def test_newer_code_with_other_libraries_goes_through_the_launcher_window(tray, acts, monkeypatch):  # noqa: F811
    tray._on_disk = "9.9.9"
    monkeypatch.setattr(tray_app.updater, "requirements_digest", lambda root: "digest-after-the-merge")
    tray._apply_update(tray.icon, None)
    assert handed_over(tray, acts, "window")


def test_newer_code_whose_libraries_the_record_calls_stale_goes_through_the_window_too(tray, acts, monkeypatch):  # noqa: F811
    tray._code_changed = True
    monkeypatch.setattr(tray_app.updater, "libraries_stale", lambda root: True)
    tray._apply_update(tray.icon, None)
    assert handed_over(tray, acts, "window")


def test_libraries_left_behind_by_a_failed_install_get_their_own_menu_line(tray, acts):  # noqa: F811
    tray._stale = True
    assert "Поставить библиотеки: requirements.txt изменился" in rows(tray)
    tray._apply_update(tray.icon, None)
    assert handed_over(tray, acts, "window")


def test_a_click_with_nothing_to_do_does_nothing(tray, acts):  # noqa: F811
    tray._apply_update(tray.icon, None)
    assert still_running(tray, acts)


# --- a program whose service died


def test_a_program_that_failed_offers_to_restart_itself(tray, acts):  # noqa: F811
    tray._app.shown = Glance(state=core.ERROR, problem=core.PROBLEM_FAILED, enabled=True, microphone="Headset")
    line = row(tray, "Перезапустить")
    assert line.text == "Перезапустить: программа остановилась" and line.enabled is True
    tray._apply_update(tray.icon, None)
    assert acts["pulled"] == [] and handed_over(tray, acts, "quiet")


def test_any_other_error_is_not_mended_by_a_restart_and_offers_none(tray):  # noqa: F811
    tray._app.shown = Glance(state=core.ERROR, problem=core.PROBLEM_DATABASE, enabled=True, microphone="Headset")
    assert not [text for text in rows(tray) if text.startswith("Перезапустить")]


def test_newer_code_says_more_than_a_program_that_failed(tray):  # noqa: F811
    tray._app.shown = Glance(state=core.ERROR, problem=core.PROBLEM_FAILED, enabled=True, microphone="Headset")
    tray._code_changed = True
    assert [text for text in rows(tray) if text.startswith("Перезапустить")] == ["Перезапустить: код на диске изменился"]


# --- a hand-over that cannot be made leaves the program working


def test_a_successor_that_cannot_be_started_leaves_the_program_working_and_says_so(tray, acts, caplog):  # noqa: F811
    tray._code_changed = True
    acts["spawn_error"] = OSError("no such file")
    with caplog.at_level("ERROR", logger="protocallist"):
        tray._apply_update(tray.icon, None)
    assert tray.icon.stops == 0 and not tray._stopping.is_set()
    assert "Обновления: не удалось запустить новую копию — см. журнал" in rows(tray)
    assert "could not start the new copy" in caplog.text
    assert "Перезапустить: код на диске изменился" in rows(tray)        # still there for another try


def test_a_launcher_window_that_cannot_be_opened_changes_nothing(tray, acts, monkeypatch):  # noqa: F811
    tray._on_disk = "9.9.9"
    monkeypatch.setattr(tray_app.updater, "requirements_digest", lambda root: "digest-after-the-merge")
    acts["spawn_error"] = OSError("no such file")
    tray._apply_update(tray.icon, None)
    assert tray.icon.stops == 0 and acts["window"] == 0
    assert [text for text in rows(tray) if text.startswith("Обновления")]


# --- asking origin


def test_a_check_asks_origin_and_remembers_the_answer(tray, acts):  # noqa: F811
    acts["status"] = BEHIND
    assert tray._check_update() == "behind"
    assert tray._update == BEHIND


def test_an_origin_that_did_not_answer_does_not_erase_what_is_known(tray, acts):  # noqa: F811
    tray._update = BEHIND
    acts["status"] = updater.Update("unknown")
    tray._check_update()
    assert tray._update == BEHIND
    assert "Обновить до 9.9.9" in rows(tray)


def test_a_check_that_blows_up_is_logged_and_keeps_the_last_answer(tray, monkeypatch, caplog):  # noqa: F811
    def boom(root):
        raise RuntimeError("git exploded")

    tray._update = BEHIND
    monkeypatch.setattr(tray_app.updater, "status", boom)
    with caplog.at_level("ERROR", logger="protocallist"):
        assert tray._check_update() is None
    assert tray._update == BEHIND
    assert any(record.exc_info for record in caplog.records)


def test_a_check_from_the_menu_that_finds_nothing_says_when_it_looked(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)
    assert "Обновлений нет — проверено в 12:05" in rows(tray)


def test_the_time_in_the_check_line_is_that_of_the_check_it_reports(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)             # by hand: nothing new
    acts["status"] = updater.Update("unknown")
    tray._check_update()                                # the six-hour check gets no answer
    assert "Обновлений нет — проверено в 12:05" in rows(tray)
    assert not [text for text in rows(tray) if text.startswith("Нет связи")]


def test_a_check_from_the_menu_that_got_no_answer_says_so_and_not_that_all_is_current(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    acts["status"] = updater.Update("unknown")
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)
    assert "Нет связи с origin — проверено в 12:05" in rows(tray)
    assert not [text for text in rows(tray) if text.startswith("Обновлений нет")]


def test_an_update_found_later_takes_the_place_of_what_the_check_by_hand_said(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    acts["status"] = updater.Update("unknown")
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)             # by hand: origin did not answer
    acts["status"] = BEHIND
    tray._check_update()                                # hours later origin answers, and there is an update
    assert "Обновить до 9.9.9" in rows(tray) and "Проверить обновления" in rows(tray)
    assert not [text for text in rows(tray) if "проверено в" in text]


def test_what_the_check_by_hand_found_is_not_repeated_once_it_is_no_longer_known(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)             # by hand: nothing new
    tray._update = updater.Update("unknown")            # since then origin went out of reach
    assert "Проверить обновления" in rows(tray)
    assert not [text for text in rows(tray) if "проверено в" in text]


def test_a_check_that_found_an_update_shows_the_update_not_the_time(tray, acts, monkeypatch):  # noqa: F811
    tray._update = updater.Update("none")
    acts["status"] = BEHIND
    monkeypatch.setattr(tray_app.time, "strftime", lambda fmt: "12:05")
    tray._check_update_now(tray.icon, None)
    assert "Обновить до 9.9.9" in rows(tray) and "Проверить обновления" in rows(tray)


def test_a_check_does_not_run_into_an_update_that_is_being_taken(tray, acts, monkeypatch):  # noqa: F811
    tray._update = BEHIND
    seen_during_pull = []

    def pull(root, expected):
        other = threading.Thread(target=lambda: seen_during_pull.append(tray._check_update()))
        other.start()
        other.join()
        return SAME_LIBRARIES

    monkeypatch.setattr(tray_app.updater, "pull", pull)
    tray._apply_update(tray.icon, None)
    assert seen_during_pull == [None] and acts["checks"] == 0   # the check stepped aside


def test_a_second_click_while_the_first_is_at_work_does_nothing(tray, acts, monkeypatch):  # noqa: F811
    tray._update = BEHIND
    clicks, pulls, second = [], [], []

    def pull(root, expected):
        pulls.append(expected)
        if not clicks:                                      # in the middle of the first pull: a second click
            clicks.append(threading.Thread(target=lambda: second.append(tray._apply_update(tray.icon, None)),
                                           daemon=True))
            clicks[0].start()
            clicks[0].join(2)
        return SAME_LIBRARIES

    monkeypatch.setattr(tray_app.updater, "pull", pull)
    tray._apply_update(tray.icon, None)
    clicks[0].join(5)
    assert second == [None] and len(pulls) == 1             # it came back at once, having done nothing
    assert handed_over(tray, acts, "quiet")                 # one pull, one successor
