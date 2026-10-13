"""A conversation as a text for reading."""
from datetime import datetime

from protocallist import i18n, paths, transcript
from protocallist.store import Phrase, Store

TALK = [Phrase("me", 5.2, 7.0, "добрый день"), Phrase("me", 7.4, 9.0, "слышно меня"),
        Phrase("them", 19.9, 21.0, "да слышно"), Phrase("me", 3725.0, 3726.0, "тогда до завтра")]


def a_call(seconds: float = 47 * 60 + 10):
    store = Store(paths.database_file(), paths.calls_dir())
    try:
        call = store.begin(datetime(2026, 10, 5, 16, 11, 51), "Zoom")
        return store.finish(call.id, seconds)
    finally:
        store.close()


def test_a_conversation_reads_as_remarks_under_a_heading():
    assert transcript.render(a_call(), TALK).split("\n") == [
        "# Разговор 05.10.2026 16:11 — Zoom, 47 мин", "",
        "**[00:00:05] Я:** добрый день слышно меня", "",          # two phrases in a row from one side: one remark
        "**[00:00:19] Собеседники:** да слышно", "",
        "**[01:02:05] Я:** тогда до завтра", ""]


def test_the_two_sides_alternate_however_short_a_remark_is():
    talk = [Phrase("me", 1.0, 9.0, "первое"), Phrase("them", 4.0, 4.5, "угу"), Phrase("me", 9.5, 12.0, "второе")]
    assert [who for who, when, what in transcript.remarks(talk)] == ["me", "them", "me"]
    assert transcript.remarks([]) == []


def test_a_conversation_of_seconds_is_still_a_minute_and_one_without_words_is_only_a_heading():
    assert transcript.render(a_call(12.0), []) == "# Разговор 05.10.2026 16:11 — Zoom, 1 мин\n"


def test_the_heading_and_the_names_are_in_the_language_of_the_interface():
    i18n.set_language("en")
    lines = transcript.render(a_call(), TALK).split("\n")
    assert lines[0] == "# Conversation 05.10.2026 16:11 — Zoom, 47 min"
    assert lines[2].startswith("**[00:00:05] Me:**") and lines[4].startswith("**[00:00:19] Others:**")
