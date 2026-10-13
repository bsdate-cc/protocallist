"""The summaries of an archive: made on request, kept in the database, the one in force copied beside the sound.
The archive is real; what makes a summary is a stand-in."""
import logging
import threading
import time
from datetime import datetime, timedelta

import pytest

from protocallist import paths, summaries
from protocallist.config import SummaryConfig
from protocallist.i18n import Text
from protocallist.store import MADE, MINE, Phrase, Store
from protocallist.summaries import Summariser
from protocallist.summary import SummaryError

AT = datetime(2026, 10, 6, 15, 55, 33)
ANSWER = "# Выжимка\n\n## Коротко\nГоворили о плане.\n"


class World:
    def __init__(self) -> None:
        self.store = Store(paths.database_file(), paths.calls_dir())
        self.asked: list[dict] = []
        self.names: dict[str, int] = {}      # what the log calls a conversation: its number
        self.answer = ANSWER
        self.error: Exception | None = None
        self.during = None                  # what happens in the world while the summary is being made
        self.now, self.seconds = AT + timedelta(hours=6), 1000.0
        self.settings = SummaryConfig(model="sonnet")
        self.s = self.summariser()

    def summariser(self) -> Summariser:
        return Summariser(self.store, self.settings, language=lambda: "ru", ask=self._ask, today=lambda: self.now,
                          clock=lambda: self.seconds)

    def _ask(self, text, model, language, **how):
        self.asked.append(dict(text=text, model=model, language=language, **how))
        if self.during is not None:
            self.during()
        self.seconds += 64.2
        if self.error is not None:
            raise self.error
        return self.answer, f"{model}" if not how["command"] else "my-tool -"

    def call(self, *said: str, minutes_later: int = 0, program: str = "Discord"):
        call = self.store.begin(AT + timedelta(minutes=minutes_later), program)
        self.store.finish(call.id, 60.0)
        self.store.take()
        done = self.store.done(call.id, [Phrase("me" if i % 2 == 0 else "them", i * 5.0, i * 5.0 + 4, text)
                                         for i, text in enumerate(said)], "gigaam-v3")
        self.names[done.folder] = done.id
        return done

    def beside(self) -> list[str]:
        """What lies in the folder of all conversations, beside their working folders."""
        return sorted(p.name for p in paths.calls_dir().iterdir() if p.is_file())


@pytest.fixture
def world():
    w = World()
    yield w
    w.store.close()


def own(world, call, text: str):
    """A summary of the user's own, as an archive may hold one: nothing makes them any more, but one that is
    there stays in force."""
    world.store.save_summary(call.id, MINE, text, world.now)
    return summaries.settle(world.store, call)


def test_a_summary_is_made_from_the_text_of_the_archive_and_kept_there(world):
    call = world.call("начнём с плана", "давайте")
    path = world.s.make(call.id)
    asked, = world.asked
    assert "**[00:00:00] Я:** начнём с плана" in asked["text"] and "давайте" in asked["text"]     # as it would be read
    assert (asked["model"], asked["language"], asked["command"]) == ("sonnet", "ru", [])
    assert asked["where"] == paths.calls_dir() and asked["label"] == "2026-10-06_15-55-33_Discord"
    made = world.store.summary(call.id)
    assert (made.kind, made.text, made.maker, made.seconds) == (MADE, ANSWER, "sonnet", 64.2)
    assert made.saved_at.startswith("2026-10-06T21:55:33")
    assert path == world.store.summary_file(call) == paths.calls_dir() / "2026-10-06_15-55-33_Discord.md"
    assert path.read_bytes().decode("utf-8") == ANSWER and world.beside() == ["2026-10-06_15-55-33_Discord.md"]
    assert world.store.get(call.id).topic == "Говорили о плане."


def test_a_command_of_ones_own_gets_the_text_instead(world):
    world.settings.command = ["my-tool", "-"]
    call = world.call("начнём с плана")
    world.s.make(call.id)
    assert world.asked[0]["command"] == ["my-tool", "-"] and world.store.summary(call.id).maker == "my-tool -"


def test_instructions_named_in_the_settings_go_with_every_request(world):
    call = world.call("начнём с плана")
    world.s.make(call.id)
    assert world.asked[0]["instructions"] is None                        # none named: those the program comes with
    world.settings.prompt = "мои указания.md"                            # beside the settings
    world.s.make(call.id, anew=True)
    assert world.asked[1]["instructions"] == paths.home() / "мои указания.md"
    world.settings.prompt = str(paths.calls_dir() / "own.md")            # or wherever the user keeps them
    world.s.make(call.id, anew=True)
    assert world.asked[2]["instructions"] == paths.calls_dir() / "own.md"


def test_a_conversation_that_has_a_summary_is_not_sent_again_unless_asked_anew(world):
    call = world.call("начнём с плана")
    world.s.make(call.id)
    world.store.summary_file(call).unlink()                             # the file was deleted by hand
    assert world.s.make(call.id).read_text(encoding="utf-8") == ANSWER  # written again from the archive
    assert len(world.asked) == 1
    world.answer = "## Коротко\nВторой вариант.\n"
    world.s.make(call.id, anew=True)
    assert len(world.asked) == 2 and world.store.summary(call.id).text == "## Коротко\nВторой вариант.\n"
    assert world.store.summary_file(call).read_text(encoding="utf-8") == "## Коротко\nВторой вариант.\n"


def test_the_users_own_summary_is_the_one_in_force_and_no_summary_made_anew_touches_it(world):
    call = world.call("начнём с плана")
    world.s.make(call.id)
    path = own(world, call, "## Коротко\nПереезд в четверг.\n")
    assert path.read_text(encoding="utf-8") == "## Коротко\nПереезд в четверг.\n"
    assert world.store.get(call.id).topic == "Переезд в четверг."
    written = path.stat().st_mtime_ns
    world.answer = "## Коротко\nЕщё один вариант от модели.\n"
    assert world.s.make(call.id, anew=True) == path
    both = world.store.summaries(call.id)
    assert both[MINE].text == "## Коротко\nПереезд в четверг.\n" and both[MADE].text == world.answer
    assert path.read_text(encoding="utf-8") == "## Коротко\nПереезд в четверг.\n" and path.stat().st_mtime_ns == written
    assert world.store.get(call.id).topic == "Переезд в четверг."
    assert world.s.make(call.id) == path and len(world.asked) == 2      # there is one: nothing is sent


def test_nothing_makes_a_summary_of_the_users_own_any_more(world):
    assert not hasattr(world.s, "save_mine")


def test_a_summary_that_was_not_made_changes_nothing(world):
    call = world.call("начнём с плана")
    world.s.make(call.id)
    world.error = SummaryError("Claude не ответил")
    with pytest.raises(SummaryError):
        world.s.make(call.id, anew=True)
    assert world.store.summary(call.id).text == ANSWER
    assert world.store.summary_file(call).read_text(encoding="utf-8") == ANSWER
    assert world.s.line().making == () and world.beside() == ["2026-10-06_15-55-33_Discord.md"]


def test_a_conversation_without_text_is_not_sent_anywhere(world):
    waiting = world.store.begin(AT, "Zoom")
    world.store.finish(waiting.id, 60.0)
    silent = world.call(minutes_later=5)
    for call_id in (waiting.id, silent.id, 999):
        with pytest.raises(SummaryError, match="у разговора нет текста"):
            world.s.make(call_id)
    assert world.asked == [] and world.beside() == []


def test_one_summary_is_not_made_twice_at_a_time_but_two_conversations_may_be_at_once(world):
    first, second = world.call("начнём с плана"), world.call("другой разговор", minutes_later=5)
    seen = []

    def meanwhile():
        seen.append(world.s.line().making)
        if len(world.asked) == 1:
            with pytest.raises(SummaryError, match="уже делается"):
                world.s.make(first.id, anew=True)
            world.s.make(second.id)

    world.during = meanwhile
    world.s.make(first.id)
    assert seen == [(first.id,), (first.id, second.id)] and not hasattr(world.s, "making") and not hasattr(world.s, "failures")
    assert len(world.asked) == 2 and world.s.line().making == ()
    assert set(world.store.kinds()) == {first.id, second.id}


def test_a_conversation_deleted_while_its_summary_was_made_gets_none(world):
    call = world.call("начнём с плана")
    world.during = lambda: world.store.discard(call.id)
    with pytest.raises(SummaryError, match="выжимка не сохранена"):
        world.s.make(call.id)
    assert world.beside() == [] and world.s.line().making == ()


def test_a_conversation_that_is_gone_gets_no_file_beside_a_sound_it_no_longer_has(world):
    call = world.call("начнём с плана")
    world.store.discard(call.id)
    assert summaries.settle(world.store, call) == world.store.summary_file(call) and world.beside() == []


def test_two_threads_that_settle_one_conversation_do_not_trip_over_the_half_file(world, monkeypatch):
    call = world.call("начнём с плана")
    world.store.save_summary(call.id, MADE, ANSWER, AT, "opus", 60.0)
    replace, at_once, failures, together = summaries.os.replace, [0, 0], [], threading.Barrier(4)

    def slow(src, dst):
        at_once[0] += 1
        at_once[1] = max(at_once)                # how many were replacing the file at the same moment
        threading.Event().wait(0.05)             # long enough for the others to come
        try:
            replace(src, dst)
        finally:
            at_once[0] -= 1

    def one():
        try:
            together.wait(5)
            summaries.settle(world.store, call)
        except Exception as e:                   # the test must see it, not the thread's stderr
            failures.append(e)

    with monkeypatch.context() as patch:
        patch.setattr(summaries.os, "replace", slow)
        patch.setattr(summaries.Path, "read_text", lambda self, **how: "what lay there before")     # each has a file to write
        threads = [threading.Thread(target=one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    assert failures == [] and at_once == [0, 1]
    assert world.store.summary_file(call).read_text(encoding="utf-8") == ANSWER and world.beside() == [f"{call.folder}.md"]


def test_the_file_beside_the_sound_is_what_is_in_force_when_it_is_written(world, monkeypatch):
    """Between the look at the summary in force and the writing of the file another thread may keep
    the user's own. The file must end as that - not as what was in force a moment before."""
    call = world.call("начнём с плана")
    world.store.save_summary(call.id, MADE, ANSWER, AT, "opus", 60.0)
    look, looks, other = world.store.summary, [], []

    def the_user_saves():
        world.store.save_summary(call.id, MINE, "## Коротко\nСам.\n", AT)
        summaries.settle(world.store, call)

    def looked(call_id):
        found = look(call_id)
        looks.append(call_id)
        if len(looks) == 2:                 # the look for the file (the first one was for the topic): at this very moment...
            other.append(threading.Thread(target=the_user_saves))
            other[0].start()
            other[0].join(0.5)              # ...the user's own is kept, and settled if nothing holds that back
        return found

    with monkeypatch.context() as patch:
        patch.setattr(world.store, "summary", looked)
        summaries.settle(world.store, call)
        other[0].join(5)
    assert world.store.summary_file(call).read_text(encoding="utf-8") == "## Коротко\nСам.\n"


def test_a_conversation_deleted_at_the_moment_its_file_is_written_leaves_no_file(world, monkeypatch):
    call = world.call("начнём с плана")
    world.store.save_summary(call.id, MADE, ANSWER, AT, "opus", 60.0)
    look, looks = world.store.summary, []

    def looked(call_id):
        found = look(call_id)
        looks.append(call_id)
        if len(looks) == 2:                 # the look for the file: and right after it the conversation is forgotten
            world.store.discard(call.id)
        return found

    with monkeypatch.context() as patch:
        patch.setattr(world.store, "summary", looked)
        summaries.settle(world.store, call)
    assert world.beside() == [] and world.store.get(call.id) is None


def test_a_summary_with_the_line_ends_of_windows_is_not_written_again_while_it_is_the_same(world, monkeypatch):
    call = world.call("начнём с плана")
    own(world, call, "## Коротко\r\nСам.\r\n")
    assert world.store.summary_file(call).read_bytes() == "## Коротко\r\nСам.\r\n".encode("utf-8")
    written = []
    with monkeypatch.context() as patch:
        patch.setattr(summaries.os, "replace", lambda src, dst: written.append(dst))
        summaries.settle(world.store, call)
    assert written == [] and world.beside() == [f"{call.folder}.md"]


def test_an_archive_that_is_closed_is_a_reason_that_is_said_not_an_error_nobody_expects(world):
    call = world.call("начнём с плана")
    world.store.close()
    for asked in (lambda: world.s.make(call.id), lambda: world.s.make(call.id, anew=True)):
        with pytest.raises(SummaryError, match="архив не открыт"):
            asked()
    assert world.s.line().making == () and world.asked == []


def test_the_users_own_summary_survives_a_summary_made_anew_that_fails(world):
    call = world.call("начнём с плана")
    world.s.make(call.id)
    own(world, call, "## Коротко\nСам.\n")
    world.error = SummaryError(Text("Claude не ответил за {minutes} мин", minutes=15))
    with pytest.raises(SummaryError):
        world.s.make(call.id, anew=True)
    both = world.store.summaries(call.id)
    assert (both[MINE].text, both[MADE].text) == ("## Коротко\nСам.\n", ANSWER)      # an attempt that failed touches neither
    assert world.store.summary_file(call).read_text(encoding="utf-8") == "## Коротко\nСам.\n"


def test_an_archive_closed_while_a_summary_was_made_is_said_not_thrown(world):
    call = world.call("начнём с плана")
    world.during = world.store.close
    with pytest.raises(SummaryError, match="выжимка не сохранена: ProgrammingError"):
        world.s.make(call.id)


def test_nothing_of_a_summary_reaches_the_log(world, caplog):
    call = world.call("начнём с плана")
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        world.s.make(call.id)
        world.s.send([call.id], first=True, anew=True)                  # ...nor by way of the line, made or not
        settled(world)
        world.error = FAILURE
        world.s.send([call.id], first=True, anew=True)
        settled(world)
    assert "план" not in caplog.text and "Коротко" not in caplog.text and "stands still" in caplog.text


def test_the_file_beside_the_sound_is_the_summary_in_force_whole_or_not_at_all(world, monkeypatch):
    call = world.call("начнём с плана")
    world.s.make(call.id)

    def full(src, dst):
        raise OSError(28, "No space left on device")

    world.answer = "## Коротко\nПереезд в четверг.\n"
    with monkeypatch.context() as patch:
        patch.setattr(summaries.os, "replace", full)
        with pytest.raises(SummaryError, match="выжимка сохранена, но файл рядом со звуком не записан"):
            world.s.make(call.id, anew=True)
    assert world.store.summary(call.id).text == world.answer            # the archive has it; the file is the older one
    assert world.store.summary_file(call).read_text(encoding="utf-8") == ANSWER
    assert summaries.settle(world.store, call).read_text(encoding="utf-8") == "## Коротко\nПереезд в четверг.\n"


def test_two_threads_may_ask_for_summaries_of_one_archive(world):
    calls = [world.call(f"разговор {n}", minutes_later=n) for n in range(6)]
    threads = [threading.Thread(target=world.s.make, args=(call.id,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert set(world.store.kinds()) == {call.id for call in calls} and len(world.beside()) == 6


# --- the line: conversations sent to be summarised one after another, for a page that does not wait


FAILURE = SummaryError(Text("Claude не ответил за {minutes} мин", minutes=15))
NOTHING = summaries.Line((), (), None, {})


def wait_for(what, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not what() and time.monotonic() < deadline:
        time.sleep(0.005)
    return bool(what())


def settled(world, seconds: float = 5.0) -> None:
    """Wait until no summary is being made and none waits its turn."""
    def still() -> bool:
        line = world.s.line()
        return not line.making and not line.queued

    assert wait_for(still, seconds)


def sent(world) -> list[int]:
    """The conversations whose text was sent, in the order it was."""
    return [world.names[asked["label"]] for asked in world.asked]


def talks(world, how_many: int) -> list[int]:
    return [world.call(f"разговор {n}", minutes_later=n).id for n in range(how_many)]


def test_conversations_sent_together_are_summarised_one_at_a_time_each_in_its_turn(world):
    a, b, c = talks(world, 3)
    gate, seen = threading.Event(), []

    def meanwhile():
        seen.append(world.s.line())
        gate.wait(5)

    world.during = meanwhile
    assert world.s.send([a, b, c, a]) == [a, b, c]                       # each once, however often it is named
    assert wait_for(lambda: len(seen) == 1) and (seen[0].making, seen[0].queued) == ((a,), (b, c))
    assert world.s.send([a, b, c]) == [] and world.s.send([c, c]) == []      # nothing is sent twice
    gate.set()
    settled(world)
    assert [(line.making, line.queued) for line in seen] == [((a,), (b, c)), ((b,), (c,)), ((c,), ())]
    assert sent(world) == [a, b, c] and set(world.store.kinds()) == {a, b, c} and world.s.line() == NOTHING


def test_what_is_sent_while_the_line_goes_joins_its_end_and_what_is_asked_for_alone_goes_next(world):
    a, b, c, d = talks(world, 4)
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.s.send([a, b])
    assert wait_for(lambda: world.s.line().making == (a,))
    assert world.s.send([c]) == [c] and world.s.line().queued == (b, c)
    assert world.s.send([d], first=True) == [d] and world.s.line().queued == (d, b, c)
    assert world.s.send([c], first=True) == [c] and world.s.line().queued == (c, d, b)     # one that waits is moved, not doubled
    assert world.s.send([a], first=True) == [] and world.s.line().queued == (c, d, b)      # the one being made is left alone
    gate.set()
    settled(world)
    assert sent(world) == [a, c, d, b]


def test_the_first_summary_that_fails_stops_the_line_and_the_rest_is_not_sent_anywhere(world, caplog):
    a, b, c = talks(world, 3)
    world.error = FAILURE
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        world.s.send([a, b, c])
        settled(world)
    line = world.s.line()
    assert (str(line.stopped.reason), line.stopped.failed, line.stopped.waiting) == ("Claude не ответил за 15 мин", a, (b, c))
    assert {call_id: str(why) for call_id, why in line.failures.items()} == {a: "Claude не ответил за 15 мин"}
    assert sent(world) == [a] and world.store.kinds() == {}
    assert "summary of call 1 was not made: Claude did not answer in 15 min" in caplog.text     # the log is English
    assert "the line of summaries stands still: 2 more were not sent" in caplog.text


def test_a_line_that_stands_still_is_started_again_or_forgotten(world):
    a, b, c, d = talks(world, 4)
    world.error = FAILURE
    world.s.send([a, b])
    settled(world)
    world.error = None
    assert world.s.restart() is True
    settled(world)
    assert sent(world) == [a, a, b] and world.s.line() == NOTHING        # why it stood is forgotten with it
    assert world.s.restart() is False and world.s.reset() is False       # nothing stands still
    world.error = FAILURE
    world.s.send([c, d])
    settled(world)
    assert world.s.reset() is True
    line = world.s.line()
    assert line.stopped is None and line.queued == () and sent(world) == [a, a, b, c]
    assert set(line.failures) == {c}                                     # the page of that conversation still says why
    world.error = None
    world.s.send([c])
    settled(world)
    assert world.s.line() == NOTHING                                     # ...until it is sent again


def test_what_is_sent_while_the_line_stands_still_must_say_what_becomes_of_it(world):
    a, b, c, d, e, f = talks(world, 6)
    world.error = FAILURE
    world.s.send([a, b])
    settled(world)
    world.error = None
    assert world.s.send([c]) is None and world.s.send([c], first=True) is None       # not said: nothing happens
    assert world.s.line().stopped is not None and sent(world) == [a]
    assert world.s.send([c, b], old="keep") == [c]                       # the line starts again, what is new at its end
    settled(world)
    assert sent(world) == [a, a, b, c] and world.s.line() == NOTHING
    world.error = FAILURE
    world.s.send([d, e])
    settled(world)
    world.error = None
    assert world.s.send([f], old="drop") == [f]                          # the line that stood is forgotten
    settled(world)
    assert sent(world) == [a, a, b, c, d, f] and set(world.store.kinds()) == {a, b, c, f}
    world.error = FAILURE
    world.s.send([d, e], old="drop")                                     # nothing stands: said or not, it is all the same
    settled(world)
    world.error = None
    assert world.s.send([e], first=True, old="keep") == [e]              # one of the line itself, asked for alone: it goes first
    settled(world)
    assert sent(world)[-3:] == [d, e, d] and set(world.store.kinds()) == {a, b, c, d, e, f}


def test_the_line_can_be_emptied_while_it_goes_and_what_is_being_made_is_finished(world):
    a, b, c = talks(world, 3)
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.s.send([a, b, c])
    assert wait_for(lambda: world.s.line().making == (a,))
    assert world.s.halt() == 2 and world.s.line().queued == () and world.s.halt() == 0
    gate.set()
    settled(world)
    assert sent(world) == [a] and set(world.store.kinds()) == {a} and world.s.line() == NOTHING


def test_a_conversation_that_is_gone_stops_nothing(world):
    a, b = talks(world, 2)
    world.during = lambda: world.store.discard(a) if len(world.asked) == 1 else None     # deleted while its summary was made
    world.s.send([a, 999, b])                                           # ...and one that never was
    settled(world)
    assert sent(world) == [a, b] and set(world.store.kinds()) == {b} and world.s.line() == NOTHING


def test_whoever_makes_the_summaries_failing_stops_the_line_even_if_that_conversation_is_deleted_meanwhile(world):
    a, b, c, d = talks(world, 4)
    world.error = FAILURE
    world.during = lambda: world.store.discard(a) if len(world.asked) == 1 else None
    world.s.send([a, b, c])
    settled(world)
    line = world.s.line()
    assert sent(world) == [a]                                            # the next one is not sent to what does not answer
    assert (str(line.stopped.reason), line.stopped.failed, line.stopped.waiting, line.failures) == \
        ("Claude не ответил за 15 мин", 0, (b, c), {})
    world.s.reset()
    world.during = lambda: world.store.discard(d)
    world.s.send([d])                                                   # ...and with nothing else waiting, nothing stands
    settled(world)
    assert world.s.line() == NOTHING


def test_a_reason_that_cannot_be_put_into_words_does_not_leave_the_line_dead(world, caplog):
    a, b, c = talks(world, 3)
    world.error = SummaryError(Text("причина с дырой {oops}"))          # a text with a hole that nothing fills
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.s.send([a, b])
        settled(world, 2.0)
    line = world.s.line()
    assert str(line.stopped.reason) == "выжимка не получилась — см. журнал" and line.stopped.waiting == (b,)
    assert "Traceback" in caplog.text
    world.error = None
    assert world.s.send([c], old="drop") == [c]                          # and the line goes on working
    settled(world)
    assert sent(world) == [a, c]


def test_an_archive_closed_while_the_line_goes_stops_it_and_sends_nothing_more(world):
    a, b = talks(world, 2)
    world.during = world.store.close
    world.s.send([a, b])
    settled(world)
    line = world.s.line()
    assert sent(world) == [a] and str(line.stopped.reason).startswith("выжимка не сохранена") and line.stopped.waiting == (b,)


def test_a_conversation_that_is_deleted_leaves_the_line(world):
    a, b, c, d, e = talks(world, 5)
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.s.send([a, b])
    assert wait_for(lambda: world.s.line().making == (a,))
    world.s.drop(b)
    assert world.s.line().queued == ()
    gate.set()
    settled(world)
    world.error = FAILURE
    world.s.send([c, d, e])
    settled(world)
    world.s.drop(d)
    assert (world.s.line().stopped.failed, world.s.line().stopped.waiting) == (c, (e,))
    world.s.drop(c)
    line = world.s.line()
    assert (line.stopped.failed, line.stopped.waiting, line.failures) == (0, (e,), {})
    world.s.drop(e)
    assert world.s.line() == NOTHING                                     # nothing is left of the line that stood


def test_whatever_goes_wrong_in_the_line_is_said_and_nothing_is_left_as_being_made(world, caplog):
    a, b = talks(world, 2)
    world.error = RuntimeError("what nobody foresaw")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        world.s.send([a, b])
        settled(world)
    line = world.s.line()
    assert str(line.stopped.reason) == "выжимка не получилась — см. журнал" and "Traceback" in caplog.text
    assert (line.stopped.failed, line.stopped.waiting, line.making) == (a, (b,), ())


def test_a_conversation_in_the_line_is_sent_only_if_it_has_no_summary_or_one_is_asked_anew(world):
    a, = talks(world, 1)
    world.s.send([a])
    settled(world)
    assert world.store.summary(a).text == ANSWER
    world.answer = "## Коротко\nВторой вариант.\n"
    world.s.send([a])                                                   # it has one: nothing is sent anywhere
    settled(world)
    assert world.store.summary(a).text == ANSWER and sent(world) == [a]
    world.s.send([a], first=True, anew=True)
    settled(world)
    assert world.store.summary(a).text == "## Коротко\nВторой вариант.\n" and sent(world) == [a, a]
    b = world.call("другой разговор", minutes_later=9).id
    gate = threading.Event()
    world.during = lambda: gate.wait(5)
    world.s.send([b])
    assert wait_for(lambda: world.s.line().making == (b,))
    world.s.send([a], first=True, anew=True)
    world.s.send([a], first=True)                                       # asked for once more, plainly: still to be made anew
    gate.set()
    settled(world)
    assert sent(world) == [a, a, b, a]


def test_the_line_and_a_summary_made_directly_do_not_make_one_conversation_twice(world):
    a, b = talks(world, 2)
    gates = {True: threading.Event(), False: threading.Event()}         # for the thread of the line; for any other
    world.during = lambda: gates[threading.current_thread().name == "summaries"].wait(5)
    world.s.send([a, b])
    assert wait_for(lambda: world.s.line().making == (a,))
    direct = threading.Thread(target=world.s.make, args=(b,))           # b waits in the line - and is made directly meanwhile
    direct.start()
    assert wait_for(lambda: world.s.line().making == (a, b))
    assert world.s.send([b]) == []
    gates[True].set()                                                    # a is done: the line comes to b, which is being made
    assert wait_for(lambda: world.s.line() == summaries.Line((b,), (), None, {}))
    gates[False].set()
    direct.join(5)
    settled(world)
    assert sent(world) == [a, b] and world.s.line() == NOTHING


def test_who_gets_the_text_can_be_asked_before_anything_is_sent(world):
    assert world.s.target() == ("claude", "sonnet")
    world.settings.command = ["python", "C:/scripts/my_summary.py"]
    assert world.s.target() == ("command", "python my_summary.py")
