"""What a conversation is about, in one line - for a list of conversations.

Programs do not say what a meeting is called, so the line is taken from what there is: from
the summary in force - its first paragraph - or, while there is no summary, from the
beginning of the conversation itself.
"""
from __future__ import annotations

import re

from protocallist.store import Phrase, Store

SUMMARY = 160       # so many characters at most from a summary...
START = 120         # ...and from the beginning of a conversation,
FIRST = 12          # which is looked for in so many of its phrases: more are never needed for one line
_POINT = re.compile(r"^[-*]\s+(?:\[[\d:]+\]\s*)?")      # the mark of a point of a list, and its time


def _cut(text: str, limit: int) -> str:
    """No longer than `limit`, cut at a word where there is one."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    return (head[:head.rindex(" ")] if " " in head else head) + "…"


def of_summary(text: str) -> str:
    """The first paragraph of a summary: its first lines that are not a heading - of a list, its
    first point. As a summary is made that is what stands under "In short"; one the user rewrote in
    a way of their own is about whatever it begins with."""
    paragraph: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if line and not line.startswith("#"):
            if paragraph and _POINT.match(line):
                break                           # the second point of a list is not the first paragraph
            paragraph.append(_POINT.sub("", line))
        elif paragraph:
            break
    return _cut(" ".join(paragraph), SUMMARY)


def of_phrases(phrases: list[Phrase]) -> str:
    """The beginning of a conversation: its first phrases in a row."""
    return _cut(" ".join(phrase.text for phrase in phrases[:FIRST]), START)


def refresh(store: Store, call_id: int) -> None:
    """Work out what a conversation is about and keep it in the archive."""
    summary = store.summary(call_id)
    about = of_summary(summary.text) if summary is not None else ""
    store.set_topic(call_id, about or of_phrases(store.phrases(call_id)))
