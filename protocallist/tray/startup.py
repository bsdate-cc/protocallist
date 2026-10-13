"""The window behind protocallist.cmd: a few checks, each a line on the screen and in
startup.log, then the program is started without a window.

Every check is a function that prints nothing and returns what it found, so each can be
tried on its own. English only: this window is for the moment when something is wrong.
Standard library and the settings only - the libraries are among the things checked.
"""
from __future__ import annotations

import ctypes
import importlib
import msvcrt
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, NamedTuple

from protocallist import APP_NAME, VERSION, config, paths
from protocallist.tray import relaunch, updater


class Check(NamedTuple):
    name: str
    status: str                     # "OK" | "WARN" | "FAIL"
    summary: str
    details: tuple[str, ...] = ()


MIN_PYTHON = (3, 12)
# import name -> what pip installs
LIBRARIES = (("numpy", "numpy"), ("pyaudiowpatch", "PyAudioWPatch"), ("pycaw", "pycaw"),
             ("pystray", "pystray"), ("PIL", "Pillow"), ("soundfile", "soundfile"))
STEPS = 5
START_WAIT_S = 8.0
BY_HAND = r"by hand: .venv\Scripts\python -m pip install -r requirements.txt"


# ---- the checks ----

def check_python(version: tuple | None = None, maxsize: int | None = None) -> Check:
    v = tuple(version or sys.version_info[:3])
    bits = 64 if (maxsize or sys.maxsize) > 2**32 else 32
    summary = f"{'.'.join(map(str, v))}, {bits}-bit"
    if v[:2] < MIN_PYTHON:
        return Check("Python", "FAIL", summary, ("Python 3.12 or newer is needed",))
    return Check("Python", "OK", summary)


def check_libraries(import_module: Callable | None = None, stale: Callable[[], bool] | None = None) -> Check:
    """Every library imports - and, when `stale` is given, requirements.txt is the one they
    were installed from: a pinned version that moved imports fine and is still the wrong one."""
    imp = import_module or importlib.import_module
    missing = []
    for module, package in LIBRARIES:
        try:
            imp(module)
        except Exception:
            missing.append(package)
    if missing:
        return Check("Libraries", "FAIL", "missing: " + ", ".join(missing), (BY_HAND,))
    if stale is not None and stale():
        return Check("Libraries", "FAIL", "requirements.txt changed since the last install", (BY_HAND,))
    return Check("Libraries", "OK", "all in place")


def check_data() -> Check:
    try:
        base = paths.home()
        probe = base / ".startup-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except paths.Busy as e:
        return Check("Data folder", "FAIL", "held by the program under its name of before", (str(e),))
    except OSError as e:
        return Check("Data folder", "FAIL", "cannot be written to", (str(e),))
    return Check("Data folder", "OK", str(base))


def check_settings() -> Check:
    """No file is not a problem - a new installation runs on defaults, with recording off.
    A file that cannot be read is: nothing is recorded then, and saying so here turns
    "it does not record" into a five-second fix."""
    exists = paths.config_file().exists()
    loaded = config.load()
    details = tuple(f"{p.where}: {p.message}" for p in loaded.problems[:5])
    if config.unparsed(loaded):
        return Check("Settings", "WARN", "config.json cannot be read - nothing will be recorded", details)
    recording = loaded.config.recording
    if not recording.enabled:
        doing = "recording is off - switch it on from the tray menu"
    elif not recording.microphone:
        doing = "no microphone chosen - choose one from the tray menu"
    else:
        doing = f"recording conversations on {recording.microphone}"
    summary = doing if exists else f"no file yet - {doing}"
    return Check("Settings", "WARN" if loaded.problems else "OK", summary, details)


def format_line(i: int, result: Check) -> str:
    """`[2/5] Libraries ......... OK    all in place`; details on indented lines."""
    dots = "." * max(2, 18 - len(result.name))
    head = f"[{i}/{STEPS}] {result.name} {dots} {result.status:<5} {result.summary}".rstrip()
    return "\n".join([head, *(" " * 8 + line for line in result.details)])


# ---- the program that may be running already ----

def running() -> bool:
    """Whether a Protocallist holds the lock of the one running program."""
    try:
        handle = open(paths.lock_file(), "a+")
    except OSError:
        return False
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return True
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        handle.close()


def read_pid() -> int | None:
    """The process the running program said it is; None if it said nothing we can use."""
    try:
        pid = int(paths.pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def image_of(pid: int) -> str:
    """The file name of the program that runs under this number now; "" if it cannot be asked."""
    from protocallist.micholders import program_of     # ctypes only: the libraries are not needed for it

    return program_of(pid)


def _terminate(pid: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def stop_running(say: Callable[[str], None], *, is_running: Callable[[], bool] | None = None,
                 pid: Callable[[], int | None] | None = None, terminate: Callable[[int], None] | None = None,
                 image: Callable[[int], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """End the running program. True: nothing holds the lock any more - the proof is the
    lock going free, never the return of a kill. A process is ended only when the program
    itself said which one it is, and only while a Python still runs under that number: a
    number left by a program that died is a number Windows gives to something else, and
    something unrelated would die for it. What it was recording is put in order at the next start."""
    is_running = running if is_running is None else is_running
    number = (read_pid if pid is None else pid)()
    if not is_running():
        return True
    if number is None:
        say("  the running program did not say which process it is - close it by hand "
            "(Exit in its menu, or Ctrl+C in its window)")
        return False
    if (image_of if image is None else image)(number).lower() not in ("python.exe", "pythonw.exe"):
        say(f"  process {number}, which the running program named, is not this program any more - "
            "close the program by hand (Exit in its menu, or Ctrl+C in its window)")
        return False
    (_terminate if terminate is None else terminate)(number)
    deadline = clock() + 5.0
    while is_running() and clock() < deadline:
        sleep(0.2)
    if is_running():
        say("  the running program did not stop")
        return False
    say("  stopped")
    return True


# ---- the console ----

def ask_yes(prompt: str, default: bool = True, input_fn=input, isatty: bool | None = None) -> bool:
    """Y/n. A stdin that is not a console answers no without blocking: nothing is installed
    or stopped unattended. default=False is for the question whose yes ENDS something - a
    bare Enter there must mean "leave it alone"."""
    tty = sys.stdin.isatty() if isatty is None else isatty
    if not tty:
        return False
    yes = ("", "y", "yes", "д", "да") if default else ("y", "yes", "д", "да")
    try:
        return input_fn(prompt).strip().lower() in yes
    except EOFError:
        return False


def hold(reason: str) -> None:
    try:
        input(f"\n{reason} - press Enter to close this window... ")
    except EOFError:
        pass


def pip_install(root: Path) -> bool:
    """pip in view: its output goes to the console, never to the log (an index URL can carry credentials)."""
    try:
        return subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt")],
                              cwd=str(root)).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_requirements(root: Path, say: Callable[[str], None], *, is_running: Callable[[], bool] | None = None,
                         stop: Callable[[Callable[[str], None]], bool] | None = None,
                         pip: Callable[[Path], bool] | None = None, mark: Callable[[Path], None] | None = None,
                         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                         wait_s: float = 10.0) -> bool:
    """The update door of the launcher (--install-requirements): the libraries are installed
    with the program gone - it holds the files pip replaces. The program that opened this
    window is on its way out; it is given a moment, then stopped like any other."""
    is_running = running if is_running is None else is_running
    deadline = clock() + wait_s
    while is_running() and clock() < deadline:
        sleep(0.2)
    if is_running() and not (stop_running if stop is None else stop)(say):
        return False
    say("Installing the libraries from requirements.txt...")
    if not (pip_install if pip is None else pip)(root):
        return False
    (updater.mark_installed if mark is None else mark)(root)
    return True


def spawn(root: Path) -> None:
    """Start the program without a window and let go of it: it lives on when this window closes."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    with relaunch.stderr_file() as last_words:
        subprocess.Popen([str(pythonw), "-m", "protocallist"], cwd=str(root), env={**os.environ, "PYTHONPATH": str(root)},
                         creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=last_words, close_fds=True)


def last_words(lines: int = 8) -> tuple[str, ...]:
    """The end of what the windowless program said before it died - a traceback, as a rule."""
    try:
        said = (paths.logs_dir() / relaunch.STDERR).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(said.strip().splitlines()[-lines:])


class _Log:
    """startup.log: the same lines the window shows, with the time of the run."""

    def __init__(self) -> None:
        try:
            self._file = open(paths.logs_dir() / "startup.log", "a", encoding="utf-8")
            self._file.write(f"\n=== protocallist.cmd, {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except OSError:
            self._file = None

    def line(self, text: str) -> None:
        if self._file is not None:
            try:
                self._file.write(text + "\n")
                self._file.flush()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    """0: the program runs (started now, or left running). 1: it does not.
    With --install-requirements (the program hands over to this window when an update
    changed requirements.txt) the libraries are installed first, without questions."""
    args = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parents[2]
    log = _Log()

    def say(text: str) -> None:
        print(text, flush=True)
        log.line(text)

    def show(i: int, result: Check) -> Check:
        say(format_line(i, result))
        return result

    def stale() -> bool:
        return updater.libraries_stale(root)

    say(f"{APP_NAME} {VERSION}")
    if "--install-requirements" in args:
        say("Update: installing the libraries, then starting the program again.")
        if not install_requirements(root, say):
            hold(f"The libraries were not installed - {APP_NAME} was NOT started. To try again: protocallist.cmd")
            return 1
    results = [show(1, check_python())]
    libraries = show(2, check_libraries(stale=stale))
    if libraries.status == "FAIL" and results[0].status == "OK" and ask_yes("        Install them now? [Y/n] "):
        say("Installing the libraries from requirements.txt...")
        if pip_install(root):
            updater.mark_installed(root)
            importlib.invalidate_caches()
            libraries = show(2, check_libraries())      # they are what requirements.txt says now: pip saw to it
    data = show(3, check_data())
    results += [libraries, data]
    if data.status != "FAIL":       # the settings are in that folder: without it there is nowhere to look for them
        results.append(show(4, check_settings()))
    if any(result.status == "FAIL" for result in results):
        hold(f"{APP_NAME} was NOT started")
        return 1
    if updater.installed_digest() is None:
        updater.mark_installed(root)    # an install older than the record: everything imports, so this is it

    if running():
        say(format_line(5, Check("Program", "OK", "running already")))
        if not ask_yes("        Restart it? A conversation that is being recorded now would be cut in two. [y/N] ",
                       default=False):
            say("        left running")
            if any(result.status == "WARN" for result in results):
                hold("There are warnings above")
            return 0
        if not stop_running(say):
            hold(f"{APP_NAME} was NOT restarted")
            return 1
    spawn(root)
    deadline = time.monotonic() + START_WAIT_S
    while not running() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not running():
        say(format_line(5, Check("Program", "FAIL", "did not start",
                                 (rf"see protocallist.log and {relaunch.STDERR} in {paths.logs_dir()}", *last_words()))))
        hold(f"{APP_NAME} was NOT started")
        return 1
    say(format_line(5, Check("Program", "OK", "started - look for its icon in the tray")))
    if any(result.status == "WARN" for result in results):
        hold("There are warnings above")
    return 0


def run(argv: list[str] | None = None) -> int:
    """What protocallist.cmd runs. Always 0: wherever the walk went wrong this window has held
    itself open and has been read; the `|| pause` of protocallist.cmd is for a Python that blew up."""
    main(argv)
    return 0


if __name__ == "__main__":
    sys.exit(run())
