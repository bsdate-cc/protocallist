"""The text of a conversation for reading: what the panel gives as a file, and what a summary
is made from. The text itself is in the archive; here it is only put into words."""
from __future__ import annotations

from datetime import datetime

from protocallist.i18n import N_, _
from protocallist.store import Call, Phrase

HEADING = N_("# Разговор {date} — {program}, {minutes} мин")
NAMES = {"me": N_("Я"), "them": N_("Собеседники")}


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def remarks(phrases: list[Phrase]) -> list[tuple[str, float, str]]:
    """(who, when it began, what was said): phrases of one side that follow one another are one remark."""
    out: list[tuple[str, float, str]] = []
    for phrase in phrases:
        if out and out[-1][0] == phrase.speaker:
            out[-1] = (phrase.speaker, out[-1][1], f"{out[-1][2]} {phrase.text}")
        else:
            out.append((phrase.speaker, phrase.start_s, phrase.text))
    return out


def render(call: Call, phrases: list[Phrase]) -> str:
    """The heading and the remarks, in the language of the interface at this moment."""
    started = datetime.fromisoformat(call.started_at)
    minutes = max(1, round((call.duration_s or 0.0) / 60))
    lines = [_(HEADING).format(date=started.strftime("%d.%m.%Y %H:%M"), program=call.program, minutes=minutes), ""]
    for speaker, began, text in remarks(phrases):
        lines += [f"**[{clock(began)}] {_(NAMES[speaker])}:** {text}", ""]
    return "\n".join(lines)
