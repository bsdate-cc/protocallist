"""The tray pictures: one per state, and each to be told from the others."""
import numpy as np

from protocallist.tray import icons

STATES = (icons.OFF, icons.WAITING, icons.RECORDING, icons.ERROR, icons.TRANSCRIBING)


def pixels(state: str) -> np.ndarray:
    return np.asarray(icons.image(state))


def test_every_state_has_a_picture_of_the_size_the_tray_wants():
    for state in STATES:
        image = icons.image(state)
        assert image.size == (icons.SIZE, icons.SIZE) and image.mode == "RGBA"


def test_no_two_states_look_alike():
    pictures = [icons.image(state).tobytes() for state in STATES]
    assert len(set(pictures)) == len(STATES)


def test_recording_is_red_and_waiting_is_not():
    def reds(state):
        p = pixels(state)
        return int(((p[..., 3] > 0) & (p[..., 0] > 180) & (p[..., 1] < 90)).sum())

    assert reds(icons.RECORDING) > 1000
    assert reds(icons.WAITING) == 0 and reds(icons.OFF) == 0


def test_nothing_watched_is_an_outline_not_one_more_colour():
    def painted(state):
        return int((pixels(state)[..., 3] > 0).sum())

    assert painted(icons.OFF) < painted(icons.WAITING) / 2    # mostly air inside


def test_an_unknown_state_is_drawn_as_nothing_watched():
    assert icons.image("something new").tobytes() == icons.image(icons.OFF).tobytes()


def test_recognition_is_the_waiting_bubble_with_dots_in_it():
    def whites(state):
        p = pixels(state)
        return int(((p[..., 3] > 0) & (p[..., :3].min(axis=-1) > 240)).sum())

    assert whites(icons.TRANSCRIBING) > 150 and whites(icons.WAITING) == 0
    grey = (pixels(icons.TRANSCRIBING)[..., 3] > 0).sum()
    assert grey == (pixels(icons.WAITING)[..., 3] > 0).sum()             # the same bubble under the dots
