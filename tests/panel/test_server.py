"""The server of the panel: who is answered and who is not, and how a file, a page and a download go out.
Real HTTP on a port of this machine; what answers the questions of the page is a stand-in."""
import json
import logging
import socket
import time

import pytest

from protocallist.panel import server
from protocallist.panel.api import Answer
from protocallist.panel.server import Panel

from .conftest import request

SECRET = "the-secret-of-this-test-0123456789abcdef"
SAID = "начнём с плана"


class Asked:
    """Stands in for the Api: remembers what it was asked and answers what the test tells it to."""

    def __init__(self) -> None:
        self.seen: list[tuple] = []
        self.answer = Answer(data={"said": SAID})
        self.error: Exception | None = None

    def handle(self, method, path, query, body) -> Answer:
        self.seen.append((method, path, query, body))
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.fixture
def pages(tmp_path):
    folder = tmp_path / "static"
    folder.mkdir()
    for name, text in (("index.html", "<!doctype html><title>Протоколист</title>"), ("app.js", "// the page"),
                       ("app.css", "body{}"), ("texts.json", "{}")):
        (folder / name).write_text(text, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for a browser", encoding="utf-8")
    return folder


@pytest.fixture
def served(pages):
    panel = Panel(Asked(), 0, SECRET, static=pages)
    panel.start()
    yield panel
    panel.stop()


def get(panel, path, **how):
    return request(panel.port, "GET", path, cookie=how.pop("cookie", panel.secret), **how)


def change(panel, method, path, body=b"", **headers):
    return request(panel.port, method, path, cookie=panel.secret, headers={"X-Protocallist": "1", **headers}, body=body)


# --- the key


def test_the_key_is_made_once_and_kept_in_its_file(tmp_path):
    path = tmp_path / "panel.key"
    first = server.key(path)
    assert len(first) >= 32 and path.read_text(encoding="ascii") == first and server.key(path) == first
    path.write_text("short", encoding="ascii")
    again = server.key(path)
    assert len(again) >= 32 and again != "short" and path.read_text(encoding="ascii") == again


def test_a_stranger_is_given_nothing_but_where_to_open_the_panel_from(served):
    for how in ({"cookie": None}, {"cookie": "a-guess"}):
        for path in ("/", "/app.js", "/api/calls", "/api/calls/1/audio"):
            status, headers, body = get(served, path, **how)
            assert status == 403 and "Откройте панель из значка" in body.decode("utf-8") and SAID.encode() not in body
    assert request(served.port, "POST", "/api/calls/1/summary", headers={"X-Protocallist": "1"})[0] == 403
    assert get(served, "/?key=a-guess", cookie=None)[0] == 403
    assert served.api.seen == []


def test_the_link_of_the_icon_turns_the_key_into_a_cookie_and_leaves_the_address_clean(served):
    assert served.link() == f"http://127.0.0.1:{served.port}/?key={SECRET}"
    status, headers, body = get(served, f"/?key={SECRET}", cookie=None)
    assert status == 302 and headers["Location"] == "/" and body == b""
    assert headers["Set-Cookie"] == f"protocallist_{served.port}={served.secret}; Path=/; HttpOnly; SameSite=Strict"
    status, headers, body = get(served, "/")
    assert status == 200 and "<title>Протоколист</title>" in body.decode("utf-8")


def test_a_tab_left_open_goes_on_working_after_the_program_is_started_again(pages):
    """The cookie holds the key of the file, which is the same at every start: a tab that was open
    when the program was restarted needs no new click on the icon."""
    before = Panel(Asked(), 0, SECRET, static=pages)
    before.start()
    status, headers, body = get(before, f"/?key={SECRET}", cookie=None)
    kept = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    port = before.port
    before.stop()
    after = Panel(Asked(), port, SECRET, static=pages)
    after.start()
    try:
        assert request(port, "GET", "/api/calls", cookie=kept)[0] == 200
    finally:
        after.stop()


def test_the_cookies_of_other_local_pages_do_not_lock_the_user_out(served):
    """Cookies are kept by the name of a computer, not by its port: those of every other page served
    from this computer come along, whatever they look like."""
    for others in ("a/b=1; ", "version=1; path=/; ", 'said="a b; ', "na\xefve=1; ", "lonely; =; ;; ", "x=" + "y" * 5000 + "; "):
        cookies = f"{others}protocallist_{served.port}={served.secret}; later=1"
        assert request(served.port, "GET", "/api/calls", headers={"Cookie": cookies})[0] == 200, others
    ours = f"protocallist_{served.port}"
    for wrong in (f"not{ours}={served.secret}", f"{ours}=x{served.secret}", ours, f"{ours}=", "",
                  f"protocallist={served.secret}", f"protocallist_{served.port + 1}={served.secret}"):
        assert request(served.port, "GET", "/api/calls", headers={"Cookie": wrong})[0] == 403, wrong


def test_a_cookie_of_the_same_name_that_another_page_set_does_not_lock_the_user_out(served):
    """Another local page can set a cookie of the panel's name - for a longer path, which a browser
    sends first. The right one among them is enough."""
    ours = f"protocallist_{served.port}"
    for cookies in (f"{ours}=stale; {ours}={served.secret}", f"{ours}={served.secret}; {ours}=stale", f"{ours}=; {ours}=a; {ours}={served.secret}"):
        assert request(served.port, "GET", "/api/calls", headers={"Cookie": cookies})[0] == 200, cookies
    assert request(served.port, "GET", "/api/calls", headers={"Cookie": f"{ours}=stale; {ours}=another"})[0] == 403


# --- the address


def test_only_a_request_meant_for_this_computer_and_this_port_is_answered(served):
    assert get(served, "/api/calls", host=f"localhost:{served.port}")[0] == 200
    for host in ("evil.example", f"evil.example:{served.port}", "127.0.0.1", f"127.0.0.1:{served.port + 1}", ""):
        status, headers, body = get(served, "/api/calls", host=host)
        assert status == 403 and SAID.encode() not in body, host
    assert len(served.api.seen) == 1


def test_a_request_another_page_made_is_refused_whatever_port_that_page_came_from(served):
    for site in ("cross-site", "same-site"):                            # another site; another port of this computer
        status, headers, body = get(served, "/api/calls", headers={"Sec-Fetch-Site": site})
        assert status == 403 and SAID.encode() not in body, site
        assert get(served, f"/?key={SECRET}", cookie=None, headers={"Sec-Fetch-Site": site})[0] == 403
    assert served.api.seen == []
    for site in ("same-origin", "none"):                                # the page itself; an address typed or opened
        assert get(served, "/api/calls", headers={"Sec-Fetch-Site": site})[0] == 200


# --- requests that change something


def test_a_request_that_changes_anything_must_be_the_panels_own(served):
    plain = request(served.port, "POST", "/api/calls/1/summary", cookie=served.secret)
    foreign = change(served, "POST", "/api/calls/1/summary", Origin="http://evil.example")
    assert plain[0] == foreign[0] == 403 and served.api.seen == []
    for origin in (f"http://127.0.0.1:{served.port}", f"http://localhost:{served.port}"):
        assert change(served, "POST", "/api/calls/1/summary", Origin=origin)[0] == 200
    assert change(served, "DELETE", "/api/calls/1")[0] == 200            # no Origin: not a browser, and it has the key
    assert change(served, "POST", "/api/summaries", body=b'{"ids": [1]}')[0] == 200
    assert [seen[0] for seen in served.api.seen] == ["POST", "POST", "DELETE", "POST"]
    assert served.api.seen[-1][3] == b'{"ids": [1]}'
    assert change(served, "PUT", "/api/calls/1/summary/mine", body=b'{"text": "x"}')[0] == 501      # nothing is put here
    assert len(served.api.seen) == 4


def test_no_request_brings_more_than_any_could_need(served):
    assert change(served, "POST", "/api/summaries", body=b"x" * (server.MOST + 1))[0] == 413
    assert served.api.seen == []


def raw(panel, text: str, wait: float = 5.0) -> bytes:
    """What the server says to these very bytes - for requests no decent client would send."""
    with socket.create_connection(("127.0.0.1", panel.port), timeout=wait) as sender:
        sender.sendall(text.encode("latin-1"))
        got = b""
        try:
            while chunk := sender.recv(65536):
                got += chunk
        except TimeoutError:
            got += b" (and then nothing)"
    return got


def test_a_request_that_is_not_well_formed_is_refused_at_once_and_leaves_nothing_behind(served, caplog, capfd):
    head = f"Host: 127.0.0.1:{served.port}\r\nCookie: protocallist_{served.port}={served.secret}\r\nX-Protocallist: 1\r\n"
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        for length in ("-1", "-999999", "ten", "1e3"):
            said = raw(served, f"POST /api/calls/1/summary HTTP/1.1\r\n{head}Content-Length: {length}\r\n\r\n", wait=3.0)
            assert said.startswith(b"HTTP/1.0 400") and b"and then nothing" not in said, length
        said = raw(served, f"POST /api/calls/1/summary HTTP/1.1\r\n{head}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", wait=3.0)
        assert said.startswith(b"HTTP/1.0 400")                         # a body without a length is not an empty one
        said = raw(served, "GET http://[ HTTP/1.1\r\nHost: x\r\n\r\n", wait=3.0)       # an address that cannot be read
        assert said.startswith(b"HTTP/1.0 400")
    assert served.api.seen == [] and caplog.text == "" and capfd.readouterr() == ("", "")
    assert get(served, "/api/calls")[0] == 200                           # and it goes on answering


def test_a_connection_that_says_nothing_is_let_go_of(served, monkeypatch):
    assert server._Handler.timeout == 30                                # seconds: no thread is held for ever
    monkeypatch.setattr(server._Handler, "timeout", 0.3)
    silent = socket.create_connection(("127.0.0.1", served.port), timeout=5)
    try:
        assert silent.recv(64) == b""                                    # closed by the server, with not a word
    finally:
        silent.close()


# --- pages


def test_the_files_of_the_page_are_served_and_nothing_else_of_the_disk(served):
    for path, kind in (("/", "text/html; charset=utf-8"), ("/app.js", "text/javascript; charset=utf-8"),
                       ("/app.css", "text/css; charset=utf-8"), ("/texts.json", "application/json; charset=utf-8")):
        status, headers, body = get(served, path)
        assert status == 200 and headers["Content-Type"] == kind and body
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"
        assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    for path in ("/favicon.ico", "/index.html", "/../secret.txt", "/static/app.js", "/%2e%2e/secret.txt", "/app.js/"):
        assert get(served, path)[0] == 404, path
    assert served.api.seen == []


# --- what the page asks


def test_a_question_of_the_page_goes_to_what_answers_it_with_its_words_and_comes_back_as_json(served):
    status, headers, body = get(served, "/api/calls?q=%D0%BF%D0%BB%D0%B0%D0%BD&x=1")
    assert status == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"said": SAID} and SAID.encode("utf-8") in body      # as it is, not escaped
    assert served.api.seen == [("GET", "/api/calls", {"q": "план", "x": "1"}, b"")]
    served.api.answer = Answer(409, {"error": "этот разговор сейчас записывается"})
    status, headers, body = change(served, "DELETE", "/api/calls/7")
    assert status == 409 and json.loads(body.decode("utf-8")) == {"error": "этот разговор сейчас записывается"}


def test_an_answer_that_fails_is_said_in_the_log_without_what_was_asked(served, caplog):
    served.api.error = RuntimeError("what nobody foresaw")
    with caplog.at_level(logging.ERROR, logger="protocallist"):
        status, headers, body = get(served, "/api/calls?q=%D0%BF%D0%BB%D0%B0%D0%BD")
    assert status == 500 and json.loads(body.decode("utf-8")) == {"error": "внутренняя ошибка — см. журнал"}
    assert "the panel could not answer GET /api/calls" in caplog.text and "план" not in caplog.text
    served.api.error = None
    assert get(served, "/api/calls")[0] == 200                           # and it goes on answering


def test_nothing_a_browser_asks_for_goes_to_the_log(served, caplog, capfd):
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        get(served, "/api/calls?q=%D0%BF%D0%BB%D0%B0%D0%BD")
        get(served, "/nothing")
        get(served, "/", cookie=None)
    assert caplog.text == "" and capfd.readouterr() == ("", "")


# --- a file, and the part of it that is asked for


def test_a_sound_goes_out_whole_or_in_the_part_the_browser_asks_for(served, tmp_path):
    sound = tmp_path / "2026-10-06_15-55-33_Discord.opus"
    sound.write_bytes(bytes(range(256)) * 1000)                         # 256 000 bytes, each telling its place
    served.api.answer = Answer(file=sound)
    status, headers, body = get(served, "/api/calls/1/audio")
    assert status == 200 and headers["Content-Type"] == "audio/ogg" and headers["Accept-Ranges"] == "bytes"
    assert len(body) == 256000 and headers["Content-Length"] == "256000"
    for asked, first, last in (("bytes=1000-1999", 1000, 1999), ("bytes=255000-", 255000, 255999),
                               ("bytes=-500", 255500, 255999), ("bytes=255990-999999", 255990, 255999), ("bytes=0-0", 0, 0)):
        status, headers, body = get(served, "/api/calls/1/audio", headers={"Range": asked})
        assert status == 206 and headers["Content-Range"] == f"bytes {first}-{last}/256000", asked
        assert body == (bytes(range(256)) * 1000)[first:last + 1]
    status, headers, body = get(served, "/api/calls/1/audio", headers={"Range": "bytes=300000-"})
    assert status == 416 and headers["Content-Range"] == "bytes */256000" and body == b""
    for odd in ("bytes=5-1", "bytes=a-b", "bytes=0-9,20-29", "seconds=1-2"):
        status, headers, body = get(served, "/api/calls/1/audio", headers={"Range": odd})
        assert (status, len(body)) in ((200, 256000), (416, 0)), odd      # the whole of it, or a plain no
    served.api.answer = Answer(file=tmp_path / "gone.opus")
    assert get(served, "/api/calls/1/audio")[0] == 404


def test_a_sound_on_its_way_to_a_browser_can_still_be_deleted(served, tmp_path):
    sound = tmp_path / "long.opus"
    sound.write_bytes(b"\0" * (16 << 20))                               # more than fits into what waits to be read
    served.api.answer = Answer(file=sound)
    listener = socket.create_connection(("127.0.0.1", served.port))
    try:
        listener.sendall(f"GET /api/calls/1/audio HTTP/1.1\r\nHost: 127.0.0.1:{served.port}\r\n"
                         f"Cookie: protocallist_{served.port}={served.secret}\r\n\r\n".encode())
        assert listener.recv(64).startswith(b"HTTP/1.0 200")            # it began - and the listener reads no further
        time.sleep(0.3)                                                 # the server has filled what it could and waits
        sound.unlink()                                                  # no file is held open between two blocks
    finally:
        listener.close()
    assert not sound.exists()


def test_a_text_goes_out_as_a_file_to_save_under_the_name_of_the_conversation(served):
    served.api.answer = Answer(text="# Разговор\n", name="2026-10-06_15-55-33_Мой_Созвон - text.md")
    status, headers, body = get(served, "/api/calls/1/text.md")
    assert status == 200 and body.decode("utf-8") == "# Разговор\n" and headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert headers["Content-Disposition"] == ("attachment; filename=\"conversation.md\"; filename*=UTF-8''"
                                              "2026-10-06_15-55-33_%D0%9C%D0%BE%D0%B9_%D0%A1%D0%BE%D0%B7%D0%B2%D0%BE%D0%BD%20-%20text.md")


# --- the port


def test_a_port_that_is_taken_is_said_and_one_that_was_let_go_of_can_be_had_again(pages):
    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)        # another program, willing to share its port
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    panel = Panel(Asked(), port, SECRET, static=pages)
    with pytest.raises(OSError):
        panel.start()
    panel.stop()                                                        # what never began is stopped without a word
    taken.close()
    panel.start()
    assert panel.port == port and get(panel, "/")[0] == 200
    panel.stop()
    panel.start()
    assert get(panel, "/")[0] == 200
    thief = socket.socket()
    thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)         # another program that would listen beside it
    try:
        with pytest.raises(OSError):
            thief.bind(("127.0.0.1", port))
    finally:
        thief.close()
    panel.stop()
