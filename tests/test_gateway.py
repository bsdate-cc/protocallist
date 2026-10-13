"""The gateway client against a little gateway of its own: a real server on a port of this machine."""
import json
import re
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from protocallist import gateway
from protocallist.config import GatewayConfig
from protocallist.gateway import Gateway, Refused, Segment, Unavailable

KEY = "the-key-of-the-test"


class Pretend:
    """What the pretended gateway is and does; a test changes it before asking."""
    health = {"status": "ok", "model": "gigaam-v3", "error": None, "version": "0.9.0", "pid": 1}
    answer: tuple[int, object] = (200, {"text": "раз два", "duration": 2.0, "segments": [
        {"id": 0, "start": 0.25, "end": 0.9, "text": "раз"}, {"id": 1, "start": 1.1, "end": 1.8, "text": "два"}]})
    delay = 0.0
    asked: list[dict] = []
    looked: list[dict] = []     # the plain looks at it - anything asked that is not a piece or its models - and the key of each
    models: tuple[int, object] = (404, {"error": {"code": "not_found", "message": "404: Not Found"}})     # an older gateway
    key: str | None = KEY       # the key it wants to be asked with; None - it wants none


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            Pretend.asked.append({"path": self.path, "key": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != (Pretend.key and f"Bearer {Pretend.key}"):
                return self._send(401, {"error": {"code": "unauthorized", "message": "missing or wrong key"}})
            return self._send(*Pretend.models)
        Pretend.looked.append({"path": self.path, "key": self.headers.get("Authorization")})
        self._send(200, Pretend.health)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Pretend.asked.append({"path": self.path, "key": self.headers.get("Authorization"),
                              "type": self.headers.get("Content-Type"), "body": body})
        if self.headers.get("Authorization") != (Pretend.key and f"Bearer {Pretend.key}"):
            return self._send(401, {"error": {"code": "unauthorized", "message": "missing or wrong key"}})
        time.sleep(Pretend.delay)
        self._send(*Pretend.answer)


@pytest.fixture
def pretend():
    saved = (Pretend.health, Pretend.answer, Pretend.delay, Pretend.models, Pretend.key)
    Pretend.health = dict(Pretend.health)                  # a test changes its own copy
    Pretend.asked, Pretend.looked = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    Pretend.health, Pretend.answer, Pretend.delay, Pretend.models, Pretend.key = saved


WAV = b"RIFF....WAVE-the-sound-of-a-piece"


def test_a_piece_goes_as_a_file_and_its_phrases_come_back_with_their_places(pretend):
    phrases = Gateway(pretend, KEY).transcribe(WAV)
    assert phrases == [Segment(0.25, 0.9, "раз"), Segment(1.1, 1.8, "два")]
    asked, = Pretend.asked
    assert asked["path"] == "/v1/audio/transcriptions" and asked["key"] == f"Bearer {KEY}"
    assert asked["type"].startswith("multipart/form-data; boundary=")
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in asked["body"]
    assert b'name="file"; filename="piece.wav"' in asked["body"] and WAV in asked["body"]


def test_a_gateway_that_is_there_and_ready_says_which_model_it_runs(pretend):
    gate = Gateway(pretend, KEY)
    gate.check()
    assert gate.model == "gigaam-v3"


def test_nobody_listening_means_wait_not_fail():
    gate = Gateway("http://127.0.0.1:9", KEY)                      # nothing lives on this port
    with pytest.raises(Unavailable) as down:
        gate.check()
    assert down.value.kind == gateway.DOWN
    with pytest.raises(Unavailable) as gone:
        gate.transcribe(WAV)
    assert gone.value.kind == gateway.DOWN


def test_a_gateway_that_still_loads_its_model_means_wait(pretend):
    Pretend.health["status"] = "loading"
    with pytest.raises(Unavailable) as loading:
        Gateway(pretend, KEY).check()
    assert loading.value.kind == gateway.LOADING
    Pretend.answer = (503, {"error": {"code": "not_ready", "message": "the model is still loading"}})
    with pytest.raises(Unavailable) as busy:
        Gateway(pretend, KEY).transcribe(WAV)
    assert busy.value.kind == gateway.LOADING and "still loading" in str(busy.value)


def test_a_gateway_older_than_the_phrases_with_places_is_said_to_be_old(pretend):
    for version in ("0.7.9", "0.7", "what"):
        Pretend.health["version"] = version
        with pytest.raises(Unavailable) as old:
            Gateway(pretend, KEY).check()
        assert old.value.kind == gateway.OLD, version
    for version in ("0.8.0", "0.10.1", "1.0.0"):
        Pretend.health["version"] = version
        Gateway(pretend, KEY).check()


def test_something_else_on_that_port_is_not_a_gateway(pretend):
    Pretend.health = "<html>router</html>"
    with pytest.raises(Unavailable) as other:
        Gateway(pretend, KEY).check()
    assert other.value.kind in (gateway.DOWN, gateway.LOADING)


def test_an_error_for_a_piece_is_a_refusal_with_what_the_gateway_said(pretend):
    Pretend.answer = (400, {"error": {"code": "invalid_request", "message": "only 16000 Hz WAV is supported, got 8000 Hz"}})
    with pytest.raises(Refused, match="400.*only 16000 Hz"):
        Gateway(pretend, KEY).transcribe(WAV)
    with pytest.raises(Refused, match="401.*wrong key"):
        Gateway(pretend, "another key").transcribe(WAV)
    Pretend.answer = (500, b"<h1>Internal Server Error</h1>")
    with pytest.raises(Refused, match="500"):
        Gateway(pretend, KEY).transcribe(WAV)


def test_an_answer_that_does_not_come_in_time_is_a_refusal(pretend):
    Pretend.delay = 1.5
    with pytest.raises(Refused, match="no answer in 0 s") as late:
        Gateway(pretend, KEY, timeout=0.3).transcribe(WAV)
    assert isinstance(late.value, gateway.TimedOut)                      # a refusal of its own kind: the page has words for it
    assert gateway.TIMEOUT_S == 600.0


def test_an_answer_that_is_not_what_was_asked_for_is_a_refusal(pretend):
    for answer in (b"not json at all", {"text": "раз два"}, {"segments": [{"start": 0.0}]}, {"segments": "none"}):
        Pretend.answer = (200, answer)
        with pytest.raises(Refused):
            Gateway(pretend, KEY).transcribe(WAV)
    Pretend.answer = (200, {"text": "", "duration": 600.0, "segments": []})    # ten minutes of silence: no phrases
    assert Gateway(pretend, KEY).transcribe(WAV) == []


# --- where the gateway is


def test_the_gateway_of_this_computer_is_found_by_the_two_files_it_keeps(tmp_path):
    (tmp_path / "gateway.json").write_text(json.dumps({"pid": 1, "port": 8765, "version": "0.9.0"}), encoding="utf-8")
    (tmp_path / "key").write_text("  secret\n", encoding="utf-8")
    assert gateway.locate(GatewayConfig(), home=tmp_path) == ("http://127.0.0.1:8765", "secret")


def test_the_settings_can_name_another_gateway_and_another_key_file(tmp_path):
    other = tmp_path / "other.key"
    other.write_text("theirs", encoding="utf-8")
    cfg = GatewayConfig(url="http://10.0.0.5:9000/", key_file=str(other))
    assert gateway.locate(cfg, home=tmp_path / "nowhere") == ("http://10.0.0.5:9000", "theirs")


def test_without_the_files_there_is_nobody_to_ask(tmp_path):
    with pytest.raises(Unavailable) as none:
        gateway.locate(GatewayConfig(), home=tmp_path)
    assert none.value.kind == gateway.DOWN
    (tmp_path / "key").write_text("secret", encoding="utf-8")
    (tmp_path / "gateway.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(Unavailable):
        gateway.locate(GatewayConfig(), home=tmp_path)


def test_connecting_finds_the_gateway_and_checks_it(pretend, tmp_path, monkeypatch):
    tolmach = tmp_path / ".tolmach"
    tolmach.mkdir()
    (tolmach / "key").write_text(KEY, encoding="utf-8")
    (tolmach / "gateway.json").write_text(json.dumps({"port": int(pretend.rsplit(":", 1)[1])}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    gate = gateway.connect(GatewayConfig())
    assert gate.model == "gigaam-v3" and gate.transcribe(WAV)[0].text == "раз"
    assert b'name="model"' not in Pretend.asked[-1]["body"]             # Tolmach is not told which model is its main one
    Pretend.health["version"] = "0.7.0"
    with pytest.raises(Unavailable):
        gateway.connect(GatewayConfig())


# --- this computer and nothing but it


class Elsewhere(BaseHTTPRequestHandler):
    """Stands in for a proxy on another machine: remembers what reached it and answers that it cannot help."""
    reached: list[tuple[str, str, int]] = []

    def log_message(self, *args) -> None:
        pass

    def _note(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        Elsewhere.reached.append((self.command, self.path, length))
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _note


def test_the_gateway_is_asked_directly_whatever_proxy_this_computer_is_told_to_use(pretend, monkeypatch):
    Elsewhere.reached = []
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)       # as in a program started with the proxy set
    try:
        gate = Gateway(pretend, KEY)
        gate.check()
        assert [s.text for s in gate.transcribe(WAV)] == ["раз", "два"]
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert Elsewhere.reached == []                              # the sound of a conversation went nowhere else
    assert len(Pretend.asked) == 1


@contextmanager
def answering(payload: bytes):
    """A port of this machine where a whole request is taken, `payload` is sent back, and the line is cut."""
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        while True:
            try:
                line, _ = listener.accept()
            except OSError:
                return
            with line:
                line.settimeout(5.0)
                try:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += line.recv(65536)
                    head, _, body = data.partition(b"\r\n\r\n")
                    length = re.search(rb"content-length: *(\d+)", head.lower())
                    while length is not None and len(body) < int(length.group(1)):
                        body += line.recv(65536)
                    line.sendall(payload)
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


def test_a_gateway_that_goes_away_in_the_middle_of_its_answer_means_wait_not_fail():
    half = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{"text": "'
    with answering(half) as url:
        with pytest.raises(Unavailable) as gone:
            Gateway(url, KEY).transcribe(WAV)
    assert gone.value.kind == gateway.DOWN


def test_a_port_where_something_that_is_not_a_web_server_answers_is_not_a_gateway():
    with answering(b"SSH-2.0-OpenSSH_9.5\r\n") as url:
        with pytest.raises(Unavailable) as other:
            Gateway(url, KEY).check()
        with pytest.raises(Unavailable):
            Gateway(url, KEY).transcribe(WAV)
    assert other.value.kind == gateway.DOWN


def test_a_version_with_something_around_its_numbers_is_read_by_its_numbers(pretend):
    for version in ("0.10.0-beta", "v0.9.0", "0.8.0rc1", "0.9.1.dev3"):
        Pretend.health["version"] = version
        Gateway(pretend, KEY).check()
    for version in ("0.7.9-final", "beta", ""):
        Pretend.health["version"] = version
        with pytest.raises(Unavailable) as old:
            Gateway(pretend, KEY).check()
        assert old.value.kind == gateway.OLD, version


# --- more than one model

MODELS = {"object": "list", "data": [
    {"id": "gigaam-v3", "object": "model", "language": "ru", "main": True, "installed": True, "loaded": True},
    {"id": "parakeet-unified-en", "object": "model", "language": "en", "main": False, "installed": True, "loaded": False},
    {"id": "another", "object": "model", "language": "de", "main": False, "installed": False, "loaded": False}]}


def test_the_gateway_tells_which_models_it_has(pretend):
    Pretend.models = (200, MODELS)
    assert Gateway(pretend, KEY).models() == [
        gateway.Model("gigaam-v3", "ru", True, True), gateway.Model("parakeet-unified-en", "en", False, True),
        gateway.Model("another", "de", False, False)]
    assert Pretend.asked[-1] == {"path": "/v1/models", "key": f"Bearer {KEY}"}


def test_a_gateway_that_cannot_tell_its_models_is_one_that_has_no_choice_of_them(pretend):
    assert Gateway(pretend, KEY).models() is None                        # an older one: it knows no such question


def test_a_gateway_that_stumbles_over_the_question_of_its_models_is_one_to_wait_for(pretend):
    """Not "it has no choice of models": that would be said of a gateway that merely did not answer in time."""
    for answer in ((200, {"data": "models"}), (200, {"data": [{"name": "x"}]}), (200, b"not json"), (500, {"error": {}})):
        Pretend.models = answer
        with pytest.raises(Unavailable) as waits:
            Gateway(pretend, KEY).models()
        assert waits.value.kind == gateway.DOWN, answer
    with pytest.raises(Unavailable):
        Gateway(pretend, "another key").models()
    with pytest.raises(Unavailable):
        Gateway("http://127.0.0.1:9", KEY).models()                      # nobody listens there


def test_a_piece_may_name_the_model_that_reads_it(pretend):
    Gateway(pretend, KEY).transcribe(WAV, "parakeet-unified-en")
    assert b'name="model"\r\n\r\nparakeet-unified-en\r\n' in Pretend.asked[-1]["body"]
    Gateway(pretend, KEY).transcribe(WAV)
    assert b'name="model"' not in Pretend.asked[-1]["body"]            # the main model: as it always was asked


def test_a_model_that_is_not_in_place_is_a_refusal_of_its_own_kind(pretend):
    Pretend.answer = (409, {"error": {"code": "model_not_installed", "message": "the files of the model 'x' are not on this computer"}})
    with pytest.raises(gateway.NoModel, match="not on this computer"):
        Gateway(pretend, KEY).transcribe(WAV, "x")
    assert issubclass(gateway.NoModel, Refused)
    Pretend.answer = (500, {"error": {"code": "model_failed", "message": "the model 'x' could not be loaded: bad file"}})
    with pytest.raises(Refused, match="could not be loaded: bad file") as failed:
        Gateway(pretend, KEY).transcribe(WAV, "x")
    assert isinstance(failed.value, gateway.ModelFailed) and not isinstance(failed.value, gateway.NoModel)


# --- a server of one's own


def test_a_server_of_ones_own_is_where_the_settings_say_with_its_key_or_with_none(tmp_path):
    nowhere = tmp_path / "no-tolmach-here"
    assert gateway.locate(GatewayConfig(url="http://10.0.0.5:9000/"), home=nowhere) == ("http://10.0.0.5:9000", "")
    with pytest.raises(Unavailable) as lost:                             # a key file that is named must be there
        gateway.locate(GatewayConfig(url="http://10.0.0.5:9000", key_file=str(tmp_path / "gone.key")), home=nowhere)
    assert lost.value.kind == gateway.DOWN


def test_a_server_of_ones_own_is_not_asked_which_tolmach_it_is(pretend):
    Pretend.health = {"detail": "Not Found"}                             # it knows nothing of the question Tolmach answers
    Pretend.key = None                                                   # ...and wants no key
    gate = gateway.connect(GatewayConfig(url=pretend, model="large"))
    assert gate.model == "large"                                         # the model the text is by: the one it is asked for
    assert gate.transcribe(WAV) == [Segment(0.25, 0.9, "раз"), Segment(1.1, 1.8, "два")]
    asked = Pretend.asked[-1]
    assert asked["key"] is None and b'name="model"\r\n\r\nlarge\r\n' in asked["body"]
    gate.transcribe(WAV, "another")                                      # a piece may still name the model that reads it
    assert b'name="model"\r\n\r\nanother\r\n' in Pretend.asked[-1]["body"]
    plain = gateway.connect(GatewayConfig(url=pretend))                  # no model in the settings: none is named
    plain.transcribe(WAV)
    assert plain.model == "" and b'name="model"' not in Pretend.asked[-1]["body"]


def test_a_server_of_ones_own_that_is_not_there_is_waited_for():
    with pytest.raises(Unavailable) as waits:
        gateway.connect(GatewayConfig(url="http://127.0.0.1:9"))         # nobody listens there
    assert waits.value.kind == gateway.DOWN


def test_a_server_of_ones_own_that_answers_with_an_error_to_a_plain_look_is_there(pretend):
    class Strict(Handler):
        status = 404                                                     # it has nothing at its bare address

        def do_GET(self) -> None:
            self._send(Strict.status, {"detail": "whatever it says of its bare address"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Strict)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        Pretend.key = None
        for Strict.status in (404, 401, 500):                            # an answer, whatever it is: somebody is there
            gate = gateway.connect(GatewayConfig(url=f"http://127.0.0.1:{server.server_address[1]}"))
            assert gate.transcribe(WAV)[0].text == "раз", Strict.status
    finally:
        server.shutdown()
        server.server_close()


def test_a_server_of_ones_own_with_a_key_is_asked_with_it(pretend, tmp_path):
    key = tmp_path / "their.key"
    key.write_text(KEY + "\n", encoding="utf-8")
    gate = gateway.connect(GatewayConfig(url=pretend, key_file=str(key)))
    assert gate.transcribe(WAV)[0].text == "раз" and Pretend.asked[-1]["key"] == f"Bearer {KEY}"
    assert Pretend.looked[-1] == {"path": "/", "key": f"Bearer {KEY}"}  # the look at its bare address goes with the key, too


def test_the_models_of_a_server_of_ones_own_are_those_it_lists(pretend):
    Pretend.key = None
    Pretend.models = (200, {"object": "list", "data": [
        {"id": "small", "object": "model", "language": "en"}, {"id": "large", "object": "model"},
        {"id": "far", "object": "model", "installed": False, "main": True}]})
    gate = gateway.connect(GatewayConfig(url=pretend, model="large"))
    assert gate.models() == [gateway.Model("small", "en", False, True), gateway.Model("large", "", True, True),
                             gateway.Model("far", "", True, False)]      # what it says of a model itself is believed
    assert Pretend.asked[-1] == {"path": "/v1/models", "key": None}
    for answer in ((404, {}), (401, {"detail": "Unauthorized"}), (403, {}), (405, {"detail": "Method Not Allowed"}),
                   (200, {"models": []}), (200, {"data": "models"})):
        Pretend.models = answer
        assert gate.models() is None, answer                             # it does not know the question: no choice of models
    Pretend.models = (500, {"error": {}})
    with pytest.raises(Unavailable):                                     # it stumbled: asked again, not taken for one with no choice
        gate.models()


def test_a_server_of_ones_own_is_asked_directly_too(pretend, monkeypatch):
    Elsewhere.reached = []
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)       # as in a program started with the proxy set
    Pretend.models = (200, MODELS)
    try:
        gate = Gateway(pretend, KEY, own=True)
        gate.check()
        assert [s.text for s in gate.transcribe(WAV)] == ["раз", "два"] and len(gate.models()) == 3
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert Elsewhere.reached == []                              # neither the look, nor the sound, nor the question of models


def test_a_server_that_sends_the_asker_on_is_not_followed():
    """The sound and the key go where the settings say and nowhere else."""
    Elsewhere.reached = []
    other = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=other.serve_forever, daemon=True).start()

    class Moved(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _on(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _on

    moved = ThreadingHTTPServer(("127.0.0.1", 0), Moved)
    threading.Thread(target=moved.serve_forever, daemon=True).start()
    try:
        gate = Gateway(f"http://127.0.0.1:{moved.server_address[1]}", KEY, own=True)
        gate.check()                                                     # it answers: it is there
        with pytest.raises(Refused, match="302"):
            gate.transcribe(WAV)
        assert gate.models() is None                                     # ...and has no choice of models to offer
        with pytest.raises(Refused, match="302"):
            Gateway(f"http://127.0.0.1:{moved.server_address[1]}", KEY).transcribe(WAV)
    finally:
        for server in (moved, other):
            server.shutdown()
            server.server_close()
    assert Elsewhere.reached == []


def test_a_key_file_saved_with_a_mark_before_its_first_letter_is_read_without_it(tmp_path):
    marked = tmp_path / "their.key"
    marked.write_text("theirs\n", encoding="utf-8-sig")                 # as Notepad may save a file
    assert gateway.locate(GatewayConfig(url="http://10.0.0.5:9000", key_file=str(marked)), home=tmp_path)[1] == "theirs"
    (tmp_path / "gateway.json").write_text(json.dumps({"port": 8765}), encoding="utf-8")
    assert gateway.locate(GatewayConfig(key_file=str(marked)), home=tmp_path) == ("http://127.0.0.1:8765", "theirs")


def test_phrases_come_back_trimmed_and_no_phrases_at_all_is_not_a_failure(pretend):
    """As a server of another kind may answer: a blank before every phrase, and nothing where nothing was said."""
    Pretend.answer = (200, {"text": " Hello there. Fine.", "segments": [
        {"start": 0, "end": 1.5, "text": " Hello there."}, {"start": 1.5, "end": 2, "text": " Fine. "}]})
    assert Gateway(pretend, KEY).transcribe(WAV) == [Segment(0.0, 1.5, "Hello there."), Segment(1.5, 2.0, "Fine.")]
    Pretend.answer = (200, {"text": "", "segments": None})
    assert Gateway(pretend, KEY).transcribe(WAV) == []


def test_a_key_that_cannot_be_sent_is_trouble_with_the_gateway_not_with_the_program(pretend):
    with pytest.raises(Unavailable):                                     # no letter of it can stand where a key is sent
        Gateway(pretend, "ключ").models()
    with pytest.raises(Unavailable):
        Gateway(pretend, "ключ", own=True).check()
