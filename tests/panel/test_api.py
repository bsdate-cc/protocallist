"""What the page of the panel asks and is answered: a real archive, a real summariser whose "Claude" is a
stand-in, and a stand-in for the service behind the icon."""
import logging
import threading
import time
from datetime import datetime, timedelta

from protocallist import app as core
from protocallist import gateway, i18n, summaries, transcriber
from protocallist.app import Glance
from protocallist.i18n import Text
from protocallist.panel import api as asked
from protocallist.panel.api import Api
from protocallist.store import FAILED, MADE, MINE, RECORDED, Phrase
from protocallist.summary import SummaryError

from .conftest import ANSWER, AT, ENGLISH, Service

TALK = (("me", "Начнём."), ("me", "С плана."), ("them", "Давайте."), ("me", "Про Лицензию узнаю сегодня."))


def local(moment: str) -> str:
    """A moment as the archive writes it: local time with the offset of this computer."""
    return datetime.fromisoformat(moment).astimezone().isoformat(timespec="seconds")


def wait_for(what, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not what() and time.monotonic() < deadline:
        time.sleep(0.005)
    return bool(what())


def settled(world, seconds: float = 5.0) -> None:
    """Wait until no summary is being made and none waits its turn."""
    def still() -> bool:
        line = world.service.makes_summaries.line()
        return not line.making and not line.queued

    assert wait_for(still, seconds)


def status(world) -> dict:
    return world.ask("GET", "/api/status")[1]


def own(world, call, text: str) -> None:
    """A summary of the user's own, as an archive may hold one: the panel no longer makes them."""
    world.store.save_summary(call.id, MINE, text, world.now)
    summaries.settle(world.store, call)


# --- what the program is doing


def test_the_page_is_told_what_the_icon_shows_and_where_a_summary_would_go(world):
    world.service.shown = Glance(state=core.RECORDING, program="Zoom", minutes=12, microphone="Headset",
                                 recognising=False, waiting=2, failed=1, gateway="down")
    status, data = world.ask("GET", "/api/status")
    assert status == 200 and data["version"] == "0.1.0" and data["language"] == "ru" and data["archive"] is True
    assert (data["state"], data["program"], data["minutes"], data["microphone"]) == ("recording", "Zoom", 12, "Headset")
    assert (data["recognising"], data["waiting"], data["failed"], data["gateway"]) == (False, 2, 1, "down")
    assert data["summary"] == {"kind": "claude", "name": "opus"} and data["keep_audio_days"] == 30
    assert data["making"] == [] and data["failures"] == {} and data["problem"] == "" and data["recording"] == 0
    assert data["queued"] == [] and data["stopped"] is None
    going = world.store.begin(AT, "Zoom")
    assert world.ask("GET", "/api/status")[1]["recording"] == going.id
    world.service.shown = Glance(state=core.ERROR, problem=core.PROBLEM_DATABASE)
    assert world.ask("GET", "/api/status")[1]["problem"] == "база не открывается"


def test_without_an_archive_the_page_is_told_so_and_asks_in_vain(world):
    service = Service()
    api = Api(service)
    data = api.handle("GET", "/api/status", {}, b"").data
    assert data["archive"] is False and data["stamp"] == "" and data["summary"] == {"kind": "", "name": ""}
    assert api.handle("GET", "/api/calls", {}, b"").data == {"calls": [], "ids": []}
    assert api.handle("GET", "/api/calls/1", {}, b"").status == 404
    for method, path in (("POST", "/api/calls/1/summary"), ("DELETE", "/api/calls/1"),
                         ("POST", "/api/summaries"), ("POST", "/api/summaries/restart"), ("POST", "/api/summaries/reset"),
                         ("POST", "/api/summaries/halt")):
        answer = api.handle(method, path, {}, b'{"text": "x"}')
        assert answer.status == 503 and answer.data == {"error": "архив не открыт"}
    assert api.handle("POST", "/api/calls/1/retry", {}, b"").status == 409


def test_the_stamp_changes_when_anything_the_list_shows_has_changed(world):
    stamps = [world.ask("GET", "/api/status")[1]["stamp"]]

    def changed() -> bool:
        stamps.append(world.ask("GET", "/api/status")[1]["stamp"])
        return stamps[-1] != stamps[-2]

    assert not changed()
    call = world.call(*TALK)
    assert changed()                                                    # a conversation more
    world.store.set_topic(call.id, "Начнём.")
    assert changed()
    world.service.makes_summaries.make(call.id)
    assert changed()                                                    # it has a summary, and is about something else
    own(world, call, ANSWER + "\n## Задачи\n- Моя.\n")
    assert changed() and world.store.get(call.id).topic == "Говорили о плане."     # the user's own is in force: that alone
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=5), "Zoom").id, 60.0)
    assert changed()
    assert world.store.take().id == waiting.id and changed()             # it is being recognised now: that alone
    world.store.sound(call).write_bytes(b"OggS")
    assert changed()                                                    # its sound was made small
    world.store.audio_deleted(call.id, world.now)
    assert changed() and not changed()


# --- the list


def test_the_list_says_of_every_conversation_what_the_table_shows(world):
    talk = world.call(*TALK, seconds=4441.0)
    world.store.set_topic(talk.id, "Начнём. С плана.")
    world.store.sound(talk).write_bytes(b"x" * 1234)
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=5), "Zoom").id, 12.4)
    status, data = world.ask("GET", "/api/calls")
    first, second = data["calls"]
    assert status == 200 and first == {
        "id": talk.id, "started": local("2026-10-06T15:55:33"), "ended": local("2026-10-06T17:09:34"), "program": "Discord",
        "seconds": 4441, "state": "done", "remarks": 3, "topic": "Начнём. С плана.", "about": "start", "summary": "", "redo": "",
        "sound": {"state": "kept", "bytes": 1234, "until": local("2026-11-05T17:09:34")}}
    assert (second["id"], second["state"], second["seconds"], second["remarks"], second["topic"], second["about"]) == \
        (waiting.id, RECORDED, 12, 0, "", "")
    assert second["sound"] == {"state": "none"}                         # its recordings are not even there


def test_the_list_says_whose_summary_is_in_force_and_what_the_conversation_is_about_by_it(world):
    made, mine = world.call(*TALK), world.call(*TALK, minutes_later=5)
    world.service.makes_summaries.make(made.id)
    own(world, mine, "## Коротко\nПереезд в четверг.\n")
    world.service.makes_summaries.make(mine.id, anew=True)
    rows = {row["id"]: row for row in world.ask("GET", "/api/calls")[1]["calls"]}
    assert (rows[made.id]["summary"], rows[made.id]["about"], rows[made.id]["topic"]) == (MADE, "summary", "Говорили о плане.")
    assert (rows[mine.id]["summary"], rows[mine.id]["about"], rows[mine.id]["topic"]) == (MINE, "summary", "Переезд в четверг.")


def test_the_list_says_where_the_sound_of_a_conversation_is(world):
    kept, deleted, raw = world.call(*TALK), world.call(*TALK, minutes_later=5), world.call(*TALK, minutes_later=10)
    world.store.sound(kept).write_bytes(b"OggS")
    world.store.audio_deleted(deleted.id, world.now)
    world.store.folder(raw).mkdir()
    (world.store.folder(raw) / "line.wav").write_bytes(b"RIFF")

    def sounds() -> dict:
        return {row["id"]: row["sound"] for row in world.ask("GET", "/api/calls")[1]["calls"]}

    assert sounds() == {kept.id: {"state": "kept", "bytes": 4, "until": local("2026-11-05T15:56:33")},
                        deleted.id: {"state": "deleted", "at": local("2026-10-07T15:55:33")}, raw.id: {"state": "raw"}}
    world.service.cfg.storage.keep_audio_days = -1
    assert sounds()[kept.id] == {"state": "kept", "bytes": 4, "until": None}        # for ever


def test_sound_kept_for_more_days_than_a_date_can_hold_is_kept_for_ever(world):
    call = world.call(*TALK)
    world.store.sound(call).write_bytes(b"OggS")
    world.service.cfg.storage.keep_audio_days = 99999999
    assert world.ask("GET", "/api/calls")[1]["calls"][0]["sound"] == {"state": "kept", "bytes": 4, "until": None}


def test_the_remarks_of_a_recognised_conversation_are_counted_once(world, monkeypatch):
    call = world.call(*TALK)
    counted, count = [], world.store.remarks
    monkeypatch.setattr(world.store, "remarks", lambda call_id: counted.append(call_id) or count(call_id))
    for _ in range(3):
        assert world.ask("GET", "/api/calls")[1]["calls"][0]["remarks"] == 3
    assert counted == [call.id]
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=5), "Zoom").id, 60.0)
    assert world.ask("GET", "/api/calls")[1]["calls"][1]["remarks"] == 0 and counted == [call.id]
    world.store.take()
    world.store.done(waiting.id, [Phrase("me", 0.0, 1.0, "Да.")], "gigaam-v3")
    assert world.ask("GET", "/api/calls")[1]["calls"][1]["remarks"] == 1    # not before there was anything to count


def test_words_are_looked_for_in_everything_that_was_said(world):
    first, second = world.call(*TALK), world.call(("them", "Совсем о другом."), minutes_later=5)
    assert world.ask("GET", "/api/calls", q="лицензию")[1] == {"ids": [first.id]}
    assert world.ask("GET", "/api/calls", q="О ДРУГОМ")[1] == {"ids": [second.id]}
    assert world.ask("GET", "/api/calls", q="  ")[1] == {"ids": []}


# --- one conversation


def test_a_conversation_is_told_whole_with_its_remarks_and_who_spoke_when(world):
    call = world.call(*TALK, ("them", "Хорошо."), ("them", "Тогда всё."))
    world.service.makes_summaries.make(call.id)
    own(world, call, "## Коротко\nСам.\n")
    status, data = world.ask("GET", f"/api/calls/{call.id}")
    assert status == 200 and (data["id"], data["name"], data["model"], data["summary"]) == \
        (call.id, "2026-10-06_15-55-33_Discord", "gigaam-v3", MINE)
    assert data["remarks_list"] == [
        {"who": "me", "at": 0.0, "text": "Начнём. С плана."}, {"who": "them", "at": 10.0, "text": "Давайте."},
        {"who": "me", "at": 15.0, "text": "Про Лицензию узнаю сегодня."}, {"who": "them", "at": 20.0, "text": "Хорошо. Тогда всё."}]
    # phrases five seconds apart, each four long: a second of pause joins them into one stretch of speech
    assert data["speech"] == {"me": [[0.0, 9.0], [15.0, 19.0]], "them": [[10.0, 14.0], [20.0, 29.0]]}
    assert data["summaries"][MADE] == {"text": ANSWER, "saved": local("2026-10-07T15:55:33"), "maker": "opus", "seconds": 0.0,
                                       "stale": False}
    assert data["summaries"][MINE] == {"text": "## Коротко\nСам.\n", "saved": local("2026-10-07T15:55:33"), "maker": "", "seconds": None,
                                       "stale": False}
    assert (data["text_at"], data["redo"], data["redo_error"]) == (local("2026-10-06T15:56:33"), "", "")


def test_a_conversation_that_failed_says_why_and_one_that_is_not_there_says_so(world):
    failed = world.store.finish(world.store.begin(AT, "Zoom").id, 60.0)
    for _ in range(3):
        world.store.take()
        world.store.missed(failed.id, "the gateway answered 500: boom")
    data = world.ask("GET", f"/api/calls/{failed.id}")[1]
    assert (data["state"], data["error"], data["attempts"], data["remarks_list"], data["summaries"]) == \
        (FAILED, "the gateway answered 500: boom", 3, [], {})
    assert world.ask("GET", "/api/calls/999") == (404, {"error": "такой записи нет"})


def test_the_sound_the_text_and_the_summaries_of_a_conversation_are_given_as_files(world):
    call = world.call(*TALK)
    assert world.ask("GET", f"/api/calls/{call.id}/audio") == (404, {"error": "звука этой записи нет"})
    world.store.sound(call).write_bytes(b"OggS")
    assert world.api.handle("GET", f"/api/calls/{call.id}/audio", {}, b"").file == world.store.sound(call)
    text = world.api.handle("GET", f"/api/calls/{call.id}/text.md", {}, b"")
    assert text.name == "2026-10-06_15-55-33_Discord - текст.md" and "**[00:00:10] Собеседники:** Давайте." in text.text
    assert world.ask("GET", f"/api/calls/{call.id}/summary.md") == (404, {"error": "такой выжимки нет"})
    world.service.makes_summaries.make(call.id)
    own(world, call, "## Коротко\nСам.\n")
    in_force = world.api.handle("GET", f"/api/calls/{call.id}/summary.md", {}, b"")
    assert (in_force.text, in_force.name) == ("## Коротко\nСам.\n", "2026-10-06_15-55-33_Discord - выжимка.md")
    assert world.api.handle("GET", f"/api/calls/{call.id}/summary.md", {"kind": MADE}, b"").text == ANSWER
    assert world.ask("GET", f"/api/calls/{call.id}/summary.md", kind="another")[0] == 404
    assert world.ask("GET", "/api/calls/999/audio")[0] == world.ask("GET", "/api/calls/999/text.md")[0] == 404
    i18n.set_language("en")                                             # the names follow the language, as the text does
    assert world.api.handle("GET", f"/api/calls/{call.id}/text.md", {}, b"").name == "2026-10-06_15-55-33_Discord - text.md"
    assert world.api.handle("GET", f"/api/calls/{call.id}/summary.md", {}, b"").name == "2026-10-06_15-55-33_Discord - summary.md"


# --- what is asked to be done


def test_conversations_chosen_in_the_list_are_sent_to_the_end_of_the_line(world):
    first, second, third = (world.call(*TALK, minutes_later=5 * n) for n in range(3))
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=30), "Zoom").id, 60.0)     # not recognised yet
    gate, seen = threading.Event(), []

    def meanwhile():
        if not seen:
            seen.append(world.ask("POST", f"/api/calls/{first.id}/summary"))
        gate.wait(5)

    world.during = meanwhile
    assert world.ask("POST", "/api/summaries", {"ids": [first.id, second.id, waiting.id, 999]}) == \
        (202, {"taken": [first.id, second.id]})                         # only what has a text to send
    assert wait_for(lambda: seen) and seen == [(409, {"error": "выжимка этого разговора уже делается"})]
    assert (status(world)["making"], status(world)["queued"], status(world)["stopped"]) == ([first.id], [second.id], None)
    assert world.ask("POST", "/api/summaries", {"ids": [third.id, second.id]}) == (202, {"taken": [third.id]})
    assert status(world)["queued"] == [second.id, third.id]
    gate.set()
    settled(world)
    assert [asked["label"] for asked in world.asked] == [first.folder, second.folder, third.folder]
    assert set(world.store.kinds()) == {first.id, second.id, third.id}


def test_a_summary_asked_for_on_the_page_of_a_conversation_goes_next(world):
    first, second, third = (world.call(*TALK, minutes_later=5 * n) for n in range(3))
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.ask("POST", "/api/summaries", {"ids": [first.id, second.id]})
    assert wait_for(lambda: status(world)["making"] == [first.id])
    assert world.ask("POST", f"/api/calls/{third.id}/summary") == (202, {})
    assert status(world)["queued"] == [third.id, second.id]
    gate.set()
    settled(world)
    assert [asked["label"] for asked in world.asked] == [first.folder, third.folder, second.folder]
    world.ask("POST", f"/api/calls/{third.id}/summary")                 # it has one: nothing is sent again...
    settled(world)
    assert len(world.asked) == 3
    world.ask("POST", f"/api/calls/{third.id}/summary", {"anew": True})  # ...unless that is what is asked
    settled(world)
    assert len(world.asked) == 4
    assert world.ask("POST", "/api/calls/999/summary") == (404, {"error": "такой записи нет"})
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=30), "Zoom").id, 60.0)
    assert world.ask("POST", f"/api/calls/{waiting.id}/summary") == (409, {"error": "у разговора нет текста"})
    assert len(world.asked) == 4 and status(world)["queued"] == []


def test_a_line_that_stopped_is_told_to_the_page_which_must_say_what_becomes_of_it(world):
    a, b, c, d = (world.call(*TALK, minutes_later=5 * n) for n in range(4))
    refused = SummaryError(Text("Claude ответил ошибкой {status} — см. журнал", status=403))
    world.error = refused
    world.ask("POST", "/api/summaries", {"ids": [a.id, b.id]})
    settled(world)
    told = status(world)
    assert told["stopped"] == {"reason": "Claude ответил ошибкой 403 — см. журнал", "failed": a.id, "waiting": [b.id]}
    assert told["failures"] == {str(a.id): "Claude ответил ошибкой 403 — см. журнал"}
    assert (told["making"], told["queued"]) == ([], [])
    world.error = None
    standing = (409, {"error": "очередь выжимок остановлена", "stopped": True})
    assert world.ask("POST", "/api/summaries", {"ids": [c.id]}) == standing
    assert world.ask("POST", f"/api/calls/{c.id}/summary") == standing and len(world.asked) == 1
    assert world.ask("POST", "/api/summaries", {"ids": [999], "old": "drop"}) == (202, {"taken": []})
    assert status(world)["stopped"] is not None                         # nothing to send: nothing is touched
    assert world.ask("POST", "/api/summaries", {"ids": [c.id], "old": "keep"}) == (202, {"taken": [c.id]})
    settled(world)
    assert [asked["label"] for asked in world.asked] == [a.folder, a.folder, b.folder, c.folder]
    assert status(world)["stopped"] is None
    world.error = refused
    world.ask("POST", "/api/summaries", {"ids": [d.id]})
    settled(world)
    world.error = None
    assert world.ask("POST", f"/api/calls/{c.id}/summary", {"anew": True, "old": "drop"}) == (202, {})
    settled(world)
    assert status(world)["stopped"] is None and world.store.summary(d.id) is None and len(world.asked) == 6


def test_a_line_that_stopped_is_started_again_or_forgotten_on_request(world):
    a, b = world.call(*TALK), world.call(*TALK, minutes_later=5)
    idle = (409, {"error": "очередь выжимок не остановлена"})
    assert world.ask("POST", "/api/summaries/restart") == idle and world.ask("POST", "/api/summaries/reset") == idle
    world.error = SummaryError(Text("Claude не дал выжимку — см. журнал"))
    world.ask("POST", "/api/summaries", {"ids": [a.id, b.id]})
    settled(world)
    assert world.ask("POST", "/api/summaries/restart") == (202, {})     # nothing is better: it stops again
    settled(world)
    assert status(world)["stopped"]["waiting"] == [b.id] and len(world.asked) == 2
    assert world.ask("POST", "/api/summaries/reset") == (200, {})
    assert status(world)["stopped"] is None
    world.ask("POST", "/api/summaries", {"ids": [a.id, b.id]})
    settled(world)
    world.error = None
    assert world.ask("POST", "/api/summaries/restart") == (202, {})
    settled(world)
    assert set(world.store.kinds()) == {a.id, b.id} and status(world)["stopped"] is None


def test_the_line_is_emptied_on_request_and_a_deleted_conversation_leaves_it(world):
    a, b, c, d = (world.call(*TALK, minutes_later=5 * n) for n in range(4))
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.ask("POST", "/api/summaries", {"ids": [a.id, b.id, c.id, d.id]})
    assert wait_for(lambda: status(world)["making"] == [a.id])
    assert world.ask("DELETE", f"/api/calls/{c.id}") == (200, {})
    assert status(world)["queued"] == [b.id, d.id]
    assert world.ask("POST", "/api/summaries/halt") == (200, {"dropped": 2})
    assert status(world)["queued"] == []
    gate.set()
    settled(world)
    assert set(world.store.kinds()) == {a.id} and len(world.asked) == 1 and status(world)["stopped"] is None


def test_a_conversation_in_which_nothing_was_said_is_not_put_into_the_line(world):
    silent, talk = world.call(), world.call(*TALK, minutes_later=5)
    assert world.ask("POST", "/api/summaries", {"ids": [silent.id, talk.id]}) == (202, {"taken": [talk.id]})
    assert world.ask("POST", f"/api/calls/{silent.id}/summary") == (409, {"error": "у разговора нет текста"})
    settled(world)
    assert len(world.asked) == 1 and status(world)["stopped"] is None and status(world)["failures"] == {}


def test_a_request_for_summaries_that_makes_no_sense_sends_nothing(world):
    call = world.call(*TALK)
    for body in ({}, {"ids": []}, {"ids": "1"}, {"ids": [str(call.id)]}, {"ids": [True]}, {"ids": [0]}, {"ids": [-1]},
                 {"ids": [1.0]}, {"ids": [10 ** 20]}, {"ids": list(range(1, 1002))}, {"ids": [call.id], "old": "whatever"},
                 {"ids": [call.id], "old": 1}):
        assert world.ask("POST", "/api/summaries", body)[0] == 400, body
    assert world.ask("POST", f"/api/calls/{call.id}/summary", {"old": "whatever"})[0] == 400
    assert world.asked == [] and status(world)["queued"] == [] and status(world)["making"] == []


def test_one_failed_conversation_is_tried_again_on_request(world):
    failed = world.store.finish(world.store.begin(AT, "Zoom").id, 60.0)
    for _ in range(3):
        world.store.take()
        world.store.missed(failed.id, "boom")
    assert world.ask("POST", f"/api/calls/{failed.id}/retry") == (200, {}) and world.store.get(failed.id).state == RECORDED
    assert world.ask("POST", f"/api/calls/{failed.id}/retry") == (409, {"error": "этот разговор не ждёт повтора"})


def test_a_conversation_is_deleted_on_request_unless_it_is_being_recorded_or_recognised(world, caplog):
    recording = world.store.begin(AT, "Zoom")
    done = world.call(*TALK, minutes_later=5)
    world.store.sound(done).write_bytes(b"OggS")
    world.ask("GET", "/api/calls")
    assert world.ask("DELETE", f"/api/calls/{recording.id}") == (409, {"error": "этот разговор сейчас записывается"})
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=10), "Zoom").id, 60.0)
    world.store.take()
    assert world.ask("DELETE", f"/api/calls/{waiting.id}") == (409, {"error": "этот разговор сейчас распознаётся"})
    with caplog.at_level(logging.INFO, logger="protocallist"):
        assert world.ask("DELETE", f"/api/calls/{done.id}") == (200, {})
    assert world.store.get(done.id) is None and not world.store.sound(done).exists()
    assert "call 2 (2026-10-06_16-00-33_Discord) is deleted from the panel" in caplog.text and "Лицензи" not in caplog.text
    assert world.ask("DELETE", f"/api/calls/{done.id}") == (200, {})     # one that is gone is gone
    held = world.call(*TALK, minutes_later=20)
    world.store.sound(held).write_bytes(b"OggS")
    caplog.clear()
    with open(world.store.sound(held), "rb"), caplog.at_level(logging.WARNING, logger="protocallist"):     # open in a player
        assert world.ask("DELETE", f"/api/calls/{held.id}") == (
            409, {"error": "файлы записи сейчас заняты — закройте то, что их держит, и повторите"})
    assert world.store.get(held.id) is not None and "call 4: its files could not be deleted (PermissionError)" in caplog.text
    assert world.ask("DELETE", f"/api/calls/{held.id}") == (200, {}) and world.store.get(held.id) is None
    assert {row["id"] for row in world.ask("GET", "/api/calls")[1]["calls"]} == {recording.id, waiting.id}


def test_the_file_of_the_sound_is_shown_and_the_recording_that_goes_on_is_dropped_on_request(world):
    call = world.call(*TALK)
    assert world.ask("POST", f"/api/calls/{call.id}/reveal") == (404, {"error": "звука этой записи нет"})
    world.store.sound(call).write_bytes(b"OggS")
    assert world.ask("POST", f"/api/calls/{call.id}/reveal") == (200, {}) and world.revealed == [world.store.sound(call)]
    assert world.ask("POST", "/api/recording/discard") == (200, {}) and world.service.told == ["discard"]


def test_the_recording_to_throw_away_is_the_one_the_page_means(world):
    """Between the two clicks of "stop and delete" one conversation may end and another begin."""
    first = world.store.begin(AT, "Zoom")
    world.store.finish(first.id, 60.0)
    second = world.store.begin(AT + timedelta(minutes=5), "Zoom")
    assert world.ask("POST", "/api/recording/discard", {"id": first.id}) == (409, {"error": "эта запись уже закончилась"})
    assert world.service.told == []
    assert world.ask("POST", "/api/recording/discard", {"id": second.id}) == (200, {}) and world.service.told == ["discard"]


def test_what_cannot_be_asked_is_said_plainly(world):
    assert world.ask("GET", "/api/nothing")[0] == 404 and world.ask("POST", "/api/calls")[0] == 404
    assert world.ask("GET", "/api/calls/abc")[0] == 404 and world.ask("DELETE", "/api/status")[0] == 404
    assert world.ask("GET", "/api/calls/" + "9" * 20)[0] == 404 and world.ask("DELETE", "/api/calls/" + "9" * 5000)[0] == 404
    call = world.call(*TALK)
    world.service.makes_summaries.make(call.id)
    assert world.ask("PUT", f"/api/calls/{call.id}/summary/mine", {"text": "## Коротко\nСам.\n"})[0] == 404
    assert set(world.store.summaries(call.id)) == {MADE}                 # a summary is not written by hand any more
    world.asked.clear()
    assert world.api.handle("POST", f"/api/calls/{call.id}/summary", {}, b"not json").status == 400
    assert world.asked == [] and world.service.makes_summaries.line().making == ()     # and nothing was sent anywhere
    for body in (b"[1]", b"null", b"5", b'"anew"'):                     # JSON, but not what a request is made of
        assert world.api.handle("POST", f"/api/calls/{call.id}/summary", {}, body).status == 400, body
    assert world.asked == [] and world.service.makes_summaries.line().making == ()


# --- recognising anew, with another model

NEW = [Phrase("me", 0.0, 1.5, "Let us begin.")]


def kept(world, *said, **how):
    """A recognised conversation whose sound is kept: only such a one can be recognised anew."""
    call = world.call(*(said or TALK), **how)
    world.store.sound(call).write_bytes(b"OggS")
    return call


def test_the_page_is_told_which_models_the_gateway_has(world):
    world.service.cfg.gateway.url = "http://127.0.0.1:1"                 # a gateway of the user's own choosing
    assert world.ask("GET", "/api/models") == (200, {"models": [
        {"name": "gigaam-v3", "language": "ru", "main": True, "installed": True},
        {"name": ENGLISH, "language": "en", "main": False, "installed": True}]})
    assert world.asked_models == [world.service.cfg.gateway]            # it is looked for where recognition looks for it
    world.clock += 4.9
    world.known = None
    assert len(world.ask("GET", "/api/models")[1]["models"]) == 2 and len(world.asked_models) == 1     # not asked for every page
    world.clock += 0.1
    assert world.ask("GET", "/api/models") == (200, {"models": []}) and len(world.asked_models) == 2   # not there, or an older one
    world.clock += 5
    world.known = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, False)]
    assert [m["installed"] for m in world.ask("GET", "/api/models")[1]["models"]] == [True, False]


def test_a_conversation_is_asked_to_be_recognised_anew_and_the_page_is_told_that_it_waits(world, caplog):
    call = kept(world)
    stamp = status(world)["stamp"]
    with caplog.at_level(logging.INFO, logger="protocallist"):
        assert world.ask("POST", f"/api/calls/{call.id}/recognise", {"model": ENGLISH}) == (202, {})
    assert world.store.get(call.id).redo_model == ENGLISH and status(world)["stamp"] != stamp
    row = world.ask("GET", "/api/calls")[1]["calls"][0]
    assert (row["state"], row["redo"], row["remarks"]) == ("done", ENGLISH, 3)     # its text is there as it was
    data = world.ask("GET", f"/api/calls/{call.id}")[1]
    assert (data["redo"], data["redo_error"], data["model"], len(data["remarks_list"])) == (ENGLISH, "", "gigaam-v3", 3)
    assert f"call {call.id} is to be recognised anew with {ENGLISH}" in caplog.text and "Лицензи" not in caplog.text
    assert world.ask("POST", f"/api/calls/{call.id}/recognise", {"model": "gigaam-v3"}) == (
        409, {"error": "этот разговор уже распознаётся заново"})
    assert world.store.get(call.id).redo_model == ENGLISH
    assert world.ask("DELETE", f"/api/calls/{call.id}") == (200, {}) and world.store.take_redo() is None     # deleted like any other


def test_what_cannot_be_recognised_anew_is_refused_and_says_why(world):
    call = kept(world)
    ask = lambda call_id, body: world.ask("POST", f"/api/calls/{call_id}/recognise", body)
    for body in ({}, {"model": ""}, {"model": 5}, {"model": [ENGLISH]}):
        assert ask(call.id, body)[0] == 400, body
    assert ask(call.id, {"model": "whisper-1"}) == (409, {"error": "такой модели в Толмаче нет"})
    world.clock += 5
    world.known = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, False)]
    assert ask(call.id, {"model": ENGLISH}) == (409, {"error": "модель не установлена в Толмаче"})
    world.clock += 5
    world.known = None
    assert ask(call.id, {"model": ENGLISH}) == (409, {"error": "Толмач не отвечает или в нём нет выбора моделей"})
    world.clock += 5
    world.known = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, True)]
    silent = world.call(*TALK, minutes_later=5)                          # its sound is gone, or was never made small
    assert ask(silent.id, {"model": ENGLISH}) == (409, {"error": "звука этой записи нет"})
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=10), "Zoom").id, 60.0)
    world.store.sound(waiting).write_bytes(b"OggS")
    assert ask(waiting.id, {"model": ENGLISH}) == (409, {"error": "этот разговор ещё не распознан"})
    assert ask(999, {"model": ENGLISH}) == (404, {"error": "такой записи нет"})
    assert world.store.take_redo() is None                               # nothing of all that was put into the line
    assert ask(call.id, {"model": "gigaam-v3"}) == (202, {})             # the main model is a model like any other


def test_the_page_is_told_how_recognition_anew_ended(world):
    call = kept(world)
    told = lambda: world.ask("GET", f"/api/calls/{call.id}")[1]
    stamp = lambda: status(world)["stamp"]
    assert world.ask("GET", "/api/calls")[1]["calls"][0]["remarks"] == 3
    for why, words, english in (
            (transcriber.NO_MODEL, "модель не установлена в Толмаче", "the model is not installed in Tolmach"),
            (transcriber.NO_SOUND, "звука этой записи нет", "this conversation has no sound"),
            (transcriber.NO_CHOICE, "в этом Толмаче нет выбора моделей — нужен Толмач новее",
             "this Tolmach has no choice of models — a newer Tolmach is needed"),
            (transcriber.NO_LOAD, "Толмач не смог загрузить модель — подробности в его журнале",
             "Tolmach could not load the model — see its log for details"),
            (transcriber.NO_WORDS, "модель не услышала ни слова — прежний текст оставлен",
             "the model heard no words — the text of before is kept"),
            (transcriber.NO_ANSWER, "Толмач не ответил за 10 мин", "Tolmach did not answer in 10 min"),
            (transcriber.BAD_SOUND, "звук этой записи не читается", "the sound of this conversation cannot be read"),
            ("the gateway answered 500: boom", "the gateway answered 500: boom", "the gateway answered 500: boom")):
        world.store.redo(call.id, ENGLISH)
        before = stamp()
        world.store.redo_failed(call.id, ENGLISH, why)
        assert (told()["redo"], told()["redo_error"], told()["model"]) == ("", words, "gigaam-v3") and stamp() != before
        i18n.set_language("en")
        assert told()["redo_error"] == english
        i18n.set_language("ru")
    world.store.redo(call.id, ENGLISH)
    before = stamp()
    world.store.redone(call.id, NEW, ENGLISH, AT + timedelta(days=2))
    data = told()
    assert (data["redo"], data["redo_error"], data["model"], data["text_at"]) == ("", "", ENGLISH, local("2026-10-08T15:55:33"))
    assert [r["text"] for r in data["remarks_list"]] == ["Let us begin."] and stamp() != before
    assert world.ask("GET", "/api/calls")[1]["calls"][0]["remarks"] == 1     # counted anew: the text is another one


def test_a_summary_made_before_the_text_was_written_anew_says_so(world):
    call = kept(world)
    world.service.makes_summaries.make(call.id)                          # a day after the conversation
    own(world, call, "## Коротко\nСам.\n")
    stale = lambda: {kind: s["stale"] for kind, s in world.ask("GET", f"/api/calls/{call.id}")[1]["summaries"].items()}
    assert stale() == {MADE: False, MINE: False}
    world.store.redo(call.id, ENGLISH)
    assert stale() == {MADE: False, MINE: False}                         # asked for only: the text is the one they were made by
    world.store.redone(call.id, NEW, ENGLISH, AT + timedelta(days=2))
    assert stale() == {MADE: True, MINE: True}
    world.now = AT + timedelta(days=3)
    world.service.makes_summaries.make(call.id, anew=True)
    assert stale() == {MADE: False, MINE: True}
    world.store._db.execute("UPDATE calls SET text_at = NULL WHERE id = ?", (call.id,))     # an archive that does not know when
    assert stale() == {MADE: False, MINE: False}


def test_the_models_are_asked_of_the_gateway_recognition_would_find(world, monkeypatch):
    class Found:
        def models(self):
            return [gateway.Model("gigaam-v3", "ru", True, True)]

    looked = []
    monkeypatch.setattr(gateway, "connect", lambda cfg: looked.append(cfg) or Found())
    cfg = world.service.cfg.gateway
    assert asked.models(cfg) == [gateway.Model("gigaam-v3", "ru", True, True)] and looked == [cfg]

    def nobody(cfg):
        raise gateway.Unavailable(gateway.DOWN, "nobody listens")

    monkeypatch.setattr(gateway, "connect", nobody)
    assert asked.models(cfg) is None                                     # no gateway: no choice of models, and no failure
    assert Api(world.service)._models is asked.models                    # this is what a panel asks by, unless told otherwise


def test_what_was_asked_for_and_ended_between_two_looks_of_the_page_is_still_news(world):
    call = kept(world)
    stamps = [status(world)["stamp"]]
    world.store.redo(call.id, ENGLISH)
    world.store.redo_failed(call.id, ENGLISH, "the gateway answered 500: boom")   # asked and refused before the page looked again
    stamps.append(status(world)["stamp"])
    for day in (2, 3):                           # ...and recognised anew before it looked: twice, the second time nothing but the text changes
        world.store.redo(call.id, ENGLISH)
        world.store.redone(call.id, NEW, ENGLISH, AT + timedelta(days=day))
        stamps.append(status(world)["stamp"])
    assert len(set(stamps)) == 4


def test_a_gateway_slow_to_answer_is_not_asked_again_the_moment_it_has_answered(world):
    world.slow = 10.0                            # longer than what it says is remembered for
    world.ask("GET", "/api/models")
    world.clock += 4.9
    world.ask("GET", "/api/models")
    assert len(world.asked_models) == 1
    world.clock += 0.1
    world.ask("GET", "/api/models")
    assert len(world.asked_models) == 2


def test_a_request_to_recognise_anew_is_taken_back_on_request(world, caplog):
    call = kept(world)
    path = f"/api/calls/{call.id}/recognise"
    assert world.ask("DELETE", path) == (409, {"error": "этот разговор не ждёт распознавания заново"})
    world.ask("POST", path, {"model": ENGLISH})
    with caplog.at_level(logging.INFO, logger="protocallist"):
        assert world.ask("DELETE", path) == (200, {})
    assert world.store.get(call.id).redo_model is None and world.ask("GET", f"/api/calls/{call.id}")[1]["redo"] == ""
    assert f"call {call.id} is not to be recognised anew after all" in caplog.text and "Лицензи" not in caplog.text
    assert world.ask("DELETE", "/api/calls/999/recognise")[0] == 409
    world.service.store = None
    assert world.ask("DELETE", path)[0] == 409


def test_a_summary_that_was_being_made_while_the_text_was_written_anew_is_one_of_the_text_before(world):
    call = kept(world)
    world.store.redo(call.id, ENGLISH)
    world.now = AT + timedelta(days=2)

    def written_anew():                          # "Claude" is still thinking - and the new text is there
        world.store.redone(call.id, NEW, ENGLISH, world.now + timedelta(seconds=30))
        world.now += timedelta(minutes=1)

    world.during = written_anew
    world.service.makes_summaries.make(call.id)
    world.during = None
    assert world.ask("GET", f"/api/calls/{call.id}")[1]["summaries"][MADE]["stale"] is True


def test_a_summary_is_of_the_moment_before_its_text_was_read(world):
    """Between reading the text and beginning the summary the text may be written anew - by the thread that
    recognises, which waits for the same archive."""
    call = kept(world)
    world.store.redo(call.id, ENGLISH)
    world.now = AT + timedelta(days=2)
    read = world.store.phrases

    def read_and_then_replaced(call_id):
        said = read(call_id)
        if world.store.get(call_id).redo_model:
            world.store.redone(call_id, NEW, ENGLISH, world.now + timedelta(seconds=30))
            world.now += timedelta(minutes=1)
        return said

    world.store.phrases = read_and_then_replaced
    world.service.makes_summaries.make(call.id)
    world.store.phrases = read
    assert world.asked[-1]["text"].count("Начнём") == 1                  # made of the text of before...
    assert world.ask("GET", f"/api/calls/{call.id}")[1]["summaries"][MADE]["stale"] is True     # ...and marked so


def test_a_server_of_ones_own_is_not_called_tolmach(world):
    assert world.ask("GET", "/api/status")[1]["own"] is False            # the page is told who is asked for the text
    world.service.cfg.gateway.url = "http://127.0.0.1:8000"
    assert world.ask("GET", "/api/status")[1]["own"] is True
    call = kept(world)
    ask = lambda body: world.ask("POST", f"/api/calls/{call.id}/recognise", body)
    assert ask({"model": "whisper-1"}) == (409, {"error": "такой модели у сервиса распознавания нет"})
    world.clock += 5
    world.known = None
    assert ask({"model": ENGLISH}) == (409, {"error": "сервис распознавания не отвечает или не говорит, какие у него модели"})
    told = lambda: world.ask("GET", f"/api/calls/{call.id}")[1]["redo_error"]
    for why, words, english in (
            (transcriber.NO_MODEL, "такой модели у сервиса распознавания нет", "the recognition service has no such model"),
            (transcriber.NO_CHOICE, "сервис распознавания не говорит, какие у него модели",
             "the recognition service does not say which models it has"),
            (transcriber.NO_LOAD, "сервис распознавания не смог загрузить модель", "the recognition service could not load the model"),
            (transcriber.NO_ANSWER, "сервис распознавания не ответил за 10 мин", "the recognition service did not answer in 10 min"),
            (transcriber.NO_WORDS, "модель не услышала ни слова — прежний текст оставлен",
             "the model heard no words — the text of before is kept")):
        world.store.redo(call.id, ENGLISH)
        world.store.redo_failed(call.id, ENGLISH, why)
        assert told() == words
        i18n.set_language("en")
        assert told() == english
        i18n.set_language("ru")
    world.service.cfg.gateway.url = ""                                   # Tolmach again: called by its name again
    assert told() == "модель не услышала ни слова — прежний текст оставлен"
    world.store.redo(call.id, ENGLISH)
    world.store.redo_failed(call.id, ENGLISH, transcriber.NO_ANSWER)
    assert told() == "Толмач не ответил за 10 мин"
