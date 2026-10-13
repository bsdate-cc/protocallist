"""Tray icons: one image per state of the service."""
from __future__ import annotations

from PIL import Image, ImageDraw

SIZE = 64
GREY = (128, 128, 128, 255)
RED = (220, 50, 47, 255)

OFF, WAITING, RECORDING, ERROR, TRANSCRIBING = "off", "waiting", "recording", "error", "transcribing"
WHITE = (255, 255, 255, 255)

_BUBBLE = (5, 8, 59, 44)
_TAIL = ((16, 42), (16, 59), (33, 42))


def _bubble(draw: ImageDraw.ImageDraw, colour, filled: bool) -> None:
    if filled:
        draw.rounded_rectangle(_BUBBLE, radius=12, fill=colour)
        draw.polygon(_TAIL, fill=colour)
    else:
        draw.polygon(_TAIL, fill=colour)
        draw.rounded_rectangle(_BUBBLE, radius=12, outline=colour, width=5)
        # the inside of the outline, cut out of the tail where the two overlap
        draw.rounded_rectangle((_BUBBLE[0] + 5, _BUBBLE[1] + 5, _BUBBLE[2] - 5, _BUBBLE[3] - 5), radius=8,
                               fill=(0, 0, 0, 0))


def image(state: str) -> Image.Image:
    """A speech bubble - what is said in a conversation. Grey: waiting for one. Red: one is
    being recorded. Outline only: nothing is watched - a SHAPE on purpose, "not recording"
    must not read as one more colour of "recording". A red cross over it: an error. Three
    dots in it: a conversation is being turned into text."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if state == RECORDING:
        _bubble(draw, RED, filled=True)
    elif state == WAITING:
        _bubble(draw, GREY, filled=True)
    elif state == TRANSCRIBING:
        _bubble(draw, GREY, filled=True)
        for x in (19, 32, 45):
            draw.ellipse((x - 5, 21, x + 5, 31), fill=WHITE)
    elif state == ERROR:
        _bubble(draw, GREY, filled=True)
        draw.line((10, 10, SIZE - 10, SIZE - 10), fill=RED, width=9)
        draw.line((10, SIZE - 10, SIZE - 10, 10), fill=RED, width=9)
    else:
        _bubble(draw, GREY, filled=False)
    return img
