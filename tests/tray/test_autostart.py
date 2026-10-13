"""Start with Windows: the entry and the command in it. The registry is the faked one of conftest."""
import subprocess
import sys
from pathlib import Path

from protocallist.tray import autostart


def test_the_entry_is_there_exactly_while_it_is_switched_on(registry):
    assert autostart.enabled() is False
    autostart.enable(Path("C:/protocallist"), "C:/python/pythonw.exe")
    assert autostart.enabled() is True
    assert list(registry.values) == ["Protocallist"]
    assert registry.opened[-1].endswith(r"CurrentVersion\Run")
    autostart.disable()
    assert autostart.enabled() is False
    autostart.disable()                                # switching off what is off is not an error


def test_the_command_runs_the_program_without_a_window_from_wherever_it_lies(registry):
    autostart.enable(Path(r"C:\Users\O'Brien\protocallist"), r"C:\py\pythonw.exe")
    command = registry.values["Protocallist"]
    assert command.startswith(r'"C:\py\pythonw.exe" -c "import sys; sys.path.insert(0, ')
    assert command.endswith('from protocallist.__main__ import main; main()"')
    assert command.count('"') == 4                     # an apostrophe in the path must not end the payload
    payload = command.split('-c "', 1)[1][:-1]
    compile(payload, "<autostart>", "exec")            # and it is Python


def test_healing_rewrites_an_entry_that_is_there_and_makes_none_that_is_not(registry):
    autostart.heal(Path("C:/new/place"), "C:/python/pythonw.exe")
    assert registry.values == {}
    autostart.enable(Path("C:/old/place"), "C:/python/pythonw.exe")
    autostart.heal(Path("C:/new/place"), "C:/python/pythonw.exe")
    assert "new" in registry.values["Protocallist"] and "old" not in registry.values["Protocallist"]


def test_an_entry_made_under_the_name_of_before_becomes_the_entry_of_the_program(registry):
    """While the program was called Protokolist in Latin letters its entry bore that name and ran that package."""
    registry.values["Protokolist"] = '"C:/old/pythonw.exe" -c "from protokolist.__main__ import main; main()"'
    autostart.heal(Path("C:/where/it/is"), "C:/python/pythonw.exe")
    assert list(registry.values) == ["Protocallist"] and autostart.enabled() is True
    assert "from protocallist.__main__ import main" in registry.values["Protocallist"] and "C:/python/pythonw.exe" in registry.values["Protocallist"]
    autostart.heal(Path("C:/where/it/is"), "C:/python/pythonw.exe")      # and that was all: nothing of before is left to take over
    assert list(registry.values) == ["Protocallist"]


def test_the_windowless_interpreter_is_the_one_next_to_ours():
    assert Path(autostart.pythonw()) == Path(sys.executable).with_name("pythonw.exe")


def test_the_registered_payload_really_starts_the_program(tmp_path):
    """Run the payload in a real interpreter with a stand-in for the package: the folder
    that is put first on the path must be the one the program is imported from."""
    package = tmp_path / "protocallist"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("def main():\n    print('started from', __file__)\n", encoding="utf-8")
    command = autostart.command(tmp_path, sys.executable)
    payload = command.split('-c "', 1)[1][:-1]
    out = subprocess.run([sys.executable, "-c", payload], capture_output=True, text=True, cwd=tmp_path.parent)
    assert "started from" in out.stdout and str(tmp_path) in out.stdout, out.stderr
