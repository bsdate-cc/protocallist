"""The pages a newcomer reads - README.md and README.ru.md - say what the program does: its settings with
their defaults, the rows of its menu, where it keeps things, which Tolmach it needs."""
import json
import re
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from protocallist import audio, config, gateway, i18n, paths

ROOT = Path(__file__).resolve().parents[1]
PAGES = {"en": "README.md", "ru": "README.ru.md"}
# The rows of the tray menu, as the program names them in Russian: the pages tell about every one of them.
ROWS = ("Открыть панель", "Записывать разговоры", "Остановить и удалить эту запись", "Микрофон для разговоров",
        "Открыть папку разговоров", "Повторить распознавание", "Открыть файл настроек", "Открыть журнал",
        "Запускать вместе с Windows", "Проверить обновления", "Выход")
# What the panel says, by the names its texts are kept under.
PANEL = ("Make a summary", "make again", "recognise anew…", "Delete the conversation", "save as a file",
         "Hide shorter than")
both = pytest.mark.parametrize("language", list(PAGES))


def page(language: str) -> str:
    return (ROOT / PAGES[language]).read_text(encoding="utf-8")


def settings(of=None, prefix: str = "") -> dict:
    """Every setting by its name in config.json, with its default."""
    of = config.Config() if of is None else of
    out = {}
    for f in fields(of):
        value = getattr(of, f.name)
        out.update(settings(value, f"{prefix}{f.name}.") if is_dataclass(value) else {prefix + f.name: value})
    return out


def test_each_page_leads_to_the_other():
    assert "](README.ru.md)" in page("en") and "](README.md)" in page("ru")


def test_the_two_pages_are_one_page_in_two_languages():
    def parts(text):        # its parts, the rows of its tables, its paragraphs
        return (len(re.findall(r"^## ", text, re.MULTILINE)), len(re.findall(r"^\| ", text, re.MULTILINE)),
                len(re.split(r"\n\s*\n", text.strip())))

    def commands(text):
        return re.findall(r"^```\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)

    assert parts(page("en")) == parts(page("ru")) and parts(page("en"))[0] >= 8
    assert commands(page("en")) == commands(page("ru")) != []          # what is typed is typed the same in any language


def test_the_program_is_fetched_and_started_as_the_pages_say():
    # ".\" - a terminal of Windows 11 runs nothing from the folder it is in without it
    start = "git clone https://github.com/bsdate-cc/protocallist.git\ncd protocallist\n.\\protocallist.cmd\n"
    assert start in page("en") and (ROOT / "protocallist.cmd").is_file()


@both
def test_a_link_into_the_page_finds_its_part(language):
    said = page(language)
    parts = {"#" + heading.lower().replace(" ", "-") for heading in re.findall(r"^## (.+)$", said, re.MULTILINE)}
    links = set(re.findall(r"\]\((#[^)]+)\)", said))
    assert links and links <= parts


@both
def test_the_table_of_settings_names_every_setting_with_its_default_and_no_other(language):
    table = re.findall(r"^\| `([a-z_.]+)` \| `(.+?)` \|", page(language), re.MULTILINE)
    told = {name: json.loads(default) for name, default in table}
    assert told == settings() and len(table) == len(settings())
    switches = {name for name, default in settings().items() if isinstance(default, bool)}
    assert {name for name, default in told.items() if isinstance(default, bool)} == switches     # 0 is not "off"


def test_the_rows_the_pages_tell_about_are_the_rows_of_the_menu():
    tray = (ROOT / "protocallist" / "tray" / "app.py").read_text(encoding="utf-8")
    menu = tray[tray.index("    def _menu("):tray.index("    def _recording(")]
    rows = set(re.findall(r'item\(lambda i: _\("([^"]+)"\)', menu)) - {"Обновления: {note}"}      # a note, not a row to choose
    assert rows | {"Проверить обновления"} == set(ROWS)      # that one is drawn by a function of its own: it says when, too


@both
def test_the_rows_of_the_menu_are_named_as_the_menu_names_them(language):
    i18n.set_language(language)
    said = page(language)
    assert [row for row in ROWS if f"| **{i18n._(row)}** |" not in said] == []
    assert i18n._("Обновить до {version}").format(version="X.Y.Z") in said


@both
def test_the_tolmach_that_is_needed_is_the_one_the_program_asks_for(language):
    needed = ".".join(map(str, gateway.MIN_VERSION))
    assert f"](https://github.com/bsdate-cc/tolmach) {needed} " in page(language)


@both
def test_things_are_where_the_pages_say(language):
    folder = "%USERPROFILE%\\" + paths.DEFAULT_FOLDER
    names = (folder + "\\config.json", folder + "\\", paths.database_file().name, paths.key_file().name,
             f"logs\\{paths.NAME}.log", "logs\\startup.log", paths.ENV)
    assert [name for name in names if f"`{name}`" not in page(language)] == []


@both
def test_what_the_panel_says_is_quoted_as_the_panel_says_it(language):
    texts = json.loads((ROOT / "protocallist" / "panel" / "static" / "texts.json").read_text(encoding="utf-8"))
    quoted = {"en": '"{}"', "ru": "«{}»"}[language]
    assert [key for key in PANEL if quoted.format(texts[key][language]) not in page(language)] == []


@both
def test_the_request_a_server_of_ones_own_gets_is_the_one_the_pages_tell_of(language):
    client = (ROOT / "protocallist" / "gateway.py").read_text(encoding="utf-8")
    words = ("/v1/audio/transcriptions", "response_format", "verbose_json", "segments", "/v1/models", "Authorization", "Bearer")
    assert [word for word in words if word not in client or word not in page(language)] == []
    assert f"{audio.RATE // 1000} {dict(en='kHz', ru='кГц')[language]}" in page(language)


@both
def test_the_files_a_page_points_at_are_there(language):
    named = re.findall(r"\]\((?!https?://|#)([^)]+)\)", page(language))
    assert len(named) >= 3 and [name for name in named if not (ROOT / name).is_file()] == []
