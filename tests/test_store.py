import logging
import sqlite3
import threading
from datetime import datetime

import pytest

from protocallist import paths
from datetime import timedelta

from protocallist.store import DONE, FAILED, MADE, MINE, RECORDED, RECORDING, TRANSCRIBING, Phrase, Store

AT = datetime(2026, 10, 5, 16, 11, 51)


@pytest.fixture
def store():
    s = Store(paths.database_file(), paths.calls_dir())
    yield s
    s.close()


def set_state(store: Store, call_id: int, state: str) -> None:
    store._db.execute("UPDATE calls SET state = ? WHERE id = ?", (state, call_id))


def test_a_new_database_has_the_tables_and_knows_its_version(store):
    db = sqlite3.connect(paths.database_file())
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert {"redo_model", "redo_error", "text_at"} <= {row[1] for row in db.execute("PRAGMA table_info(calls)")}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"calls", "phrases", "summaries", "numbers"} <= tables
    assert "voice" in {row[1] for row in db.execute("PRAGMA table_info(phrases)")}
    assert "topic" in {row[1] for row in db.execute("PRAGMA table_info(calls)")}
    db.close()


def test_opening_the_database_again_keeps_what_is_in_it(store):
    call = store.begin(AT, "Zoom", "Zoom Meeting")
    store.close()
    again = Store(paths.database_file(), paths.calls_dir())
    assert again.get(call.id) == call
    again.close()


def test_the_number_of_a_conversation_is_never_given_to_another(store):
    """Whatever still thinks of a conversation by its number - a summary on its way, a text half
    recognised - must never find another conversation under it."""
    first, last = store.begin(AT, "Zoom"), store.begin(AT + timedelta(minutes=5), "Zoom")
    store.discard(last.id)
    again = store.begin(AT + timedelta(minutes=10), "Zoom")
    assert (first.id, last.id, again.id) == (1, 2, 3)                   # not the number that came free
    store.discard(again.id)
    store.close()
    reopened = Store(paths.database_file(), paths.calls_dir())
    try:
        assert reopened.begin(AT + timedelta(minutes=15), "Zoom").id == 4      # nor after a new start
    finally:
        reopened.close()


def test_a_conversation_begins_with_a_folder_named_by_time_and_program(store):
    call = store.begin(AT, "Zoom", "Zoom Meeting")
    assert (call.state, call.program, call.title, call.folder) == (RECORDING, "Zoom", "Zoom Meeting",
                                                                   "2026-10-05_16-11-51_Zoom")
    assert call.started_at.startswith("2026-10-05T16:11:51") and call.ended_at is None
    assert store.folder(call) == paths.calls_dir() / "2026-10-05_16-11-51_Zoom" and store.folder(call).is_dir()


def test_two_conversations_of_one_second_and_an_odd_program_name_get_folders_of_their_own(store):
    first = store.begin(AT, "Blue Sky Meetings")
    second = store.begin(AT, "Blue Sky Meetings")
    assert (first.folder, second.folder) == ("2026-10-05_16-11-51_Blue_Sky_Meetings",
                                             "2026-10-05_16-11-51_Blue_Sky_Meetings_2")
    assert first.title is None
    assert store.begin(AT, "Мой Созвон").folder == "2026-10-05_16-11-51_Мой_Созвон"
    assert store.begin(AT, "a/b:c").folder == "2026-10-05_16-11-51_a_b_c"


def test_a_finished_recording_waits_for_recognition_and_knows_when_it_ended(store):
    call = store.finish(store.begin(AT, "Zoom").id, 2834.5)
    assert (call.state, call.duration_s) == (RECORDED, 2834.5)
    assert call.ended_at.startswith("2026-10-05T16:59:05")
    assert store.calls(RECORDED) == [call] and store.calls(RECORDING) == []


def test_conversations_are_listed_from_the_earliest(store):
    late = store.begin(datetime(2026, 10, 5, 18, 0, 0), "Zoom")
    early = store.begin(datetime(2026, 10, 5, 9, 0, 0), "Discord")
    assert [c.id for c in store.calls()] == [early.id, late.id]


def test_a_discarded_conversation_leaves_nothing(store):
    call = store.begin(AT, "Zoom")
    (store.folder(call) / "mic.wav").write_bytes(b"x")
    beside = [store.sound(call), store.root / f"{call.folder}.transcript.md", store.summary_file(call),
              store.root / f"{call.folder}.opus.part", store.root / f"{call.folder}.md.part"]
    other = store.root / f"{call.folder}_2.opus"                       # of another conversation of the same second
    for path in beside + [other]:
        path.write_bytes(b"x")
    store.discard(call.id)
    assert store.get(call.id) is None and not store.folder(call).exists()
    assert [path.name for path in store.root.iterdir()] == [other.name]
    store.discard(call.id)                                   # twice is not an error


# --- after a crash


def test_a_discarded_conversation_takes_only_what_the_program_made(store):
    call = store.begin(AT, "Zoom")
    (store.folder(call) / "mic.wav").write_bytes(b"x")
    (store.folder(call) / "kept by me.txt").write_text("my own", encoding="utf-8")
    notes = store.root / f"{call.folder}.my notes.txt"
    notes.write_text("my own", encoding="utf-8")
    store.discard(call.id)
    assert store.get(call.id) is None and notes.exists()
    assert [path.name for path in store.folder(call).iterdir()] == ["kept by me.txt"]


def test_a_recording_cut_short_by_a_crash_is_kept_with_the_length_of_its_sound(store):
    call = store.begin(AT, "Zoom")
    store.recover(lambda folder: 754.25, min_seconds=30)
    kept = store.get(call.id)
    assert (kept.state, kept.duration_s) == (RECORDED, 754.25)
    assert kept.ended_at.startswith("2026-10-05T16:24:25")


def test_a_recording_too_short_or_without_files_is_dropped_on_recovery(store):
    short = store.begin(AT, "Zoom")
    gone = store.begin(AT, "Discord")
    store.folder(gone).rmdir()
    asked = []
    store.recover(lambda folder: asked.append(folder.name) or 12.0, min_seconds=30)
    assert store.calls() == []
    assert asked == [short.folder]                           # nobody measures a folder that is not there
    assert not store.folder(short).exists()


def test_recognition_cut_short_starts_over(store):
    call = store.finish(store.begin(AT, "Zoom").id, 100.0)
    set_state(store, call.id, TRANSCRIBING)
    store.recover(lambda folder: 0.0, min_seconds=30)
    assert store.get(call.id).state == RECORDED


def test_what_is_in_order_is_left_alone(store):
    done = store.finish(store.begin(AT, "Zoom").id, 100.0)
    set_state(store, done.id, DONE)
    store.recover(lambda folder: 0.0, min_seconds=30)
    assert store.get(done.id).state == DONE and store.folder(done).is_dir()


def test_a_folder_nobody_knows_and_a_record_without_a_folder_are_reported_and_left(store, caplog):
    orphan = paths.calls_dir() / "2026-01-01_00-00-00_Old"
    orphan.mkdir()
    done = store.finish(store.begin(AT, "Zoom").id, 100.0)
    store.folder(done).rmdir()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        store.recover(lambda folder: 0.0, min_seconds=30)
    text = caplog.text
    assert "2026-01-01_00-00-00_Old" in text and f"call {done.id} has no folder" in text
    assert orphan.is_dir() and store.get(done.id) is not None


def test_a_recording_that_cannot_be_mended_is_left_for_the_next_start_and_the_rest_is_recovered(store, caplog):
    locked = store.begin(AT, "Zoom")
    fine = store.begin(AT, "Discord")

    def length(folder):
        if folder.name == locked.folder:
            raise PermissionError(13, "another program holds mic.wav")
        return 100.0

    with caplog.at_level(logging.WARNING, logger="protocallist"):
        store.recover(length, min_seconds=30)                # does not raise: the service starts
    assert store.get(locked.id).state == RECORDING           # tried again at the next start
    assert store.folder(locked).is_dir()
    assert store.get(fine.id).state == RECORDED
    assert "could not be put in order" in caplog.text


# --- recognition


def recorded(store: Store, minutes_later: int = 0, seconds: float = 60.0):
    call = store.begin(AT + timedelta(minutes=minutes_later), "Zoom")
    return store.finish(call.id, seconds)


TALK = [Phrase("me", 0.5, 2.0, "добрый день"), Phrase("them", 2.1, 4.0, "здравствуйте"), Phrase("me", 4.2, 6.5, "начнём")]


def test_the_earliest_conversation_that_waits_is_taken_first_and_only_once(store):
    later, earlier = recorded(store, minutes_later=10), recorded(store)
    taken = store.take()
    assert taken.id == earlier.id and taken.state == TRANSCRIBING
    assert store.take().id == later.id
    assert store.take() is None                                  # nothing waits any more


def test_a_conversation_still_being_recorded_is_not_taken(store):
    store.begin(AT, "Zoom")
    assert store.take() is None


def test_the_text_of_a_conversation_is_written_whole_with_its_new_state(store):
    call = recorded(store)
    store.take()
    done = store.done(call.id, TALK, "gigaam-v3")
    assert (done.state, done.model, done.error) == (DONE, "gigaam-v3", None)
    assert store.phrases(call.id) == TALK


def test_a_text_that_cannot_be_written_whole_is_not_written_at_all(store):
    call = recorded(store)
    store.take()
    broken = TALK + [Phrase("me", 7.0, 8.0, None)]                # the database refuses a phrase without words
    with pytest.raises(sqlite3.IntegrityError):
        store.done(call.id, broken, "gigaam-v3")
    assert store.phrases(call.id) == [] and store.get(call.id).state == TRANSCRIBING


def test_recognising_again_replaces_the_text(store):
    call = recorded(store)
    store.done(call.id, TALK, "gigaam-v3")
    store.done(call.id, TALK[:1], "gigaam-v4")
    assert store.phrases(call.id) == TALK[:1] and store.get(call.id).model == "gigaam-v4"


def test_phrases_come_back_in_the_order_they_were_said(store):
    call = recorded(store)
    store.done(call.id, list(reversed(TALK)), "gigaam-v3")
    assert store.phrases(call.id) == TALK


def test_a_conversation_put_back_waits_again_and_no_attempt_is_counted(store):
    call = recorded(store)
    store.take()
    store.put_back(call.id)
    assert (store.get(call.id).state, store.get(call.id).attempts) == (RECORDED, 0)
    store.done(call.id, TALK, "gigaam-v3")
    store.put_back(call.id)                                       # only what is being recognised can be put back
    assert store.get(call.id).state == DONE


def test_a_third_failed_attempt_fails_the_conversation_for_good_with_the_reason(store):
    call = recorded(store)
    for attempt in (1, 2):
        store.take()
        missed = store.missed(call.id, "the gateway answered 500")
        assert (missed.state, missed.attempts) == (RECORDED, attempt)
    store.take()
    failed = store.missed(call.id, "no answer in 10 minutes")
    assert (failed.state, failed.attempts, failed.error) == (FAILED, 3, "no answer in 10 minutes")
    assert store.take() is None                                   # a failed one is not taken by itself


def test_failed_conversations_are_tried_anew_on_request(store):
    first, second, fine = recorded(store), recorded(store, 5), recorded(store, 9)
    for call in (first, second):
        for _ in range(3):
            store.missed(call.id, "the gateway answered 500")
    assert store.retry_failed() == 2
    again = store.get(first.id)
    assert (again.state, again.attempts, again.error) == (RECORDED, 0, None)
    assert store.retry_failed() == 0 and store.get(fine.id).state == RECORDED


def test_the_states_are_counted(store):
    assert store.counts() == {}
    a, b, c = recorded(store), recorded(store, 5), recorded(store, 9)
    store.begin(AT + timedelta(minutes=20), "Zoom")
    store.done(a.id, TALK, "gigaam-v3")
    for _ in range(3):
        store.missed(b.id, "x")
    assert store.counts() == {DONE: 1, FAILED: 1, RECORDED: 1, RECORDING: 1}


def test_sound_that_outlived_its_time_is_found_and_marked_as_deleted(store):
    old, new, waiting = recorded(store), recorded(store, minutes_later=60 * 24 * 40), recorded(store, 5)
    store.done(old.id, TALK, "gigaam-v3")
    store.done(new.id, TALK, "gigaam-v3")
    border = AT + timedelta(days=30)
    assert [c.id for c in store.expired(border)] == [old.id]     # the one that only waits keeps its sound
    store.audio_deleted(old.id, AT + timedelta(days=31))
    assert store.expired(border) == []
    assert store.get(old.id).audio_deleted_at.startswith("2026-11-05")


def test_one_thread_reads_the_archive_while_another_writes_into_it(store):
    # The service behind the icon asks what waits every second; recognition writes a whole
    # conversation at once. They share one connection, and it does not bear two at a time.
    import threading

    calls = [recorded(store, minutes_later=n) for n in range(3)]
    talk = [Phrase("me" if n % 2 else "them", float(n), n + 0.5, "слово") for n in range(400)]
    failures: list[BaseException] = []
    stop = threading.Event()

    def read():
        try:
            while not stop.is_set():
                store.counts(), store.get(calls[0].id), store.calls(), store.phrases(calls[1].id)
        except BaseException as e:      # the test must see it, not the thread's stderr
            failures.append(e)

    readers = [threading.Thread(target=read) for _ in range(4)]
    for reader in readers:
        reader.start()
    try:
        for turn in range(150):
            store.done(calls[turn % 3].id, talk, "gigaam-v3")
            store.put_back(calls[turn % 3].id)
    except BaseException as e:
        failures.append(e)
    finally:
        stop.set()
        for reader in readers:
            reader.join()
    assert failures == []
    assert len(store.phrases(calls[0].id)) == 400


def test_a_conversation_recognised_at_last_forgets_why_it_failed_before(store):
    call = recorded(store)
    store.missed(call.id, "the gateway answered 500")
    done = store.done(call.id, TALK, "gigaam-v3")
    assert (done.state, done.error, done.attempts) == (DONE, None, 1)


def test_the_archive_is_closed_in_its_turn_like_everything_else(store):
    """A close that lands inside a statement of the other thread is what the lock is there against."""
    closing = threading.Thread(target=store.close)
    with store._lock:
        closing.start()
        closing.join(0.3)
        waited = closing.is_alive()
    closing.join(5)
    assert waited and not closing.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        store.counts()


# --- summaries, and what a conversation is about


def recognised(store: Store, minutes_later: int = 0):
    call = store.begin(AT + timedelta(minutes=minutes_later), "Zoom")
    store.finish(call.id, 60.0)
    store.take()
    return store.done(call.id, [Phrase("me", 0.5, 2.0, "начнём с плана")], "gigaam-v3")


def test_an_archive_of_the_first_version_is_brought_up_to_date_with_all_it_holds():
    old = sqlite3.connect(paths.database_file())
    old.executescript("""
        CREATE TABLE calls (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, duration_s REAL,
                            program TEXT NOT NULL, title TEXT, folder TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
                            attempts INTEGER NOT NULL DEFAULT 0, model TEXT, audio_deleted_at TEXT);
        CREATE TABLE phrases (id INTEGER PRIMARY KEY, call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                              speaker TEXT NOT NULL, voice INTEGER, start_s REAL NOT NULL, end_s REAL NOT NULL,
                              text TEXT NOT NULL);
        CREATE INDEX phrases_by_call ON phrases (call_id, start_s);
        INSERT INTO calls (id, started_at, program, folder, state, model)
            VALUES (7, '2026-10-05T16:11:51+04:00', 'Zoom', '2026-10-05_16-11-51_Zoom', 'done', 'gigaam-v3');
        INSERT INTO phrases (call_id, speaker, start_s, end_s, text) VALUES (7, 'me', 0.5, 2.0, 'начнём с плана');
        PRAGMA user_version = 1;
    """)
    old.close()
    store = Store(paths.database_file(), paths.calls_dir())
    call = store.get(7)
    assert (call.state, call.model, call.topic) == (DONE, "gigaam-v3", None)
    assert store.phrases(7) == [Phrase("me", 0.5, 2.0, "начнём с плана")] and store.summaries(7) == {}
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 3
    new = store.begin(AT, "Discord")
    store.discard(new.id)
    assert new.id == 8 and store.begin(AT, "Discord").id == 9          # the numbers go on from those it holds
    store.close()


def test_the_summary_that_was_made_and_the_users_own_are_kept_apart(store):
    call = recognised(store)
    assert store.summary(call.id) is None and store.summaries(call.id) == {}
    store.save_summary(call.id, MADE, "# Выжимка\nкак сделал Claude\n", AT, "opus", 64.2)
    made = store.summary(call.id)
    assert (made.kind, made.text, made.maker, made.seconds) == (MADE, "# Выжимка\nкак сделал Claude\n", "opus", 64.2)
    assert made.saved_at.startswith("2026-10-05T16:11:51")
    store.save_summary(call.id, MINE, "# Выжимка\nкак поправил я\n", AT + timedelta(hours=1))
    mine = store.summary(call.id)
    assert (mine.kind, mine.text, mine.maker, mine.seconds) == (MINE, "# Выжимка\nкак поправил я\n", None, None)
    assert set(store.summaries(call.id)) == {MADE, MINE}


def test_a_summary_made_anew_never_touches_the_users_own(store):
    call = recognised(store)
    store.save_summary(call.id, MADE, "первая", AT, "opus", 60.0)
    store.save_summary(call.id, MINE, "моя", AT)
    store.save_summary(call.id, MADE, "вторая", AT + timedelta(hours=2), "sonnet", 30.0)
    both = store.summaries(call.id)
    assert both[MINE].text == "моя" and (both[MADE].text, both[MADE].maker) == ("вторая", "sonnet")
    assert store.summary(call.id).text == "моя"                        # the one in force is still the user's own


def test_summaries_go_with_their_conversation_and_none_is_kept_for_one_that_is_gone(store):
    call = recognised(store)
    store.save_summary(call.id, MADE, "выжимка", AT, "opus", 60.0)
    store.discard(call.id)
    assert store._rows("SELECT COUNT(*) FROM summaries")[0][0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        store.save_summary(call.id, MADE, "опоздала", AT, "opus", 60.0)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_summary(recognised(store, 5).id, "another", "что-то", AT)


def test_which_conversations_have_a_summary_and_what_each_is_about(store):
    first, second, third = recognised(store), recognised(store, 5), recognised(store, 10)
    store.save_summary(first.id, MADE, "выжимка", AT, "opus", 60.0)
    store.save_summary(third.id, MINE, "моя", AT)
    store.save_summary(third.id, MADE, "выжимка", AT, "opus", 60.0)
    assert store.kinds() == {first.id: MADE, third.id: MINE}           # of the two, the one in force
    store.set_topic(second.id, "Сроки переезда в большой зал")
    assert store.get(second.id).topic == "Сроки переезда в большой зал" and store.get(first.id).topic is None


# --- where the files of a conversation lie


def test_the_files_of_a_conversation_lie_beside_one_another_under_its_name(store):
    call = store.begin(AT, "Zoom")
    assert store.root == paths.calls_dir() and store.folder(call) == store.root / "2026-10-05_16-11-51_Zoom"
    assert store.sound(call) == store.root / "2026-10-05_16-11-51_Zoom.opus"
    assert store.summary_file(call) == store.root / "2026-10-05_16-11-51_Zoom.md"


def test_a_name_is_not_given_twice_while_anything_of_the_first_conversation_is_left(store):
    first = recognised(store)
    store.folder(first).rmdir()                                        # recognised: its working folder is gone
    assert store.begin(AT, "Zoom").folder == "2026-10-05_16-11-51_Zoom_2"      # ...but its record is there
    alone = store.root / "2026-10-05_16-11-51_Discord.md"              # a summary left by a conversation forgotten by hand
    alone.write_text("выжимка", encoding="utf-8")
    assert store.begin(AT, "Discord").folder == "2026-10-05_16-11-51_Discord_2"


def test_a_recognised_conversation_needs_no_working_folder(store, caplog):
    done = recognised(store)
    store.folder(done).rmdir()
    waiting = store.finish(store.begin(AT + timedelta(minutes=5), "Zoom").id, 100.0)
    store.folder(waiting).rmdir()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        store.recover(lambda folder: 0.0, min_seconds=30)
    assert f"call {waiting.id} has no folder" in caplog.text and f"call {done.id} " not in caplog.text


# --- what a list of conversations and a search ask


def said(store: Store, minutes_later: int, *phrases: tuple[str, str]):
    call = store.begin(AT + timedelta(minutes=minutes_later), "Zoom")
    store.finish(call.id, 60.0)
    store.take()
    return store.done(call.id, [Phrase(speaker, i * 5.0, i * 5.0 + 4, text) for i, (speaker, text) in enumerate(phrases)],
                      "gigaam-v3")


def test_phrases_of_one_side_that_follow_one_another_are_counted_as_one_remark(store):
    talk = said(store, 0, ("me", "начнём"), ("me", "с плана"), ("them", "давайте"), ("me", "хорошо"), ("me", "итак"))
    silent = said(store, 5)
    assert store.remarks(talk.id) == 3 and store.remarks(silent.id) == 0 and store.remarks(999) == 0


def test_words_are_found_in_what_was_said_and_in_summaries_whatever_the_case(store):
    first = said(store, 0, ("me", "Про Лицензию на программу узнаю сегодня."))
    second = said(store, 5, ("them", "Давайте о другом."))
    third = said(store, 10, ("me", "Ничего похожего."))
    store.save_summary(second.id, MADE, "## Коротко\nОткрыт вопрос с ЛИЦЕНЗИЕЙ.\n", AT, "opus", 60.0)
    store.save_summary(third.id, MINE, "## Коротко\nпро лицензию ни слова... кроме этого.\n", AT)
    assert store.search("лицензи") == {first.id, second.id, third.id}
    assert store.search("  ЛИЦЕНЗИЮ на  ") == {first.id}                 # as typed, without the spaces around
    assert store.search("программу узнаю") == {first.id} and store.search("нет такого") == set()
    assert store.search("") == set() and store.search("   ") == set()
    assert store.search("100%") == set() and store.search("_") == set()   # no sign means "anything" here


def test_a_search_does_not_keep_the_recording_side_waiting(store):
    """It reads through a connection of its own: a long look through years of talk must not hold up the
    thread that begins a recording."""
    said(store, 0, ("me", "начнём с плана"))
    with store._lock:                                                   # the other side is in the middle of something
        found = []
        looking = threading.Thread(target=lambda: found.append(store.search("план")))
        looking.start()
        looking.join(5)
        assert not looking.is_alive() and found == [{1}]


def test_a_conversation_one_of_whose_files_is_held_open_stays_whole(store):
    """All of its files or none: a refusal must not cost the conversation its sound or one of its two sides."""
    done = recognised(store, 10)
    store.sound(done).write_bytes(b"OggS")
    store.summary_file(done).write_text("выжимка", encoding="utf-8")
    whole = sorted(path.name for path in store.root.iterdir())
    for held in (store.summary_file(done), store.sound(done)):          # whichever of them some program has open
        with open(held, "rb"):
            with pytest.raises(OSError):
                store.forget(done.id)
        assert store.get(done.id) is not None and store.phrases(done.id)
        assert sorted(path.name for path in store.root.iterdir()) == whole
        assert store.sound(done).read_bytes() == b"OggS" and store.summary_file(done).read_text(encoding="utf-8") == "выжимка"
    assert store.forget(done.id) is True and store.get(done.id) is None
    assert [path.name for path in store.root.iterdir() if path.name.startswith(done.folder)] == []
    waiting = store.finish(store.begin(AT + timedelta(minutes=20), "Zoom").id, 60.0)
    for name in ("mic.wav", "line.wav"):
        (store.folder(waiting) / name).write_bytes(b"RIFF")
    for held in ("mic.wav", "line.wav"):                                # one of the two recordings is open in a player
        with open(store.folder(waiting) / held, "rb"):
            with pytest.raises(OSError):
                store.forget(waiting.id)
        assert sorted(path.name for path in store.folder(waiting).iterdir()) == ["line.wav", "mic.wav"]     # both sides, as they were
    assert store.get(waiting.id).state == RECORDED
    assert store.forget(waiting.id) is True and not store.folder(waiting).exists()


def test_forgetting_a_conversation_takes_only_what_the_program_made(store):
    done = recognised(store, 10)
    store.sound(done).write_bytes(b"OggS")
    (store.root / f"{done.folder}.opus.part").write_bytes(b"half")
    notes = store.root / f"{done.folder}.my notes.txt"
    notes.write_text("my own", encoding="utf-8")
    folder = store.folder(done)
    folder.mkdir(exist_ok=True)
    (folder / "mic.wav").write_bytes(b"RIFF")
    (folder / "kept by me.txt").write_text("my own", encoding="utf-8")
    assert store.forget(done.id) is True and store.get(done.id) is None
    assert sorted(path.name for path in store.root.iterdir() if path.name.startswith(done.folder)) == [done.folder, notes.name]
    assert [path.name for path in folder.iterdir()] == ["kept by me.txt"]      # the folder stays, with what is not the program's


def test_one_failed_conversation_can_be_tried_again(store):
    failed, other = store.finish(store.begin(AT, "Zoom").id, 60.0), store.finish(store.begin(AT, "Discord").id, 60.0)
    for call in (failed, other):
        for _ in range(3):
            store.take()
            store.missed(call.id, "the gateway answered 500")
    assert store.retry(failed.id) is True
    again = store.get(failed.id)
    assert (again.state, again.attempts, again.error) == (RECORDED, 0, None) and store.get(other.id).state == FAILED
    assert store.retry(failed.id) is False and store.retry(999) is False       # only one that failed


def test_a_conversation_is_forgotten_unless_it_is_being_recorded_or_recognised(store):
    recording = store.begin(AT, "Zoom")
    done = recognised(store, 10)
    waiting = store.finish(store.begin(AT + timedelta(minutes=5), "Zoom").id, 60.0)
    store.sound(done).write_bytes(b"OggS")
    store.save_summary(done.id, MADE, "выжимка", AT, "opus", 60.0)
    assert store.forget(recording.id) is False and store.get(recording.id) is not None and store.folder(recording).is_dir()
    taken = store.take()
    assert taken.id == waiting.id and store.forget(waiting.id) is False and store.folder(waiting).is_dir()
    store.put_back(waiting.id)
    assert store.forget(waiting.id) is True and store.get(waiting.id) is None and not store.folder(waiting).exists()
    assert store.forget(done.id) is True and store.summaries(done.id) == {} and not store.sound(done).exists()
    assert store.forget(done.id) is True                                # one that is gone is gone


# --- recognising anew, with another model

NEW = [Phrase("me", 0.0, 1.5, "let us begin"), Phrase("them", 2.0, 3.0, "go ahead")]


def with_text(store: Store, minutes: int = 0):
    call = store.begin(AT + timedelta(minutes=minutes), "Zoom")
    store.finish(call.id, 60.0)
    store.take()
    return store.done(call.id, TALK, "gigaam-v3", AT + timedelta(minutes=minutes + 2))


def test_an_archive_of_the_second_version_learns_when_its_texts_were_written():
    from protocallist import store as archive

    old = sqlite3.connect(paths.database_file())
    for statements in archive._SCHEMA[:2]:
        for statement in statements:
            old.execute(statement)
    old.execute("INSERT INTO calls (id, started_at, ended_at, program, folder, state, model) VALUES "
                "(3, '2026-10-05T16:11:51+04:00', '2026-10-05T16:40:00+04:00', 'Zoom', '2026-10-05_16-11-51_Zoom', 'done', 'gigaam-v3'),"
                "(4, '2026-10-05T17:00:00+04:00', '2026-10-05T17:05:00+04:00', 'Zoom', '2026-10-05_17-00-00_Zoom', 'recorded', NULL)")
    old.execute("INSERT INTO phrases (call_id, speaker, start_s, end_s, text) VALUES (3, 'me', 0.5, 2.0, 'начнём с плана')")
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()
    store = Store(paths.database_file(), paths.calls_dir())
    done, waiting = store.get(3), store.get(4)
    # when a text of before was written nobody knows: as the conversation ended - never later than a summary of it
    assert (done.text_at, done.redo_model, done.redo_error) == ("2026-10-05T16:40:00+04:00", None, None)
    assert waiting.text_at is None and store.phrases(3) == [Phrase("me", 0.5, 2.0, "начнём с плана")]
    store.close()


def test_the_text_of_a_conversation_knows_when_it_was_written(store):
    call = with_text(store)
    assert datetime.fromisoformat(call.text_at) == (AT + timedelta(minutes=2)).astimezone()
    other = recorded(store, minutes_later=9)
    store.take()
    written = store.done(other.id, TALK, "gigaam-v3")                    # no moment named: now
    assert abs((datetime.now().astimezone() - datetime.fromisoformat(written.text_at)).total_seconds()) < 5


def test_a_recognised_conversation_is_asked_to_be_recognised_anew_and_keeps_its_text_meanwhile(store):
    call = with_text(store)
    assert store.redo(call.id, "parakeet-unified-en") is True
    asked = store.get(call.id)
    assert (asked.state, asked.redo_model, asked.model) == (DONE, "parakeet-unified-en", "gigaam-v3")
    assert store.phrases(call.id) == TALK                                # read, searched and summarised as before
    assert store.redo(call.id, "another") is False and store.get(call.id).redo_model == "parakeet-unified-en"     # it waits already
    waiting = recorded(store, minutes_later=9)
    assert store.redo(waiting.id, "parakeet-unified-en") is False        # it has no text to replace: it is simply recognised
    assert store.redo(999, "parakeet-unified-en") is False


def test_those_asked_anew_are_taken_the_earliest_first_and_stay_asked_until_it_is_done(store):
    first, second = with_text(store), with_text(store, minutes=10)
    assert store.take_redo() is None
    store.redo(second.id, "parakeet-unified-en")
    store.redo(first.id, "parakeet-unified-en")
    assert store.take_redo().id == first.id and store.take_redo().id == first.id      # until its new text is written
    assert store.take() is None                                          # it does not wait to be recognised the first time
    store.redone(first.id, NEW, "parakeet-unified-en", AT + timedelta(hours=1))
    assert store.take_redo().id == second.id


def test_the_new_text_takes_the_place_of_the_old_in_one_go(store):
    call = with_text(store)
    store.redo(call.id, "parakeet-unified-en")
    assert store.redone(call.id, NEW, "parakeet-unified-en", AT + timedelta(hours=1)) is True
    after = store.get(call.id)
    assert store.phrases(call.id) == NEW and (after.model, after.redo_model, after.redo_error, after.state) == \
        ("parakeet-unified-en", None, None, DONE)
    assert datetime.fromisoformat(after.text_at) == (AT + timedelta(hours=1)).astimezone()
    store.redo(call.id, "gigaam-v3")
    with pytest.raises(sqlite3.IntegrityError):                         # a text that cannot be written whole...
        store.redone(call.id, TALK + [Phrase("me", 7.0, 8.0, None)], "gigaam-v3", AT + timedelta(hours=2))
    assert store.phrases(call.id) == NEW and store.get(call.id).redo_model == "gigaam-v3"     # ...changes nothing


def test_an_attempt_anew_that_failed_leaves_the_text_and_says_why(store):
    call = with_text(store)
    store.redo(call.id, "parakeet-unified-en")
    store.redo_failed(call.id, "parakeet-unified-en", "the gateway answered 500: boom")
    after = store.get(call.id)
    assert (after.redo_model, after.redo_error, after.model, after.state) == (None, "the gateway answered 500: boom", "gigaam-v3", DONE)
    assert store.phrases(call.id) == TALK and store.take_redo() is None
    assert store.redo(call.id, "parakeet-unified-en") is True and store.get(call.id).redo_error is None      # asked again


def test_a_new_text_nobody_asked_for_is_not_written(store):
    call = with_text(store)
    assert store.redone(call.id, NEW, "parakeet-unified-en", AT) is False and store.phrases(call.id) == TALK
    store.redo(call.id, "parakeet-unified-en")
    store.discard(call.id)                                               # the conversation was forgotten meanwhile
    assert store.redone(call.id, NEW, "parakeet-unified-en", AT) is False and store.phrases(call.id) == []


def test_a_request_to_recognise_anew_can_be_taken_back(store):
    call = with_text(store)
    assert store.cancel_redo(call.id) is False                           # nothing was asked for
    store.redo(call.id, "parakeet-unified-en")
    assert store.cancel_redo(call.id) is True
    after = store.get(call.id)
    assert (after.redo_model, after.redo_error, after.model) == (None, None, "gigaam-v3") and store.phrases(call.id) == TALK
    assert store.take_redo() is None
    assert store.redone(call.id, NEW, "parakeet-unified-en", AT) is False and store.phrases(call.id) == TALK     # what was being read is not written


def test_the_end_of_an_attempt_does_not_touch_a_request_made_meanwhile_with_another_model(store):
    """Taken back and asked for with another model while the last piece was being read: what ends is the attempt
    with the first model, and it is nobody's any more."""
    call = with_text(store)
    store.redo(call.id, "model-a")
    store.cancel_redo(call.id)
    store.redo(call.id, "model-b")
    assert store.redone(call.id, NEW, "model-a", AT) is False            # it read to the end...
    after = store.get(call.id)
    assert (after.redo_model, after.model) == ("model-b", "gigaam-v3") and store.phrases(call.id) == TALK
    assert store.redo_failed(call.id, "model-a", "the gateway answered 500: boom") is False      # ...or failed
    after = store.get(call.id)
    assert (after.redo_model, after.redo_error) == ("model-b", None)
    assert store.redo_failed(call.id, "model-b", "boom") is True         # the attempt that is asked for says how it ended
    assert (store.get(call.id).redo_model, store.get(call.id).redo_error) == (None, "boom")
    assert store.redo_failed(call.id, "model-b", "again") is False and store.get(call.id).redo_error == "boom"     # taken back: nothing to say
