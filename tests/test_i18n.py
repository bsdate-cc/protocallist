"""Two languages for what the user reads: Russian is the source, English is looked up."""
import ast
import json
import re
from pathlib import Path

from protocallist import config, i18n, paths
from protocallist.i18n import Text, _


def test_russian_is_shown_as_written():
    assert _("Выход") == "Выход"


def test_english_is_looked_up():
    i18n.set_language("en")
    assert _("Выход") == "Exit"


def test_a_text_without_a_translation_is_shown_as_it_is():
    i18n.set_language("en")
    assert _("Привет, мир") == "Привет, мир"


def test_auto_follows_the_language_of_windows(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    i18n.set_language("auto")
    assert i18n.language() == "en"
    monkeypatch.setattr(i18n, "system_language", lambda: "ru")
    i18n.set_language("auto")
    assert i18n.language() == "ru"
    i18n.set_language("klingon")                      # anything unknown is "auto"
    assert i18n.language() == "ru"


def test_windows_in_russian_or_with_a_russian_regional_format_means_russian():
    russian, english = 0x0419, 0x0409
    assert i18n.pick_language(russian, russian) == "ru"
    assert i18n.pick_language(english, russian) == "ru"      # an English Windows of a Russian speaker
    assert i18n.pick_language(russian, english) == "ru"
    assert i18n.pick_language(english, english) == "en"


def test_the_language_follows_windows_unless_chosen():
    assert config.load().config.language == "auto"
    paths.config_file().write_text(json.dumps({"language": "en"}), encoding="utf-8")
    loaded = config.load()
    assert loaded.problems == [] and loaded.config.language == "en"
    assert tuple(i18n.LANGUAGES) == ("auto", "ru", "en")


def test_a_message_with_values_follows_the_language_of_the_moment_it_is_shown():
    note = Text("ветка {branch} — обновления выключены", branch="feature")
    assert str(note) == "ветка feature — обновления выключены"
    i18n.set_language("en")
    assert str(note) == "branch feature — updates are off"
    assert note == Text("ветка {branch} — обновления выключены", branch="feature")
    assert note != Text("ветка {branch} — обновления выключены", branch="main")
    assert len({note, Text("ветка {branch} — обновления выключены", branch="feature")}) == 1


def test_the_log_gets_a_message_in_english_whatever_the_language():
    assert i18n.english(Text("git merge не выполнен: {reason}", reason="exit 1")) == "git merge failed: exit 1"
    assert i18n.english("Выход") == "Exit"
    assert i18n.english("no translation for this") == "no translation for this"


def test_the_values_in_a_text_are_the_same_in_both_languages():
    for russian, english in i18n.EN.items():
        assert set(re.findall(r"{(\w+)}", russian)) == set(re.findall(r"{(\w+)}", english)), russian


# --- the catalogue against the code

PACKAGE = Path(__file__).resolve().parents[1] / "protocallist"


def marked_texts() -> set[str]:
    """Every string literal handed to _(), N_() or Text() anywhere in the program."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            first = node.args[0]
            if name in ("_", "N_", "Text") and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def test_everything_the_program_shows_has_an_english_text():
    missing = marked_texts() - set(i18n.EN)
    assert not missing, f"no English for: {sorted(missing)}"


def test_no_english_text_is_left_without_a_use():
    unused = set(i18n.EN) - marked_texts()
    assert not unused, f"translated but never shown: {sorted(unused)}"


def test_no_code_shows_a_russian_literal_past_the_catalogue():
    """A Cyrillic string literal outside _() / N_() / Text() would stay Russian in an English menu.
    Allowed: the catalogue itself, comments and docstrings (they are not shown), and the
    launcher window, which takes "д" and "да" for a yes."""
    cyrillic = re.compile("[А-Яа-яЁё]")
    marked = marked_texts()
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name in ("i18n.py", "startup.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and cyrillic.search(node.value)
                    and id(node) not in docstrings and node.value not in marked):
                offenders.append(f"{path.name}:{node.lineno}: {node.value[:40]!r}")
    assert not offenders, offenders
