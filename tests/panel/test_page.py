"""The page of the panel: its texts in two languages, and - where there is a browser on this machine - the
page itself, as a browser without a window shows it after its script has asked the server and drawn."""
import html
import json
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from protocallist import gateway, i18n, transcriber
from protocallist.app import Glance
from protocallist.i18n import Text
from protocallist.panel import server
from protocallist.panel.api import Answer
from protocallist.panel.server import Panel
from protocallist.store import Phrase
from protocallist.summary import SummaryError

from .conftest import AT, ENGLISH, request

SECRET = "the-secret-of-this-test-0123456789abcdef"
TALK = (("me", "Начнём с плана."), ("them", "Давайте."), ("me", "Про лицензию узнаю сегодня."))
SUMMARY = "# Выжимка\n\n## Коротко\nДоговорились о переезде в четверг.\n\n## Задачи\n- [00:01:15] Узнать про лицензию.\n- Без времени.\n"


def texts() -> dict:
    return json.loads((server.STATIC / "texts.json").read_text(encoding="utf-8"))


def named_in_the_script() -> set[str]:
    script = (server.STATIC / "app.js").read_text(encoding="utf-8")
    return {found.replace('\\"', '"') for found in re.findall(r'\b[tN]\("((?:[^"\\]|\\.)*)"', script)}


def test_every_text_of_the_page_is_there_in_both_languages_and_none_is_left_over():
    known = texts()
    assert named_in_the_script() == set(known)
    for key, both in known.items():
        assert set(both) == {"ru", "en"} and both["ru"].strip() and both["en"].strip(), key
        holes = [sorted(re.findall(r"\{(\w+)\}", text)) for text in (key, both["ru"], both["en"])]
        assert holes[0] == holes[1] == holes[2], key                    # the same values in all three


def test_the_page_puts_nothing_into_markup_and_loads_nothing_from_outside():
    """What was said is shown as text: the script never builds markup out of it. And the page works
    without a network."""
    script = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(", script)
    for name in ("index.html", "app.js", "app.css"):
        text = (server.STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://|//cdn|@import|url\(", text), name
    page = (server.STATIC / "index.html").read_text(encoding="utf-8")
    assert '<html lang="ru" translate="no">' in page and '<meta name="google" content="notranslate">' in page
    fields = len(re.findall(r'el\("(?:textarea|input", \{ type: "search")', script))
    assert script.count('spellcheck: "false"') == script.count('writingsuggestions: "false"') == fields == 2
    assert "<script src=\"/app.js\" defer></script>" in page and "style=" not in page and "onclick" not in page


def test_the_files_of_the_page_come_with_the_program(world):
    panel = Panel(world.api, 0, SECRET)
    panel.start()
    try:
        for path in ("/", "/app.js", "/app.css", "/texts.json"):
            status, headers, body = request(panel.port, "GET", path, cookie=panel.secret)
            assert status == 200 and len(body) > 500, path
    finally:
        panel.stop()


# --- the page as a browser shows it


def browser() -> str | None:
    for name in ("msedge", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(path).is_file():
            return path
    return None


seen = pytest.mark.skipif(browser() is None, reason="no browser on this machine to read the page with")


def shown(address: str, budget: int = 6000) -> str:
    """The page at this address after its script has run - read from a browser that shows no window.
    `budget` is how long the page may go on for, in milliseconds of its own time."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:        # the browser may still be letting go of it
        done = subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                               "--disable-background-networking", "--disable-component-update",
                               f"--virtual-time-budget={budget}", "--dump-dom", address], capture_output=True, timeout=120)
    return done.stdout.decode("utf-8", "replace")


def whole(page: str) -> bool:
    """Nothing on the page is a value left unfilled or a thing that is not there, written as a word."""
    said = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", page, flags=re.S))
    return not re.search(r"\{\w+\}|\bnull\b|\bundefined\b|\bNaN\b", said)


@pytest.fixture
def served(world):
    talk = world.call(*TALK, seconds=4441.0)
    world.store.set_topic(talk.id, "Начнём с плана. Давайте.")
    world.store.sound(talk).write_bytes(b"OggS")
    world.store.save_summary(talk.id, "made", SUMMARY, AT + timedelta(hours=2), "opus", 64.0)
    world.store.set_topic(talk.id, "Договорились о переезде в четверг.")
    failed = world.store.finish(world.store.begin(AT.replace(hour=10), "Zoom").id, 61.0)
    for _ in range(3):
        world.store.take()
        world.store.missed(failed.id, "the gateway answered 500")
    panel = Panel(world.api, 0, SECRET)
    panel.start()
    yield panel
    panel.stop()


@seen
def test_the_list_is_drawn_with_what_the_archive_holds(served):
    page = shown(served.link())
    assert '<html lang="ru"' in page and "<title>Протоколист</title>" in page
    assert "Договорились о переезде в четверг." in page and "74 мин" in page and "Discord" in page
    assert "не распознано" in page and "повторить" in page and "текста ещё нет" in page
    assert "Показано 2 из 2" in page and "ждёт разговора" not in page and "запись выключена" in page
    assert "Выбрано: 0" in page and "Сделать выжимки: 0" in page and "уйдёт в Claude (модель opus)" in page
    assert 'title="выбрать все, у которых нет выжимки"' in page and ">сделать</button>" not in page
    assert 'aria-pressed="true"' in page and 'aria-pressed="false"' in page     # a switch says which way it stands, either way
    assert whole(page)


@seen
def test_a_conversation_is_drawn_with_its_text_its_summary_and_its_player(served):
    page = shown(served.link() + "#/call/1")
    assert "Начнём с плана." in page and "Про лицензию узнаю сегодня." in page and "Собеседники" in page
    assert "<h3>Коротко</h3>" in page and "Узнать про лицензию." in page and "Без времени." in page
    assert '<h3 class="title">Выжимка</h3>' in page                      # the summary whole: its first line too
    assert ">00:00:10</button>" in page and ">00:01:15</button>" in page         # of a remark, and of a point of the summary
    assert "сделать заново" in page and "править" not in page and "<textarea" not in page
    assert "· opus · 64 с" in page and 'class="badge"' not in page      # who made it is what the archive says, nothing else
    assert '<audio src="/api/calls/1/audio"' in page and "01:14:01" in page
    assert 'href="/api/calls/1/summary.md"' in page and "/api/calls/1/text.md" in page
    assert "Удалить запись" in page and "2026-10-06_15-55-33_Discord.opus" in page
    assert whole(page)


@seen
def test_the_parts_of_a_conversation_stand_one_under_another_and_fold(served):
    """A summary, when there is one, is what the page is opened for: it stands on top, open, and the text
    waits folded under it. The details are a narrow strip under the player, one line when it is opened."""
    page = shown(served.link() + "#/call/1")
    player, facts = page.index('class="panel player"'), page.index('<details class="panel fold slim" id="facts-fold">')
    summary, text = page.index('<details class="panel fold" id="sum-fold" open="">'), page.index('<details class="panel fold" id="text-fold">')
    assert player < facts < summary < text
    assert page.count("сделана 06.10 17:55 · opus · 64 с") == 1          # in the head of the summary: seen when it is folded too
    assert "по прежнему тексту" not in page
    line = page[page.index('<div class="factline" id="facts">'):summary]
    assert "<b>Звук</b>" in line and "<b>Текст</b>реплик: 3 · gigaam-v3" in line and "<b>Выжимка</b>есть" in line
    assert "<b>Файлы</b>2026-10-06_15-55-33_Discord.opus, 2026-10-06_15-55-33_Discord.md" in line
    assert "<dl" not in page and "<b>Программа</b>" not in page          # one line, not a table; the program is in the head


@seen
def test_a_conversation_without_a_summary_shows_its_text_and_one_line_where_the_summary_will_be(served, world):
    plain = world.call(*TALK, minutes_later=30)
    page = shown(served.link() + f"#/call/{plain.id}")
    assert 'id="sum-fold"' not in page and '<details class="panel fold" id="text-fold" open="">' in page
    assert "<b>Выжимки ещё нет.</b>" in page and ">Сделать выжимку</button>" in page
    assert "Щелчок отправит текст этого разговора в Claude (модель opus). Больше ничего никуда не уходит." in page
    assert page.index('class="nosum"') < page.index('id="text-fold"') and whole(page)
    waiting = shown(served.link() + "#/call/2")                         # not recognised: there is nothing to make a summary of
    assert "Выжимку можно будет сделать, когда разговор распознается." in waiting and ">Сделать выжимку</button>" not in waiting
    assert 'id="sum-fold"' not in waiting and "Текста ещё нет: разговор не распознан." in waiting and whole(waiting)


@seen
def test_the_page_names_a_command_of_ones_own_as_where_the_text_goes(served, world):
    world.service.cfg.summary.command = ["my-tool", "--brief"]
    page = shown(served.link())
    assert "Текст выбранных разговоров уйдёт команде: my-tool --brief." in page
    assert "Claude" not in re.sub(r"<script.*?</script>", "", page, flags=re.S) and whole(page)


@seen
def test_the_page_speaks_the_language_of_the_program(served):
    i18n.set_language("en")
    page = shown(served.link())
    assert '<html lang="en"' in page and "not recognised" in page and "Shown 2 of 2" in page and "recording is off" in page
    assert whole(page)


@seen
def test_one_who_comes_without_the_key_sees_only_where_to_open_the_panel_from(served):
    page = shown(f"http://127.0.0.1:{served.port}/")
    assert "Откройте панель из значка Протоколиста" in page and "переезде" not in page

# --- the page as it answers to clicks

# Runs in the page after its own script: clicks through the list and a conversation, and leaves what it
# saw as JSON in the page, where the test reads it.
DRIVER = r"""
(async () => {
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const until = async (test) => { for (let i = 0; i < 400 && !test(); i++) await wait(25); return test(); };
  const rows = () => [...document.querySelectorAll("tr.row")].map((row) => `${row.children[2].textContent} ${row.children[5].textContent}`);
  const click = (node) => node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  const named = (text) => [...document.querySelectorAll("button, a")].find((node) => node.textContent.trim().startsWith(text));
  const type = (node, value, event) => { node.value = value; node.dispatchEvent(new Event(event || "input", { bubbles: true })); };
  const out = {};
  try {
    await until(() => document.querySelector("tr.row"));
    out.first = rows();
    const keys = {};                             // what the "play" and "pause" keys of a keyboard or a headset are bound to
    const bind = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
    navigator.mediaSession.setActionHandler = (key, does) => { keys[key] = does; bind(key, does); };
    await act("POST", "/api/calls/999/retry");
    out.trouble = [!$("trouble").hidden, $("trouble").textContent];
    const dot = document.querySelector("#now .dot");
    await tick();
    out.top = dot === document.querySelector("#now .dot");
    const hide = document.querySelector(".filters input[type=checkbox]");
    hide.checked = false; hide.dispatchEvent(new Event("change", { bubbles: true }));
    out.all = rows();
    click(named("Zoom")); out.zoom = rows();
    click(named("Все"));
    click(named("Длит.")); out.longest = rows();
    click(named("Длит.")); out.shortest = rows();
    click(named("Время"));
    type(document.querySelector(".filters input[type=search]"), "ЛИЦЕНЗИ");
    await until(() => S.found); out.found = rows();
    S.found = new Set(); showList();           // back at the list after a while: what was found then may be stale
    out.again = Boolean(await until(() => S.found && S.found.has(1)));
    const kept = ask;
    let broken = true;
    ask = (method, path, body) => (path === "/api/calls" && broken ? Promise.resolve({ ok: false, status: 0, data: {} }) : kept(method, path, body));
    S.calls = []; S.status.stamp = "another";
    await tick(); broken = false; await tick(); ask = kept;
    out.reread = S.calls.length;
    type(document.querySelector(".filters input[type=search]"), "");
    location.hash = "#/call/2";                  // a conversation without sound: nothing new can go wrong on its page
    await until(() => S.call && S.call.id === 2);
    out.cleared = $("trouble").hidden;
    location.hash = "#/call/1";
    await until(() => named("сделать заново"));
    out.title = document.querySelector("h1").textContent;
    click(document.querySelectorAll(".remark button.t")[2]); out.at = document.getElementById("t-now").textContent;
    const fields = [...document.querySelectorAll("input[type=search]")];
    out.spell = fields.map((node) => node.getAttribute("spellcheck")); out.suggest = fields.map((node) => node.getAttribute("writingsuggestions"));
    const heads = () => [...document.querySelectorAll(".sum h3")].map((node) => node.textContent);
    out.heads = heads();
    const left = S.audio;                        // the player of this conversation: it will be left behind
    let played = 0;
    left.play = () => { played += 1; return Promise.resolve(); };
    out.keys = [typeof keys.play, typeof keys.pause];
    keys.play();                                 // the key of the headset: it plays what is on the screen
    out.key = played;
    const fold = (id) => document.getElementById(id);
    const folds = () => ["sum-fold", "text-fold", "facts-fold"].map((id) => fold(id).open);
    out.folds = folds();
    fold("text-fold").querySelector("summary").click();
    fold("facts-fold").querySelector("summary").click();
    await refreshCall();                         // the page is drawn anew: what the user opened stays open...
    out.opened = folds();
    fold("sum-fold").querySelector("summary").click();
    await refreshCall();                         // ...and what they folded stays folded
    out.folded = folds();
    fold("sum-fold").querySelector("summary").click();
    out.edit = Boolean(named("править") || document.querySelector("textarea"));
    click(named("сделать заново"));
    out.question = (document.querySelector(".sumfoot .sure") || {}).textContent || "";
    click(named("Нет"));
    await tick();
    out.unasked = [Boolean(named("сделать заново")), S.status.making.length + S.status.queued.length];
    click(named("сделать заново"));
    const yes = named("Да");
    click(yes); click(yes);                      // a double click is one request, and no complaint
    await until(() => document.querySelector(".sumnote"));
    out.busy = (document.querySelector(".sumnote") || {}).textContent || "";
    out.twice = $("trouble").hidden;
    await until(() => document.querySelector(".sum").textContent.includes("Говорили о плане."));
    out.remade = [heads(), Boolean(await until(() => named("сделать заново")))];
    const real = ask;
    let release = null;
    const held = new Promise((done) => { release = done; });
    ask = (method, path, body) => (path === "/api/calls/1" ? held.then(() => real(method, path, body)) : real(method, path, body));
    const late = refreshCall();                 // asks about conversation 1 - and the answer is slow to come
    location.hash = "#/call/3";
    await until(() => S.call && S.call.id === 3 && document.querySelector("h1"));
    release(); await late; ask = real;
    out.other = [S.call.id, document.querySelector("h1").textContent];
    out.gone = [left.hasAttribute("src"), S.audio !== left, typeof keys.play];      // the sound that was left is let go of, not just paused
    left.dispatchEvent(new Event("play"));       // ...and whatever it still says is no business of the player on the screen
    out.own = $("play").textContent;
    out.none = [Boolean(fold("sum-fold")), fold("text-fold").open, Boolean(named("Сделать выжимку"))];
    click(named("Сделать выжимку"));
    await until(() => fold("sum-fold"));         // the summary came while the page was open: folded, where the button was
    out.came = [fold("sum-fold").open, fold("text-fold").open, Boolean(document.querySelector(".nosum"))];
    location.hash = "#/call/1";
    await until(() => S.call && S.call.id === 1 && named("Удалить запись"));
    out.anew = folds();                          // every conversation is opened by the same rule, whatever was folded in another
    click(named("Удалить запись")); out.asked = Boolean(named("Да"));
    click(named("Нет")); out.kept = Boolean(named("Удалить запись"));
    click(named("Удалить запись")); click(named("Да"));
    await until(() => !S.call && document.querySelector("#rows") && !S.calls.some((call) => call.id === 1));
    out.after = rows();
    out.free = [keys.play === null, keys.pause === null];       // no player on the screen: the keys play nothing
    left.dispatchEvent(new Event("error"));      // nor is a trouble of the player that was left a trouble of this screen
    out.quiet = $("trouble").hidden;
  } catch (e) { out.error = String((e && e.stack) || e); }
  const note = document.createElement("pre");
  note.id = "driven"; note.textContent = JSON.stringify(out);
  document.body.append(note);
})();
"""


def driven(world, tmp_path, driver: str, budget: int) -> dict:
    """What a script saw that was run in the page after the page's own: the page is served with it added."""
    static = tmp_path / "static"
    shutil.copytree(server.STATIC, static)
    with open(static / "app.js", "a", encoding="utf-8", newline="\n") as script:
        script.write(driver)
    panel = Panel(world.api, 0, SECRET, static=static)
    panel.start()
    try:
        page = shown(panel.link(), budget=budget)
    finally:
        panel.stop()
    found = re.search(r'<pre id="driven">(.*?)</pre>', page, flags=re.S)
    assert found, page[-2000:]
    saw = json.loads(html.unescape(found[1]))
    assert "error" not in saw, saw["error"]
    return saw


@seen
def test_the_page_does_what_is_clicked(world, tmp_path):
    talk = world.call(*TALK, seconds=4441.0)
    world.store.sound(talk).write_bytes(b"OggS")
    world.store.save_summary(talk.id, "made", SUMMARY, AT + timedelta(hours=2), "opus", 64.0)
    world.store.set_topic(talk.id, "Договорились о переезде в четверг.")
    failed = world.store.finish(world.store.begin(AT.replace(hour=10), "Zoom").id, 61.0)
    for _ in range(3):
        world.store.take()
        world.store.missed(failed.id, "the gateway answered 500")
    short = world.call(("me", "Проверка связи."), minutes_later=5, program="Zoom", seconds=12.0)
    world.store.sound(short).write_bytes(b"OggS")
    seen = threading.Event()                     # the page was told that a summary is being made
    world.during = lambda: seen.wait(20)         # ...and only then does "Claude" answer
    answer = world.api.handle

    def watched(method, path, query, body):
        told = answer(method, path, query, body)
        if path == "/api/status" and told.data["making"]:
            seen.set()
        return told

    world.api.handle = watched
    saw = driven(world, tmp_path, DRIVER, 40000)
    assert saw["trouble"] == [True, "этот разговор не ждёт повтора"] and saw["cleared"] is True     # a notice goes when the user moves on
    assert saw["top"] is True                                                  # the top line is left alone while it says the same
    assert saw["again"] is True and saw["reread"] == 3                        # the search and the list are asked for again
    assert saw["first"] == ["Discord 74 мин", "Zoom 1 мин"]                      # the latest first; the short one is hidden
    assert saw["all"] == ["Zoom 12 с", "Discord 74 мин", "Zoom 1 мин"]           # the tick taken off
    assert saw["zoom"] == ["Zoom 12 с", "Zoom 1 мин"]
    assert saw["longest"] == ["Discord 74 мин", "Zoom 1 мин", "Zoom 12 с"] and saw["shortest"] == saw["longest"][::-1]
    assert saw["found"] == ["Discord 74 мин"]                                    # by what was said, whatever the case
    assert saw["title"] == "Договорились о переезде в четверг." and saw["at"] == "00:00:10"
    assert saw["heads"] == ["Выжимка", "Коротко", "Задачи"] and saw["edit"] is False       # shown whole; not to be written by hand
    assert saw["question"] == "Заменить выжимку новой? Да / Нет" and saw["unasked"] == [True, 0]     # one click sends nothing
    assert saw["busy"] == "Готовится новый вариант… Пока показан прежний." and saw["twice"] is True
    assert saw["remade"] == [["Выжимка", "Коротко"], True]
    assert saw["folds"] == [True, False, False]                                # a summary there is: it is open, the text folded under it
    assert saw["opened"] == [True, True, True] and saw["folded"] == [False, True, True]     # as the user left them
    assert saw["none"] == [False, True, True] and saw["came"] == [False, True, False]      # nothing moves under one who reads
    assert saw["anew"] == [True, False, False] and len(world.asked) == 2
    assert saw["keys"] == ["function", "function"] and saw["key"] == 1          # the keys of a headset are the player's on the screen
    assert saw["gone"] == [False, True, "function"] and saw["own"] == "▶"       # a player that was left cannot come back to life
    assert saw["free"] == [True, True] and saw["quiet"] is True
    assert saw["spell"] == saw["suggest"] == ["false"]                          # what is typed is no spell checker's business
    assert saw["other"] == [3, "Zoom, 12 с"]                                   # a late answer about one conversation is not drawn into another
    assert saw["asked"] is True and saw["kept"] is True                         # one click deletes nothing; "no" takes the question back
    assert saw["after"] == ["Zoom 12 с", "Zoom 1 мин"]
    assert world.store.get(talk.id) is None and world.store.get(short.id) is not None and not world.store.sound(talk).exists()


# Ticks conversations in the list and sends them to be summarised; "Claude" refuses some, as the test tells it to.
DRIVER_OF_THE_LINE = r"""
(async () => {
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const until = async (test) => { for (let i = 0; i < 2400 && !test(); i++) await wait(25); return test(); };
  const click = (node) => node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  const named = (text) => [...document.querySelectorAll("button, a")].find((node) => node.textContent.trim().startsWith(text));
  const row = (id) => document.querySelector(`tr.row[data-id="${id}"]`);
  const cell = (id) => row(id).children[8].textContent;                 // the column of the summary
  const box = (id) => row(id).querySelector("input[type=checkbox]");
  const choose = (id) => { box(id).checked = true; box(id).dispatchEvent(new Event("change", { bubbles: true })); };
  const bar = (id) => (document.getElementById(id) || { textContent: "" }).textContent;
  const chosen = () => bar("chosen").slice(0, 10);
  const asking = () => document.querySelector("#asking:not([hidden]) .ask");
  const out = {};
  try {
    await until(() => document.querySelector("tr.row"));
    out.start = [bar("chosen"), named("Сделать выжимки").disabled, document.querySelectorAll("tr.row input[type=checkbox]").length, Boolean(box(4)), Boolean(box(5))];
    const all = document.querySelector("thead input[type=checkbox]");
    all.checked = true; all.dispatchEvent(new Event("change", { bubbles: true }));
    out.all = [chosen(), document.querySelectorAll("tr.row.chosen").length, document.querySelector("thead input[type=checkbox]").checked];
    click(named("снять выделение"));
    out.none = [chosen(), document.querySelectorAll("tr.row input:checked").length];
    click(box(1).parentNode);                    // a click beside the box is not a click on the conversation
    out.stayed = location.hash;
    choose(1);
    click(named("Zoom"));                        // another choice of what is shown: what was ticked is not on the screen...
    out.hidden = [chosen(), document.querySelectorAll("tr.row").length];
    click(named("Все"));                         // ...and what is not on the screen is not sent: the tick is gone with it
    out.back = [chosen(), document.querySelectorAll("tr.row input:checked").length];
    box(1).focus();
    choose(1);
    out.focus = document.activeElement === box(1);      // the keyboard stays where it was
    choose(2); choose(3);
    out.three = [chosen(), named("Сделать выжимки").textContent];
    click(named("Сделать выжимки"));
    out.at_once = [chosen(), document.querySelectorAll("tr.row input:checked").length, Boolean(box(1)), cell(2)];
    await until(() => cell(3) === "готовится");
    out.going = [cell(3), cell(2), cell(1), bar("going")];
    await until(() => document.getElementById("stood"));
    out.stood = [cell(3), cell(2), cell(1), bar("stood"), Boolean(box(2)), Boolean(box(1)), Boolean(document.getElementById("going"))];
    location.hash = "#/call/6";
    await until(() => S.call && S.call.id === 6 && named("Сделать выжимку"));
    click(named("Сделать выжимку"));
    out.one = [Boolean(asking()), asking() ? asking().querySelector(".hint").textContent : ""];
    click(named("отмена"));
    out.closed = !asking();
    click(named("Сделать выжимку"));
    location.hash = "#/";                        // the question belongs to the screen it was asked on
    await until(() => !S.call && row(6));
    out.left = !asking();
    choose(6);
    S.status = Object.assign({}, S.status, { stopped: null });       // the page has not heard yet that the line stopped
    click(named("Сделать выжимки"));
    await until(() => asking());                 // the program says so, and the page asks
    out.many = [Boolean(asking()), asking() ? asking().querySelector("p").textContent : "", Boolean(box(6) && box(6).checked)];
    const last = named("отмена");
    last.focus();
    last.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    out.inside = document.activeElement.textContent;      // the keyboard does not leave the question
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    out.escaped = [!asking(), Boolean(box(6) && box(6).checked), chosen()];
    click(named("Сделать выжимки"));
    click(named("Добавить в очередь"));
    await until(() => [1, 2, 6].every((id) => cell(id) === "есть"));
    out.kept = [cell(1), cell(2), cell(6), Boolean(document.getElementById("stood")), chosen()];
    choose(7); choose(8);
    click(named("Сделать выжимки"));
    await until(() => document.getElementById("stood"));
    out.again = [cell(8), cell(7)];
    click(named("сбросить"));
    await until(() => !document.getElementById("stood"));
    out.reset = [cell(8), cell(7), Boolean(box(8))];
    choose(7); choose(8);
    click(named("Сделать выжимки"));
    await until(() => document.getElementById("stood"));
    choose(9);
    click(named("Сделать выжимки"));
    click(named("Запустить заново"));           // the line that stood is forgotten: only what is chosen goes
    await until(() => cell(9) === "есть");
    out.dropped = [cell(9), cell(8), cell(7), Boolean(document.getElementById("stood"))];
    choose(7); choose(8);
    click(named("Сделать выжимки"));
    await until(() => document.getElementById("stood"));
    click(named("Перезапустить"));
    await until(() => cell(7) === "есть" && cell(8) === "есть");
    out.restarted = !document.getElementById("stood");
    choose(10); choose(11);
    click(named("Сделать выжимки"));
    await until(() => cell(11) === "готовится");
    click(named("остановить очередь"));
    await until(() => cell(10) === "нет");
    out.halted = [cell(10), Boolean(box(10))];
    await until(() => cell(11) === "есть");
    out.finished = [cell(11), cell(10), Boolean(await until(() => !document.getElementById("going")))];
  } catch (e) { out.error = String((e && e.stack) || e); }
  const note = document.createElement("pre");
  note.id = "driven"; note.textContent = JSON.stringify(out);
  document.body.append(note);
})();
"""


@seen
def test_the_page_sends_what_is_ticked_to_be_summarised_one_after_another(world, tmp_path):
    for n in range(11):                          # 1..11, a minute apart: the latest is at the top of the list
        if n == 4:
            world.store.finish(world.store.begin(AT.replace(minute=4), "Zoom").id, 600.0)      # 5: not recognised yet
        else:
            world.call(*TALK, minutes_later=n - 55, seconds=600.0)
    world.store.save_summary(4, "made", SUMMARY, AT, "opus", 64.0)
    refused, waiting, last = {2, 6, 7, 9}, [], [None]     # which requests "Claude" refuses, counted from the first

    def claude():
        mine = threading.Event()
        waiting.append(mine)
        mine.wait(20)                            # until the page has drawn that this summary is being made
        if len(world.asked) in refused:
            raise SummaryError(Text("Claude ответил ошибкой {status} — см. журнал", status=403))

    world.during = claude
    answer = world.api.handle

    def watched(method, path, query, body):
        """The page asks how things stand, then for the list, then draws: when it is told the same of the
        summary being made for the second time, it has drawn the first."""
        told = answer(method, path, query, body)
        if path == "/api/status":
            making = tuple(told.data["making"]) or None
            if making is not None and making == last[0]:
                while waiting:
                    waiting.pop().set()
            last[0] = making
        return told

    world.api.handle = watched
    saw = driven(world, tmp_path, DRIVER_OF_THE_LINE, 300000)
    where = "Текст выбранных разговоров уйдёт в Claude (модель opus). Выжимки делаются по очереди, примерно по минуте на разговор."
    assert saw["start"] == [f"Выбрано: 0{where}Сделать выжимки: 0", True, 9, False, False]     # the bar is there with nothing chosen
    assert saw["all"] == ["Выбрано: 9", 9, True] and saw["none"] == ["Выбрано: 0", 0] and saw["stayed"] in ("", "#/")
    assert saw["hidden"] == ["Выбрано: 0", 1] and saw["back"] == ["Выбрано: 0", 0]       # what is not shown is not chosen
    assert saw["focus"] is True and saw["inside"] == "Запустить заново"
    assert saw["three"] == ["Выбрано: 3", "Сделать выжимки: 3"]
    assert saw["at_once"] == ["Выбрано: 0", 0, False, "в очереди"]                # the ticks go at the click
    assert saw["going"] == ["готовится", "в очереди", "в очереди",
                            "Выжимки: готовится 1, в очереди 2Страницу можно закрыть — очередь идёт в самой программе.остановить очередь"]
    assert saw["stood"] == ["есть", "не получилось", "не отправлен",
                            "Очередь остановленаClaude ответил ошибкой 403 — см. журналПерезапустить: 2сбросить", True, True, False]
    assert saw["one"] == [True, "«Запустить заново» — прежняя очередь сбрасывается, идёт только этот разговор. "
                                "«Добавить в очередь» — прежняя запускается снова, этот разговор идёт первым."]
    assert saw["closed"] is True and saw["left"] is True
    assert saw["many"] == [True, "Разговоров в ней: 2. Причина остановки: Claude ответил ошибкой 403 — см. журнал. "
                                 "Что с ней сделать, отправляя выбранное?", True]
    assert saw["escaped"] == [True, True, "Выбрано: 1"]                         # the question taken back: the tick stays
    assert saw["kept"] == ["есть", "есть", "есть", False, "Выбрано: 0"]
    assert saw["again"] == ["не получилось", "не отправлен"] and saw["reset"] == ["нет", "нет", True]
    assert saw["dropped"] == ["есть", "нет", "нет", False] and saw["restarted"] is True
    assert saw["halted"] == ["нет", True] and saw["finished"] == ["есть", "нет", True]
    names = {call.folder: call.id for call in world.store.calls()}
    assert [names[asked["label"]] for asked in world.asked] == [3, 2, 2, 1, 6, 8, 8, 9, 8, 8, 7, 11]


@seen
def test_the_player_comes_when_the_sound_is_ready(world):
    """A conversation opened right after it was recognised has no sound to play yet: its recordings
    are still being made small. The player must come by itself when they are."""
    talk = world.call(*TALK, seconds=600.0)
    answer, asked = world.api.handle, []

    def watched(method, path, query, body):
        if path == "/api/status":
            asked.append(path)
            if len(asked) == 3:
                world.store.sound(talk).write_bytes(b"OggS")
        return answer(method, path, query, body)

    world.api.handle = watched
    panel = Panel(world.api, 0, SECRET)
    panel.start()
    try:
        page = shown(panel.link() + f"#/call/{talk.id}", budget=12000)
    finally:
        panel.stop()
    assert len(asked) >= 3 and f'<audio src="/api/calls/{talk.id}/audio"' in page
    assert "ещё не готов к прослушиванию" not in page and whole(page)


# --- recognising a conversation anew, with another model of the gateway

NEW = [Phrase("me", 0.0, 1.5, "Let us begin."), Phrase("them", 2.0, 3.0, "Go ahead.")]
AS_MANY = NEW + [Phrase("me", 4.0, 5.0, "Thanks.")]         # another text of as many remarks as TALK has
ANEW = ">распознать заново…</button>"


@seen
def test_a_conversation_can_be_recognised_anew_when_its_sound_is_kept_and_the_gateway_has_a_choice(served, world):
    page = shown(served.link() + "#/call/1")
    assert ANEW in page and '<div id="anew-note"></div>' in page and 'id="text-count">реплик: 3</span>' in page and whole(page)
    assert page.index('id="text-acts"') < page.index("/api/calls/1/text.md") < page.index(ANEW) < page.index('id="remarks"')
    plain = world.call(*TALK, minutes_later=30)                          # its sound is not there
    assert ANEW not in shown(served.link() + f"#/call/{plain.id}")
    assert ANEW not in shown(served.link() + "#/call/2")                 # not recognised: it is tried again, with the main model
    world.clock += 5
    world.known = None                                                  # the gateway is not there, or is an older one
    silent = shown(served.link() + "#/call/1")
    assert ANEW not in silent and "/api/calls/1/text.md" in silent and whole(silent)
    answer = world.api.handle
    world.api.handle = lambda method, path, query, body: Answer(500, {"error": "boom"}) if path == "/api/models" \
        else answer(method, path, query, body)
    broken = shown(served.link() + "#/call/1")                          # the question itself failed: the page is whole without it
    assert ANEW not in broken and "Про лицензию узнаю сегодня." in broken and "/api/calls/1/text.md" in broken and whole(broken)


@seen
def test_a_conversation_that_is_recognised_anew_says_how_that_stands(served, world):
    world.store.redo(1, ENGLISH)
    page = shown(served.link() + "#/call/1")
    assert ('<div id="anew-note"><p class="sumnote"><b>Распознаётся заново: английский (parakeet-unified-en)…</b> '
            'Прежний текст на месте, пока не готов новый. <button type="button" class="linkbtn">отменить</button></p></div>') in page
    assert "Начнём с плана." in page and ANEW not in page and whole(page)
    # the conversation has a summary, so its text is folded: how it stands is in the head of the part, seen folded
    assert '<details class="panel fold" id="text-fold">' in page and 'id="text-count">реплик: 3 · распознаётся заново</span>' in page
    assert '<span class="state">распознаётся заново</span>' in shown(served.link())      # the list says so too
    world.store.redo_failed(1, ENGLISH, transcriber.NO_MODEL)
    page = shown(served.link() + "#/call/1")
    assert '<p class="sumnote bad">Распознать заново не получилось: модель не установлена в Толмаче</p>' in page
    assert ANEW in page and "Начнём с плана." in page and whole(page)    # the text is there, and it can be asked for again
    assert 'id="text-count">реплик: 3 · распознать заново не получилось</span>' in page
    world.store.redo(1, ENGLISH)
    world.store.redone(1, NEW, ENGLISH, AT + timedelta(days=2))
    page = shown(served.link() + "#/call/1")
    assert page.count("сделана 06.10 17:55 · opus · 64 с · по прежнему тексту") == 1       # the summary is not made anew by itself
    assert "<b>Текст</b>реплик: 2 · parakeet-unified-en" in page and 'id="text-count">реплик: 2</span>' in page
    assert "Let us begin." in page and "Начнём с плана." not in page and '<div id="anew-note"></div>' in page and whole(page)
    assert '<span class="state plain">распознан</span>' in shown(served.link())


# Runs in the page after its own script: asks for the conversation to be recognised anew, twice - recognition
# itself is stood in for by the test, and ends when this script says so.
DRIVER_ANEW = r"""
(async () => {
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const until = async (test) => { for (let i = 0; i < 400 && !test(); i++) await wait(25); return test(); };
  const click = (node) => node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  const named = (text) => [...document.querySelectorAll("button, a")].find((node) => node.textContent.trim().startsWith(text));
  const acts = () => $("text-acts").textContent, note = () => $("anew-note").textContent;
  let flying = 0;                                // the requests of the page that have no answer yet
  const plain = ask;
  ask = async (method, path, body) => { flying += 1; try { return await plain(method, path, body); } finally { flying -= 1; } };
  const settled = async () => { await until(() => !flying); await wait(0); };
  const remarks = () => [...document.querySelectorAll(".remark p")].map((node) => node.textContent);
  const out = {};
  try {
    location.hash = "#/call/1";
    await until(() => S.call && named("распознать заново"));
    out.closed = acts();
    click(named("распознать заново"));
    await until(() => named("английский"));
    out.choice = acts();
    out.absent = Boolean(named("немецкий"));     // not installed: it is said, and is nothing to click
    click(named("отмена"));
    out.cancelled = acts();
    click(named("распознать заново"));
    await until(() => named("английский"));
    click(named("английский"));
    out.question = acts();
    click(named("отмена"));
    click(named("распознать заново"));
    out.reopened = acts();                       // a question that was left is not asked by itself
    click(named("английский"));
    click(named("Нет"));
    await tick();
    out.unasked = [acts(), S.call.redo];
    click(named("английский"));
    const yes = named("Да");
    click(yes); click(yes);                      // a double click is one request, and no complaint
    await until(() => S.call.redo);
    out.waits = [note(), acts(), $("trouble").hidden, remarks().length];
    location.hash = "#/";
    await until(() => !S.call && document.querySelector("tr.row .state"));
    out.row = document.querySelector("tr.row .state").textContent;
    location.hash = "#/call/1";
    await until(() => S.call && $("anew-note") && note());
    click(named("отменить"));
    await until(() => !S.call.redo);
    out.back = [note(), acts(), remarks().length];       // taken back: as if it had not been asked for
    click(named("распознать заново"));
    await until(() => named("английский"));
    click(named("английский")); click(named("Да"));
    await until(() => S.call.redo);
    await ask("POST", "/api/test/failed");
    await tick();
    out.failed = [note(), acts(), remarks().length];
    click(named("распознать заново"));
    await until(() => named("английский"));
    click(named("английский")); click(named("Да"));
    await until(() => S.call.redo);
    await ask("POST", "/api/test/redone");
    await tick();
    out.done = [note(), remarks(), $("text-count").textContent, $("facts").textContent.includes("реплик: 3 · parakeet-unified-en"),
      document.querySelector("#sum-fold .count").textContent];
    click(named("распознать заново"));
    await until(() => named("русский"));
    out.now = acts();                            // the model the text is by now is marked - and any may be asked for again
    click(named("русский")); click(named("Да"));
    await until(() => S.call.redo);
    await ask("POST", "/api/test/redone");
    await tick();
    out.again = [remarks(), $("text-count").textContent];
    click(named("распознать заново"));
    await until(() => named("русский"));
    await settled();                             // what the gateway has was asked for at the click: its answer is in, and draws nothing later
    await ask("POST", "/api/test/refuse");       // the next request will be refused
    click(named("английский")); click(named("Да"));
    await until(() => !$("trouble").hidden);
    out.refused = [acts(), $("trouble").textContent];     // refused: the choice is closed all the same
    click(named("распознать заново"));
    await until(() => named("русский"));
    click(named("русский"));                     // ...with a question in it
    await ask("POST", "/api/test/asked");        // asked for in another tab, while the choice stands open in this one...
    await tick();
    await ask("POST", "/api/test/failed");
    await tick();
    out.elsewhere = [acts(), note(), $("text-count").textContent];     // ...and when that is over, it does not come back open by itself
    click(named("распознать заново"));
    await until(() => named("русский"));
    out.calm = !named("Да");                     // nor is the question of then asked by itself
    location.hash = "#/";
    await until(() => !S.call && document.querySelector("tr.row"));
    location.hash = "#/call/1";
    await until(() => S.call && named("распознать заново"));
    out.fresh = acts();                          // a conversation that is opened has its choice closed, whatever was open before
    const real = ask;
    ask = (method, path, body) => (path === "/api/models" ? Promise.resolve({ ok: false, status: 500, data: {} }) : real(method, path, body));
    await models();                              // the question itself failed...
    ask = real;
    out.unanswered = (() => { try { drawText(); return acts(); } catch (e) { return String(e); } })();     // ...and the page goes on drawing
    await refreshCall();                         // at the next news of the conversation the gateway is asked again
    out.came = Boolean(await until(() => named("распознать заново")));
  } catch (e) { out.error = String((e && e.stack) || e); }
  const seen = document.createElement("pre");
  seen.id = "driven"; seen.textContent = JSON.stringify(out);
  document.body.append(seen);
})();
"""


@seen
def test_the_page_asks_for_a_conversation_to_be_recognised_anew_and_shows_how_it_went(world, tmp_path):
    talk = world.call(*TALK, seconds=4441.0)
    world.store.sound(talk).write_bytes(b"OggS")
    world.store.save_summary(talk.id, "made", SUMMARY, AT + timedelta(hours=2), "opus", 64.0)
    others = [gateway.Model("another-de", "de", False, False), gateway.Model("odd", "no language at all", False, False)]
    world.known = [world.known[0], gateway.Model(ENGLISH, "en", False, False)] + others     # the English one: not downloaded yet
    answer, asked, texts, told, refuse = world.api.handle, [], [AS_MANY, NEW], [], []

    def stood_in(method, path, query, body):
        """What the page asks is answered as ever; recognition is stood in for, and ends when the script says so."""
        if path == "/api/test/failed":
            world.store.redo_failed(talk.id, ENGLISH, transcriber.NO_MODEL)
            return Answer(data={})
        if path == "/api/test/redone":
            world.store.redone(talk.id, texts.pop(0), world.store.get(talk.id).redo_model, AT + timedelta(days=2 + len(texts)))
            return Answer(data={})
        if path == "/api/test/asked":
            world.store.redo(talk.id, ENGLISH)
            return Answer(data={})
        if path == "/api/test/refuse":
            refuse.append(True)
            return Answer(data={})
        if path.endswith("/recognise") and method == "POST":
            if refuse:
                refuse.clear()
                return Answer(409, {"error": "нет"})
            asked.append(json.loads(body))
        if path == "/api/models":
            told.append(answer(method, path, query, body))
            world.clock += 5                     # the page was opened before the model was downloaded: the next answer says it is there
            world.known = [world.known[0], gateway.Model(ENGLISH, "en", False, True)] + others
            return told[-1]
        return answer(method, path, query, body)

    world.api.handle = stood_in
    saw = driven(world, tmp_path, DRIVER_ANEW, 60000)
    models = ("русский (gigaam-v3){} · английский (parakeet-unified-en){} · немецкий (another-de) — не установлена: скачайте в меню Толмача · "
              "odd — не установлена: скачайте в меню Толмача · ")
    assert [m["installed"] for m in told[0].data["models"]] == [True, False, False, False]      # as the page was opened
    assert saw["closed"] == "сохранить в файлраспознать заново…" and saw["cancelled"] == saw["closed"]
    assert saw["choice"] == "сохранить в файлКакой моделью? " + models.format(" — сейчас", "") + "отмена" and saw["absent"] is False
    assert saw["question"] == saw["choice"].replace("английский (parakeet-unified-en)", "английский (parakeet-unified-en) — заменить текст? Да / Нет")
    assert saw["reopened"] == saw["choice"] and saw["unasked"] == [saw["choice"], ""]     # one click asks for nothing
    assert saw["waits"] == ["Распознаётся заново: английский (parakeet-unified-en)… Прежний текст на месте, пока не готов новый. отменить",
                            "сохранить в файл", True, 3]
    assert saw["back"] == ["", saw["closed"], 3]
    assert saw["row"] == "распознаётся заново"
    assert saw["failed"] == ["Распознать заново не получилось: модель не установлена в Толмаче", saw["closed"], 3]
    assert saw["done"] == ["", ["Let us begin.", "Go ahead.", "Thanks."], "реплик: 3", True,      # another text of as many remarks
                           "сделана 06.10 17:55 · opus · 64 с · по прежнему тексту"]
    assert saw["now"] == "сохранить в файлКакой моделью? " + models.format("", " — сейчас") + "отмена"
    assert saw["again"] == [["Let us begin.", "Go ahead."], "реплик: 2"]
    assert saw["refused"] == [saw["closed"], "нет"]
    assert saw["elsewhere"] == [saw["closed"], "Распознать заново не получилось: модель не установлена в Толмаче",
                                "реплик: 2 · распознать заново не получилось"]
    assert saw["calm"] is True and saw["fresh"] == saw["closed"] and saw["unanswered"] == "сохранить в файл" and saw["came"] is True
    assert asked == [{"model": ENGLISH}] * 3 + [{"model": "gigaam-v3"}]           # the double click was one request


@seen
def test_a_conversation_that_waits_is_said_to_wait_for_whoever_is_asked_for_its_text(served, world):
    waiting = world.store.finish(world.store.begin(AT + timedelta(minutes=40), "Zoom").id, 61.0)
    world.service.shown = Glance(waiting=1, gateway="down")
    page = shown(served.link() + f"#/call/{waiting.id}")
    assert "Ждёт шлюз Толмача: разговор распознается сам, когда шлюз появится." in page
    world.service.cfg.gateway.url = "http://127.0.0.1:8000"              # a server of the user's own is not Tolmach
    page = shown(served.link() + f"#/call/{waiting.id}")
    assert "Ждёт сервис распознавания: разговор распознается сам, когда сервис ответит." in page and "Толмач" not in page
    assert whole(page)
