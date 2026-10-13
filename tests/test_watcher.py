from protocallist.watcher import Ended, Released, Resumed, Started, Watcher

IGNORE = ("pythonw.exe", "SystemSettings.exe")
ZOOM, DISCORD = ["Zoom.exe"], ["Discord.exe"]
FREE: list[str] = []


def watcher() -> Watcher:
    return Watcher(IGNORE, linger=60)


def run(w: Watcher, *steps):
    """Feed (time, holders) pairs; every event that came, in order, with the time of its poll."""
    return [(now, event) for now, holders in steps for event in w.poll(now, holders)]


def test_nothing_happens_while_nobody_holds_the_microphone():
    w = watcher()
    assert run(w, (0, FREE), (1, FREE), (2, FREE)) == []
    assert not w.talking


def test_a_program_that_takes_the_microphone_begins_a_conversation_once():
    w = watcher()
    assert run(w, (0, FREE), (1, ZOOM), (2, ZOOM), (3, ZOOM)) == [(1, Started("Zoom.exe"))]
    assert w.talking


def test_an_ignored_program_begins_nothing_whatever_the_case_of_its_name():
    w = watcher()
    assert run(w, (0, ["PythonW.exe"]), (1, ["systemsettings.EXE"])) == []


def test_a_conversation_is_over_a_minute_after_the_microphone_was_let_go_of_and_ends_at_that_moment():
    w = watcher()
    events = run(w, (0, ZOOM), (100, FREE), (130, FREE), (159, FREE), (160, FREE), (161, FREE))
    assert events == [(0, Started("Zoom.exe")), (100, Released(100)), (160, Ended(100))]
    assert not w.talking


def test_the_same_program_taking_the_microphone_again_goes_on_with_the_same_conversation():
    w = watcher()
    events = run(w, (0, ZOOM), (100, FREE), (140, ["zoom.EXE"]), (300, FREE), (360, FREE))
    assert events == [(0, Started("Zoom.exe")), (100, Released(100)), (140, Resumed(140)),
                      (300, Released(300)), (360, Ended(300))]


def test_another_program_taking_the_microphone_ends_one_conversation_and_begins_the_next():
    w = watcher()
    events = run(w, (0, ZOOM), (100, FREE), (120, DISCORD))
    assert events == [(0, Started("Zoom.exe")), (100, Released(100)),
                      (120, Ended(100)), (120, Started("Discord.exe"))]
    assert w.talking


def test_a_second_program_joining_changes_nothing_while_somebody_holds_the_microphone():
    w = watcher()
    events = run(w, (0, ZOOM), (10, ZOOM + DISCORD), (20, DISCORD), (30, FREE), (90, FREE))
    # the conversation lasts while at least one of them holds the microphone
    assert events == [(0, Started("Zoom.exe")), (30, Released(30)), (90, Ended(30))]


def test_an_ignored_program_holding_the_microphone_does_not_keep_a_conversation_alive():
    w = watcher()
    events = run(w, (0, ZOOM), (100, ["pythonw.exe"]), (160, ["pythonw.exe"]))
    assert events == [(0, Started("Zoom.exe")), (100, Released(100)), (160, Ended(100))]


def test_a_headset_that_went_away_ends_the_conversation_there_and_then():
    w = watcher()
    assert run(w, (0, ZOOM), (50, None)) == [(0, Started("Zoom.exe")), (50, Ended(50))]
    w = watcher()
    assert run(w, (0, ZOOM), (50, FREE), (70, None)) == [(0, Started("Zoom.exe")), (50, Released(50)),
                                                           (70, Ended(50))]
    assert run(w, (80, None), (90, None)) == []             # no microphone, no conversations


def test_stopping_ends_a_conversation_that_is_going_on():
    w = watcher()
    w.poll(0, ZOOM)
    assert w.stop(40) == [Ended(40)]
    assert w.stop(41) == []
    w.poll(50, ZOOM)
    w.poll(100, FREE)
    assert w.stop(110) == [Ended(100)]                      # it had ended when the microphone was let go of


def test_a_conversation_thrown_away_is_not_begun_again_until_the_microphone_has_been_free_for_a_minute():
    w = watcher()
    w.poll(0, ZOOM)
    w.silence(10)
    assert not w.talking
    # the call goes on, the microphone comes and goes: nothing is recorded
    assert run(w, (11, ZOOM), (50, FREE), (80, ZOOM), (100, FREE), (159, FREE)) == []
    # a whole minute of a free microphone: the next call is a new one
    assert run(w, (160, FREE), (161, ZOOM)) == [(161, Started("Zoom.exe"))]


def test_a_new_conversation_can_begin_right_after_one_is_over():
    w = watcher()
    events = run(w, (0, ZOOM), (10, FREE), (70, FREE), (71, DISCORD))
    assert events[-2:] == [(70, Ended(10)), (71, Started("Discord.exe"))]
