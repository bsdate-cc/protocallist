"""Handing the program over to a new process.

The program cannot restart itself in place - the code it runs is already in memory, and
a second one gives up while the first holds the lock. So the old one starts a successor
and exits. Two successors:

- the quiet one (`pythonw -m protocallist.tray.relaunch`): waits for the lock to come
  free and becomes the program, importing the code as it is on disk now;
- the launcher window (`python -m protocallist.tray.startup --install-requirements`), for
  when requirements.txt changed: pip must run with the program gone - it holds the files
  pip replaces - and in a window that shows what went wrong.

Standard library only.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from protocallist import paths

log = logging.getLogger("protocallist")

WAIT_S = 20.0
STEP_S = 0.2
STDERR = "stderr.log"


def wait_until_free(running: Callable[[], bool], sleep: Callable[[float], None] = time.sleep,
                    timeout: float | None = None, clock: Callable[[], float] = time.monotonic) -> bool:
    """True once no program is running; False if the old one is still there after `timeout`."""
    deadline = clock() + (WAIT_S if timeout is None else timeout)
    while running():
        if clock() >= deadline:
            return False
        sleep(STEP_S)
    return True


def _windowless_python() -> str:
    return str(Path(sys.executable).with_name("pythonw.exe"))


def _console_python() -> str:
    """python.exe next to the running pythonw.exe: the launcher window needs a console interpreter."""
    exe = Path(sys.executable)
    python = exe.with_name("python.exe")
    return str(python if python.exists() else exe)


def stderr_file():
    """Where a windowless Python says what it can say nowhere else. Without it, a program that
    dies before its log is open - code on disk that does not even import - leaves no trace.
    Made anew at every start: it holds the last words of the last start."""
    return open(paths.logs_dir() / STDERR, "wb")


def _start(argv: list[str], root: Path, flags: int) -> None:
    env = {**os.environ, "PYTHONPATH": str(root)}
    if flags != subprocess.CREATE_NO_WINDOW:    # a window: everything is read in it
        subprocess.Popen(argv, cwd=str(root), env=env, creationflags=flags, close_fds=True)
        return
    with stderr_file() as last_words:
        subprocess.Popen(argv, cwd=str(root), env=env, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=last_words, close_fds=True)


def spawn(root: Path) -> None:
    """Start the quiet successor, detached from the program that is about to exit."""
    _start([_windowless_python(), "-m", "protocallist.tray.relaunch"], root, subprocess.CREATE_NO_WINDOW)


def spawn_console(root: Path) -> None:
    """Open the launcher window that installs the libraries and then starts the program."""
    _start([_console_python(), "-m", "protocallist.tray.startup", "--install-requirements"], root,
           subprocess.CREATE_NEW_CONSOLE)


def main(start: Callable[[], object] | None = None) -> None:
    from protocallist import logsetup
    from protocallist.tray import startup

    if not wait_until_free(startup.running):
        # The old program hangs while leaving - a sound device that will not close. Its icon is
        # gone already, so nobody would learn that nothing runs. It has had its time: it is
        # ended the way the launcher window ends one, and what it was recording is put in
        # order by the start that follows.
        logsetup.setup()
        log.warning("restart: the program that was running did not exit within %.0f s; ending it", WAIT_S)
        if not startup.stop_running(lambda text: log.warning("restart: %s", text.strip())):
            log.error("restart: the old program could not be ended; no new one was started - run protocallist.cmd")
            return
    if start is None:
        try:
            from protocallist.__main__ import tray as start
        except Exception:
            # The old program is gone and the code on disk is broken: say so where it will be looked for.
            logsetup.setup()
            log.exception("restart: the code on disk cannot be imported; no new copy was started - "
                          "mend it and run protocallist.cmd")
            return
    start()


if __name__ == "__main__":
    main()
