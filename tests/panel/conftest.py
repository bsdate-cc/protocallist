"""What the tests of the panel share: a stand-in for the service behind the icon, a real archive in the data
folder of the test, and a way to ask a served panel over real HTTP."""
import http.client
import json
from datetime import datetime, timedelta

import pytest

from protocallist import gateway, paths
from protocallist.app import Glance
from protocallist.config import Config
from protocallist.store import Phrase, Store
from protocallist.summaries import Summariser

AT = datetime(2026, 10, 6, 15, 55, 33)
ENGLISH = "parakeet-unified-en"
ANSWER = "# Выжимка\n\n## Коротко\nГоворили о плане.\n"


class Service:
    """Stands in for the service behind the icon: shows the glance it is given, remembers what it was told."""

    def __init__(self) -> None:
        self.shown = Glance()
        self.cfg = Config()
        self.store: Store | None = None
        self.makes_summaries: Summariser | None = None
        self.told: list[str] = []

    def glance(self) -> Glance:
        return self.shown

    def archive(self):
        return self.store

    def summariser(self):
        return self.makes_summaries

    def settings(self) -> Config:
        return self.cfg

    def discard_current(self) -> None:
        self.told.append("discard")


class World:
    """An archive with a summariser whose "Claude" is a stand-in, and the Api over them."""

    def __init__(self) -> None:
        from protocallist.panel.api import Api       # here, not above: the tests of the server do without it

        self.service = Service()
        self.store = self.service.store = Store(paths.database_file(), paths.calls_dir())
        self.asked: list[dict] = []
        self.answer, self.error, self.during = ANSWER, None, None
        self.now = AT + timedelta(days=1)
        self.revealed: list = []
        # what the gateway says it has - None: it is not there, or is one with no choice of models
        self.known: list | None = [gateway.Model("gigaam-v3", "ru", True, True), gateway.Model(ENGLISH, "en", False, True)]
        self.asked_models: list = []        # ...and the settings it was looked for by, every time it was asked
        self.clock = 0.0
        self.slow = 0.0                     # how long the gateway takes to say what models it has
        self.service.makes_summaries = Summariser(self.store, self.service.cfg.summary, language=lambda: "ru",
                                                  ask=self._ask, today=lambda: self.now)
        self.api = Api(self.service, reveal=self.revealed.append, models=self._models, clock=lambda: self.clock)

    def _models(self, cfg):
        self.asked_models.append(cfg)
        self.clock += self.slow
        return self.known

    def _ask(self, text, model, language, **how):
        self.asked.append(dict(text=text, model=model, **how))
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        return self.answer, model

    def call(self, *said: tuple[str, str], minutes_later: int = 0, program: str = "Discord", seconds: float = 60.0):
        """A recognised conversation; `said` is (who, what) in turn, five seconds apart."""
        call = self.store.begin(AT + timedelta(minutes=minutes_later), program)
        self.store.finish(call.id, seconds)
        self.store.take()
        self.store.folder(call).rmdir()
        return self.store.done(call.id, [Phrase(who, i * 5.0, i * 5.0 + 4, what) for i, (who, what) in enumerate(said)],
                               "gigaam-v3", AT + timedelta(minutes=minutes_later, seconds=seconds))     # as it ended

    def ask(self, method: str, path: str, body: dict | None = None, **query: str):
        """Ask the Api as the server would: (status, data)."""
        answer = self.api.handle(method, path, query, json.dumps(body).encode("utf-8") if body is not None else b"")
        return int(answer.status), answer.data


@pytest.fixture
def world():
    w = World()
    yield w
    w.store.close()


def request(port: int, method: str, path: str, *, host: str | None = None, cookie: str | None = None,
            headers: dict | None = None, body: bytes = b""):
    """One request over real HTTP: (status, headers, body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = {"Host": f"127.0.0.1:{port}" if host is None else host, **(headers or {})}
    if cookie is not None:
        sent["Cookie"] = f"protocallist_{port}={cookie}"
    connection.putrequest(method, path, skip_host=True)
    for name, value in sent.items():
        connection.putheader(name, value)
    connection.putheader("Content-Length", str(len(body)))
    connection.endheaders(body)
    answer = connection.getresponse()
    got = answer.status, dict(answer.getheaders()), answer.read()
    connection.close()
    return got
