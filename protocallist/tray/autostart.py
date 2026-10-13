"""Start with Windows: one value under HKCU\\...\\Run, per user, no admin rights.

The value itself is the setting: there, the program starts when the user logs in; gone, it
does not. A Run entry sets neither environment nor working directory, so the command
bootstraps sys.path inline and runs under pythonw (no console window).
"""
from __future__ import annotations

import sys
from pathlib import Path

_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "Protocallist"
BEFORE = "Protokolist"      # the value the program started with Windows by while it bore that name


def pythonw() -> str:
    """The windowless interpreter next to the one that runs this."""
    return str(Path(sys.executable).with_name("pythonw.exe"))


def _pyliteral(s: str) -> str:
    """A single-quoted Python string literal, always: repr() would switch to double
    quotes for a path with an apostrophe, and a double quote ends the -c payload."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def command(root: Path, interpreter: str) -> str:
    payload = (f"import sys; sys.path.insert(0, {_pyliteral(str(root))}); "
               "from protocallist.__main__ import main; main()")
    return f'"{interpreter}" -c "{payload}"'


def _there(value: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
            winreg.QueryValueEx(key, value)
        return True
    except OSError:
        return False


def enabled() -> bool:
    return _there(VALUE)


def enable(root: Path, interpreter: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command(root, interpreter))


def _remove(value: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value)
    except OSError:
        pass


def disable() -> None:
    _remove(VALUE)


def heal(root: Path, interpreter: str) -> None:
    """If the program starts with Windows, make the entry point at where it is now: a
    folder that was moved, or an environment that was made anew, would leave it dead. An
    entry made under the name of before becomes the entry of the program: that one runs a
    package that is not there any more."""
    before = _there(BEFORE)
    if before or enabled():
        enable(root, interpreter)
    if before:
        _remove(BEFORE)
