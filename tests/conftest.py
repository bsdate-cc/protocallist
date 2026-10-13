import logging
import sys
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest

from protocallist import paths


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data folder; the real one is never touched."""
    monkeypatch.setenv(paths.ENV, str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-code-here"))     # nor are its settings read
    return paths.home()


@pytest.fixture(autouse=True)
def russian(monkeypatch):
    """Every test starts in Russian, whatever the language of this Windows: the texts the
    tests name are the Russian ones, and "auto" must not depend on the machine."""
    from protocallist import i18n

    monkeypatch.setattr(i18n, "system_language", lambda: "ru")
    i18n.set_language("ru")
    yield
    i18n.set_language("ru")


class FakeRegistry:
    """One registry key with its values, in memory: as much of winreg as the program uses."""
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.opened: list[str] = []

    def OpenKey(self, hive, key, reserved=0, access=0):
        self.opened.append(key)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """No test reads or writes the real registry: "start with Windows" of the machine the
    tests run on must stay what its user made it."""
    fake = FakeRegistry()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


@pytest.fixture(autouse=True)
def libraries_record(tmp_path, monkeypatch):
    """The record of which requirements.txt the libraries came from lives in the real
    environment; no test may read or write that one."""
    from protocallist.tray import updater

    path = tmp_path / "protocallist-requirements.sha256"
    monkeypatch.setattr(updater, "stamp_file", lambda: path)
    return path


@pytest.fixture(autouse=True)
def nothing_real_is_shown_or_started(monkeypatch):
    """No test puts an icon into the tray of the machine it runs on, or starts the program
    there. A test that needs an icon brings a fake one; one that reaches the real thing
    fails at once instead of sitting in the tray. Returns the real functions for the tests
    of those very functions, which fake `subprocess` under them."""
    import pystray

    from protocallist.tray import relaunch, startup

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real tray icon or the real start of the program")

    real = SimpleNamespace(spawn=startup.spawn, pip_install=startup.pip_install,
                           successor=relaunch.spawn, launcher_window=relaunch.spawn_console)
    monkeypatch.setattr(pystray, "Icon", refuse)
    monkeypatch.setattr(startup, "spawn", refuse)
    monkeypatch.setattr(startup, "pip_install", refuse)
    monkeypatch.setattr(startup, "_terminate", refuse)
    monkeypatch.setattr(relaunch, "spawn", refuse)
    monkeypatch.setattr(relaunch, "spawn_console", refuse)
    return real


@pytest.fixture(autouse=True)
def detach_log_files():
    """logsetup.setup() opens a file in the temp home; let go of it after the test."""
    yield
    for name in ("protocallist", "pystray"):
        log = logging.getLogger(name)
        for handler in [h for h in log.handlers if isinstance(h, RotatingFileHandler)]:
            log.removeHandler(handler)
            handler.close()
