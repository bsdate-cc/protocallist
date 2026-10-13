"""What Windows says about microphones, asked for real: nothing is opened and nothing is recorded."""
import os
import subprocess
import sys
from pathlib import Path

from protocallist import micholders


def test_the_microphones_of_this_computer_have_names():
    assert all(isinstance(name, str) and name for name in micholders.microphones())


def test_a_microphone_that_is_not_there_has_no_holders_and_no_earphones():
    assert micholders.holders("no such microphone") is None
    assert micholders.speaker_of("no such microphone") is None


def test_the_holders_of_a_real_microphone_are_programs_and_never_this_one():
    for name in micholders.microphones():
        found = micholders.holders(name)
        assert isinstance(found, list) and all(program.lower().endswith(".exe") for program in found)


def test_a_process_is_named_by_its_file():
    assert micholders.program_of(os.getpid()).lower() in ("python.exe", "pythonw.exe")
    assert micholders.program_of(0) == ""


def test_a_program_without_windows_has_no_title():
    assert micholders.window_title("no-such-program.exe") == ""


ANOTHER_THREAD = """
import threading
from protocallist import micholders

micholders.microphones()                    # the first thread asks first
said = []

def ask():
    micholders.this_thread()
    try:
        said.append((isinstance(micholders.microphones(), list), micholders.holders("no such microphone, surely")))
    finally:
        micholders.leave_thread()

thread = threading.Thread(target=ask)
thread.start()
thread.join()
print(said)
"""


def test_another_thread_can_ask_once_it_has_prepared_itself():
    """The service behind the tray icon asks from a thread of its own. In a process of its
    own: among the other tests some library has made every thread ready already, and the
    question would be answered whatever this_thread() did."""
    done = subprocess.run([sys.executable, "-c", ANOTHER_THREAD], cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "[(True, None)]", done.stderr
