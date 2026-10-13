"""Start the program.

`pythonw -m protocallist` - what protocallist.cmd and the "start with Windows" entry run -
puts the icon into the tray; the service that records lives behind it.

`python -m protocallist --console` runs the same service in a console window instead, with
no icon: Ctrl+C stops it. It records only if config.json says so.

`python -m protocallist --microphones` prints the names of the microphones.
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from protocallist import APP_NAME, VERSION, config, logsetup, micholders, paths
from protocallist.app import App
from protocallist.config import RecordingConfig
from protocallist.panel import server
from protocallist.panel.api import Api
from protocallist.recorder import PortAudio, Recorder
from protocallist.service import Service, mend
from protocallist.store import Store
from protocallist.summaries import Summariser
from protocallist.transcriber import Transcriber


def single_instance():
    """The lock of the one running program, or None if another holds it. Keep the result
    alive: the lock goes when the file is closed."""
    handle = open(paths.lock_file(), "a+")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def publish_pid() -> None:
    """Say which process holds the lock, so that protocallist.cmd ends exactly this one. Best
    effort: a program that cannot write the file still runs - but it does not leave the
    number of an earlier one standing."""
    try:
        paths.pid_file().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        withdraw_pid()


def withdraw_pid() -> None:
    try:
        paths.pid_file().unlink()
    except OSError:
        pass


def heed_ctrl_c() -> None:
    """Have Windows deliver Ctrl+C to this process. A process started by one that ignores
    Ctrl+C - a launcher, a scheduler - inherits that, and the key would do nothing."""
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)


def serve(cfg: RecordingConfig, store: Store, holders=micholders.holders) -> Service:
    """The service that records with these settings, on the real microphone and sound library."""

    def make_recorder() -> Recorder:
        # asked anew for every conversation: the headset may have been plugged in a minute ago
        speaker = cfg.speaker or micholders.speaker_of(cfg.microphone)
        return Recorder(PortAudio(), cfg.microphone, speaker)

    return Service(cfg, store, holders, make_recorder, micholders.window_title)


def open_store() -> Store:
    return Store(paths.database_file(), paths.calls_dir())


def recognise(cfg: config.Config, store: Store, busy) -> Transcriber:
    """What turns the conversations of the archive into text, through the gateway the settings name."""
    return Transcriber(store, cfg, busy=busy)


def console() -> int:
    """The service in a console window. 0: it ran and was stopped; 1: the program runs
    already; 2: recording is off; 3: the database cannot be opened."""
    log = logsetup.setup()
    if not paths.config_file().exists():
        config.save(config.Config())            # something to edit
    loaded = config.load()
    for problem in loaded.problems:
        log.warning("config: %s: %s", problem.where, problem.message)
    cfg = loaded.config.recording
    if not cfg.enabled or not cfg.microphone:
        print(f"Recording is off. In {paths.config_file()} set recording.enabled to true and "
              f"recording.microphone to one of the names that `python -m protocallist --microphones` prints.")
        return 2
    lock = single_instance()
    if lock is None:
        print(f"{APP_NAME} is running already.")
        return 1
    publish_pid()
    try:
        store = open_store()
    except sqlite3.Error as e:
        log.error("the database cannot be opened (%s: %s): nothing is recorded", type(e).__name__, e)
        print(f"The database {paths.database_file()} cannot be opened ({e}). Nothing is recorded.")
        withdraw_pid()
        lock.close()
        return 3
    try:
        store.recover(mend, cfg.min_seconds)
        service = serve(cfg, store)
        stop = threading.Event()
        heed_ctrl_c()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: stop.set())
        log.info("%s %s is watching the microphone %s", APP_NAME, VERSION, cfg.microphone)
        print(f"{APP_NAME} {VERSION} is watching the microphone \"{cfg.microphone}\". Ctrl+C stops it.")
        service.run(stop)
        log.info("%s stopped", APP_NAME)
    finally:
        store.close()
        withdraw_pid()
        lock.close()
    return 0


def summarise(cfg: config.Config, store: Store) -> Summariser:
    """What makes the summaries of the archive, by the model or the command the settings name."""
    return Summariser(store, cfg.summary)


def panel(app, port: int) -> server.Panel:
    """The panel over the service behind the icon: on this port of this computer, and open to a
    browser that brings the key of this user."""
    return server.Panel(Api(app), port, server.key(paths.key_file()))


def tray() -> int:
    """The icon in the tray, until Exit is chosen. 0: it ran; 1: the program runs already."""
    lock = single_instance()
    if lock is None:
        return 1                                # a second one leaves without a word: the icon is there
    log = logsetup.setup()
    log.info("%s %s starting", APP_NAME, VERSION)
    publish_pid()
    try:
        try:
            from protocallist.tray import autostart
            from protocallist.tray.app import TrayApp
        except Exception:
            # A missing library: under pythonw this would be an icon that never appears,
            # with nothing anywhere to say why.
            log.exception("the icon could not be made: run protocallist.cmd, it checks and installs the libraries")
            raise
        root = Path(__file__).resolve().parents[1]
        try:
            autostart.heal(root, autostart.pythonw())
        except OSError:
            log.exception("the entry that starts the program with Windows could not be brought up to date")

        def make_app(on_change) -> App:
            return App(open_store=open_store, make_service=serve, holders=micholders.holders,
                       microphones=micholders.microphones, begin=micholders.this_thread,
                       end=micholders.leave_thread, on_change=on_change, make_transcriber=recognise,
                       has_archive=lambda: paths.database_file().exists(), make_summariser=summarise)

        TrayApp(make_app, root, autostart.pythonw(), panel).run()
    except Exception:
        log.exception("%s failed", APP_NAME)
        raise
    finally:
        withdraw_pid()
        lock.close()
        log.info("%s stopped", APP_NAME)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--microphones" in args:
        for name in micholders.microphones():
            print(name)
        return 0
    if "--console" in args:
        return console()
    return tray()


if __name__ == "__main__":
    sys.exit(main())
