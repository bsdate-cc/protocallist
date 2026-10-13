"""What a conversation is about, in one line: from its summary, else from its beginning."""
from datetime import datetime

import pytest

from protocallist import paths, topic
from protocallist.store import MADE, MINE, Phrase, Store

AT = datetime(2026, 10, 6, 12, 0, 0)
SUMMARY = """# Выжимка: 06.10.2026 15:55 — Discord, 74 мин

## Коротко
Договорились перенести занятия кружка в большой зал
со следующего четверга.

Расписание остаётся прежним.

## Решения
- [00:24:10] Объявление вешаем в пятницу.
"""


@pytest.fixture
def store():
    s = Store(paths.database_file(), paths.calls_dir())
    yield s
    s.close()


def recognised(store: Store, *said: str):
    call = store.begin(AT, "Zoom")
    store.finish(call.id, 60.0)
    store.take()
    return store.done(call.id, [Phrase("me" if i % 2 == 0 else "them", i * 5.0, i * 5.0 + 4, text)
                                for i, text in enumerate(said)], "gigaam-v3")


def test_a_summary_is_about_its_first_paragraph():
    assert topic.of_summary(SUMMARY) == "Договорились перенести занятия кружка в большой зал со следующего четверга."
    assert topic.of_summary("# Заметки\n\nСначала главное.\n\n## Коротко\nПотом остальное.\n") == "Сначала главное."


def test_a_first_section_that_is_a_list_gives_its_first_point_without_the_mark_and_the_time():
    assert topic.of_summary("# Выжимка\n\n## Решения\n\n- [00:24:10] Объявление вешаем в пятницу.\n- Второе.\n") == \
        "Объявление вешаем в пятницу."
    assert topic.of_summary("## Задачи\n* проверить проектор\n") == "проверить проектор"


def test_a_summary_rewritten_without_sections_gives_its_first_line_that_is_not_a_heading():
    assert topic.of_summary("# Мои заметки\n\nПереезд в четверг, прогон в среду.\n\nЛицензия не продлена.\n") == \
        "Переезд в четверг, прогон в среду."
    assert topic.of_summary("# Только заголовок\n") == "" and topic.of_summary("") == ""


def test_a_long_line_is_cut_at_a_word():
    about = topic.of_summary("## Коротко\n" + "слово " * 60)
    assert len(about) <= topic.SUMMARY and about.endswith("слово…") and "  " not in about
    assert topic.of_summary("## Коротко\n" + "я" * 300) == "я" * (topic.SUMMARY - 1) + "…"     # no word to cut at


def test_a_conversation_without_a_summary_is_about_its_beginning():
    said = [Phrase("me", 0.0, 2.0, " Проверка связи, слышно ли меня. "), Phrase("them", 2.5, 4.0, "Да, слышно хорошо."),
            Phrase("me", 5.0, 9.0, "Тогда начнём: " + "по порядку " * 30)]
    about = topic.of_phrases(said)
    assert about.startswith("Проверка связи, слышно ли меня. Да, слышно хорошо. Тогда начнём: по порядку")
    assert len(about) <= topic.START and about.endswith("…")
    assert topic.of_phrases(said[:2]) == "Проверка связи, слышно ли меня. Да, слышно хорошо."
    assert topic.of_phrases([]) == ""


def test_what_a_conversation_is_about_follows_its_summary_in_force(store):
    call = recognised(store, "Начнём с плана.", "Давайте.")
    topic.refresh(store, call.id)
    assert store.get(call.id).topic == "Начнём с плана. Давайте."      # no summary yet: the beginning
    store.save_summary(call.id, MADE, SUMMARY, AT, "opus", 60.0)
    topic.refresh(store, call.id)
    assert store.get(call.id).topic.startswith("Договорились перенести занятия кружка")
    store.save_summary(call.id, MINE, "## Коротко\nПереезд в четверг.\n", AT)
    topic.refresh(store, call.id)
    assert store.get(call.id).topic == "Переезд в четверг."            # the user's own is the one in force


def test_a_summary_that_says_nothing_leaves_the_beginning_of_the_conversation(store):
    call = recognised(store, "Начнём с плана.")
    store.save_summary(call.id, MINE, "# Только заголовок\n", AT)
    topic.refresh(store, call.id)
    assert store.get(call.id).topic == "Начнём с плана."
