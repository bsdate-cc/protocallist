"""The service: looks at the microphone once a second and turns what it sees into recordings."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from protocallist import audio
from protocallist.config import RecordingConfig
from protocallist.recorder import Recorder
from protocallist.store import Call, Store
from protocallist.watcher import Ended, Event, Released, Resumed, Started, Watcher

log = logging.getLogger(__name__)


def mend(folder: Path) -> float:
    """Repair the recordings a dead process left in a folder and make them one length;
    the seconds of sound there."""
    frames = {path: audio.repair(path) for path in (folder / "mic.wav", folder / "line.wav") if path.is_file()}
    longest = max(frames.values(), default=0)
    for path, have in frames.items():
        if have < longest:
            with open(path, "ab") as f:
                for pcm in audio.silence(longest - have):
                    f.write(pcm)
            audio.repair(path)
    return longest / audio.RATE


class Service:
    def __init__(self, cfg: RecordingConfig, store: Store, holders: Callable[[str], list[str] | None],
                 make_recorder: Callable[[], Recorder], title: Callable[[str], str] = lambda program: "",
                 clock: Callable[[], float] = time.monotonic, today: Callable[[], datetime] = datetime.now) -> None:
        self._cfg = cfg
        self._store = store
        self._holders = holders
        self._make_recorder = make_recorder
        self._title = title
        self._clock = clock
        self._today = today
        self._watcher = Watcher(cfg.ignore, cfg.linger_seconds)
        self._call: Call | None = None
        self._recorder: Recorder | None = None
        self.refused = False            # the last conversation is not recorded: its recording would not start
        self.failed = False             # the last recording stopped because it could not be written on

    @property
    def call(self) -> Call | None:
        """The conversation being recorded now."""
        return self._call

    def tick(self) -> None:
        """One look at the microphone."""
        now = self._clock()
        if self._recorder is not None and self._recorder.failed is not None:
            # the disk is full, or gone: keep what was recorded, and do not begin again while this call lasts
            self.failed = True
            for event in self._watcher.stop(now):
                self._handle(event, now)
            self._watcher.silence(now)
            return
        for event in self._watcher.poll(now, self._holders(self._cfg.microphone)):
            self._handle(event, now)

    def _handle(self, event: Event, now: float) -> None:
        if isinstance(event, Started):
            self._begin(event.program, now)
        elif self._recorder is None:
            return                      # the recording of this conversation never began
        elif isinstance(event, Released):
            self._recorder.release_mic()
        elif isinstance(event, Resumed):
            self._recorder.resume_mic()
        elif isinstance(event, Ended):
            self._end(event.at)

    def _begin(self, program: str, now: float) -> None:
        name = Path(program).stem
        call = None
        try:
            call = self._store.begin(self._today(), name, self._title(program))
            recorder = self._make_recorder()    # asks Windows about the headset: it may be gone by now
            recorder.start(self._store.folder(call), now)
        except Exception as e:
            if call is not None:
                self._store.discard(call.id)
            self._watcher.stop(now)     # as if it had not begun: the next look tries again
            if not self.refused:        # said once, not every second
                log.warning("a conversation in %s is not being recorded: %s", name, e)
            self.refused = True
            return
        self.refused = self.failed = False
        self._call, self._recorder = call, recorder
        log.info("recording a conversation in %s", name)

    def _end(self, at: float) -> None:
        call, recorder = self._call, self._recorder
        self._call = self._recorder = None
        seconds = recorder.stop(at)
        if seconds < self._cfg.min_seconds:
            self._store.discard(call.id)
            log.info("%.0f s in %s: too short for a conversation, dropped", seconds, call.program)
        else:
            self._store.finish(call.id, seconds)
            log.info("a conversation of %.0f s in %s is recorded", seconds, call.program)

    def discard_current(self) -> None:
        """Stop the recording that is going on and throw it away; this conversation is not recorded again."""
        if self._recorder is None:
            return
        call, recorder = self._call, self._recorder
        self._call = self._recorder = None
        recorder.abort()
        self._store.discard(call.id)
        self._watcher.silence(self._clock())
        log.info("the recording of a conversation in %s was thrown away", call.program)

    def close(self) -> None:
        """The service is leaving: a conversation that is going on ends here and is kept."""
        now = self._clock()
        for event in self._watcher.stop(now):
            self._handle(event, now)

    def run(self, stop: threading.Event) -> None:
        """Look at the microphone until told to stop."""
        try:
            while not stop.wait(self._cfg.poll_seconds):
                try:
                    self.tick()
                except Exception:
                    log.exception("a look at the microphone failed")
        finally:
            self.close()
