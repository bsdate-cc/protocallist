"""When a conversation begins and when it is over, judged by who holds the microphone.

Nothing here touches Windows: poll() is told the time and the programs that hold the
microphone now, and answers with what has happened. A conversation begins when a program
takes the microphone; it is over when the microphone has been free for `linger` seconds -
a program that opens its device anew, or a call that dropped, is back within them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Started:
    program: str        # the file name of the program that took the microphone


@dataclass(frozen=True)
class Released:
    """Nobody holds the microphone any more; the conversation may still go on."""
    at: float


@dataclass(frozen=True)
class Resumed:
    at: float


@dataclass(frozen=True)
class Ended:
    at: float           # the moment the microphone was let go of, not the moment it became certain


Event = Started | Released | Resumed | Ended

_IDLE, _TALKING, _LINGER, _SILENCED = "idle", "talking", "linger", "silenced"


class Watcher:
    def __init__(self, ignore: Iterable[str], linger: float) -> None:
        self._ignore = {name.lower() for name in ignore}
        self._linger = linger
        self._state = _IDLE
        self._program = ""
        self._since = 0.0       # when the microphone was let go of (linger) or was last seen busy (silenced)

    @property
    def talking(self) -> bool:
        """Is there a conversation now - going on or perhaps not over yet."""
        return self._state in (_TALKING, _LINGER)

    def poll(self, now: float, holders: Sequence[str] | None) -> list[Event]:
        """What has happened by `now`. `holders` are the file names of the programs that hold
        the microphone, or None when there is no such microphone in the system."""
        programs = [p for p in (holders or ()) if p.lower() not in self._ignore]
        if self._state == _IDLE:
            if programs:
                return self._begin(programs[0])
        elif self._state == _TALKING:
            if holders is None:
                return self._finish(now)                    # the headset is gone: nothing more will come
            if not programs:
                self._state, self._since = _LINGER, now
                return [Released(now)]
        elif self._state == _LINGER:
            if holders is None:
                return self._finish(self._since)
            if any(p.lower() == self._program.lower() for p in programs):
                self._state = _TALKING
                return [Resumed(now)]
            if programs:                                    # somebody else took it: that is another conversation
                return self._finish(self._since) + self._begin(programs[0])
            if now - self._since >= self._linger:
                return self._finish(self._since)
        elif self._state == _SILENCED:
            if programs:
                self._since = now
            elif now - self._since >= self._linger:
                self._state = _IDLE
        return []

    def _begin(self, program: str) -> list[Event]:
        self._state, self._program = _TALKING, program
        return [Started(program)]

    def _finish(self, at: float) -> list[Event]:
        self._state = _IDLE
        return [Ended(at)]

    def stop(self, now: float) -> list[Event]:
        """The service is leaving, or recording was switched off: a conversation that is going on ends here."""
        if self._state == _TALKING:
            return self._finish(now)
        if self._state == _LINGER:
            return self._finish(self._since)
        self._state = _IDLE
        return []

    def silence(self, now: float) -> None:
        """The user threw this conversation away: nothing begins until the microphone has
        been free for the whole `linger`, or the same call would be recorded again at once."""
        self._state, self._since = _SILENCED, now
