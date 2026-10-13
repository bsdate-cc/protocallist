"""What the page of the panel asks, and what it is answered.

Everything here is told to the page as data; the page puts it into words. The service behind
the icon is asked for what it shows already (`glance()`), for the archive and for what makes
summaries - and whatever the page asks to be done is done through those same three.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Callable

from protocallist import VERSION, gateway, i18n, transcriber, transcript
from protocallist.config import GatewayConfig
from protocallist.i18n import Text, _
from protocallist.store import DONE, MADE, MINE, RECORDING, Call, Store

log = logging.getLogger(__name__)

PAUSE_S = 1.0       # phrases of one side closer than this are drawn as one stretch of speech
MANY = 1000         # more conversations than this are not sent to be summarised by one request
OLD = ("", "keep", "drop")      # what becomes of a line of summaries that stands still: not said, started again, forgotten
TRACKS = ("mic.wav", "line.wav")
MODELS_S = 5.0      # the gateway is asked what models it has no more often than this, however many pages are open
# Why a conversation could not be recognised anew, where recognition names the reason by a word of its own.
ANEW = {transcriber.NO_MODEL: Text("модель не установлена в Толмаче"), transcriber.NO_SOUND: Text("звука этой записи нет"),
        transcriber.NO_CHOICE: Text("в этом Толмаче нет выбора моделей — нужен Толмач новее"),
        transcriber.NO_LOAD: Text("Толмач не смог загрузить модель — подробности в его журнале"),
        transcriber.NO_WORDS: Text("модель не услышала ни слова — прежний текст оставлен"),
        transcriber.NO_ANSWER: Text("Толмач не ответил за {minutes} мин", minutes=round(gateway.TIMEOUT_S / 60)),
        transcriber.BAD_SOUND: Text("звук этой записи не читается")}
# The same where the settings name a server of the user's own: it is not Tolmach, and is not called so.
OWN = {**ANEW, transcriber.NO_MODEL: Text("такой модели у сервиса распознавания нет"),
       transcriber.NO_CHOICE: Text("сервис распознавания не говорит, какие у него модели"),
       transcriber.NO_LOAD: Text("сервис распознавания не смог загрузить модель"),
       transcriber.NO_ANSWER: Text("сервис распознавания не ответил за {minutes} мин", minutes=round(gateway.TIMEOUT_S / 60))}


@dataclass
class Answer:
    status: int = HTTPStatus.OK
    data: dict | None = None        # sent as JSON
    file: Path | None = None        # a file sent as it is, or the part of it that is asked for
    kind: str = "audio/ogg"         # ...and what it is
    text: str | None = None         # a text for the browser to save as a file...
    name: str = ""                  # ...under this name


def _no(status: int, why: Text) -> Answer:
    return Answer(status, {"error": str(why)})


def _stands() -> Answer:
    """The line of summaries stands still, and the request did not say what becomes of it: the page asks the user."""
    return Answer(HTTPStatus.CONFLICT, {"error": str(Text("очередь выжимок остановлена")), "stopped": True})


def reveal(path: Path) -> None:
    """Show a file in Explorer - the answer to a click on the page."""
    subprocess.Popen(["explorer", f"/select,{path}"])


def models(cfg: GatewayConfig) -> list[gateway.Model] | None:
    """The models of the gateway on this computer - None if it is not there, or is one with no choice of them."""
    try:
        return gateway.connect(cfg).models()
    except gateway.Unavailable:
        return None


def _older(saved: str, text: str | None) -> bool:
    """Was this summary saved before the text there is now was written?"""
    try:
        return datetime.fromisoformat(saved) < datetime.fromisoformat(text or "")
    except (ValueError, TypeError):
        return False


class Api:
    """`app` is the service behind the icon: `glance()`, `archive()`, `summariser()`, `settings()`,
    `discard_current()`. `models(cfg)` asks the gateway what models it has."""

    def __init__(self, app, *, reveal: Callable[[Path], None] = reveal,
                 models: Callable[[GatewayConfig], list[gateway.Model] | None] = models,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._app, self._reveal, self._models, self._clock = app, reveal, models, clock
        # how many remarks a recognised conversation has: counted once for a text - (when it was written, how many)
        self._remarks: dict[int, tuple[str | None, int]] = {}
        self._asking = threading.Lock()
        self._known: tuple[float, list[gateway.Model] | None] | None = None     # (when asked, what the gateway said)
        self._routes = [
            ("GET", r"/api/status", self._status),
            ("GET", r"/api/calls", self._calls),
            ("GET", r"/api/calls/(\d{1,15})", self._call),
            ("GET", r"/api/calls/(\d{1,15})/audio", self._audio),
            ("GET", r"/api/calls/(\d{1,15})/text\.md", self._text),
            ("GET", r"/api/calls/(\d{1,15})/summary\.md", self._summary),
            ("GET", r"/api/models", self._choice),
            ("POST", r"/api/calls/(\d{1,15})/recognise", self._recognise),
            ("DELETE", r"/api/calls/(\d{1,15})/recognise", self._unask),
            ("POST", r"/api/calls/(\d{1,15})/summary", self._make),
            ("POST", r"/api/summaries", self._send),
            ("POST", r"/api/summaries/restart", self._restart),
            ("POST", r"/api/summaries/reset", self._reset),
            ("POST", r"/api/summaries/halt", self._halt),
            ("POST", r"/api/calls/(\d{1,15})/retry", self._retry),
            ("POST", r"/api/calls/(\d{1,15})/reveal", self._show),
            ("DELETE", r"/api/calls/(\d{1,15})", self._forget),
            ("POST", r"/api/recording/discard", self._discard),
        ]

    def handle(self, method: str, path: str, query: dict[str, str], body: bytes) -> Answer:
        for verb, pattern, answer in self._routes:
            found = re.fullmatch(pattern, path)
            if found and verb == method:
                try:
                    given = json.loads(body.decode("utf-8")) if body else {}
                except ValueError:
                    given = None
                if not isinstance(given, dict):     # never taken for a request with nothing in it: that one sends a text
                    return Answer(HTTPStatus.BAD_REQUEST, {"error": "not a JSON object"})
                return answer(*map(int, found.groups()), query=query, given=given)
        return Answer(HTTPStatus.NOT_FOUND, {"error": "there is no such thing to ask"})

    # ---- what there is

    def _status(self, query: dict, given: dict) -> Answer:
        """What the program is doing now, and whether the list of conversations is still what the page has."""
        glance, store, summariser = self._app.glance(), self._app.archive(), self._app.summariser()
        kind, name = summariser.target() if summariser is not None else ("", "")
        line = summariser.line() if summariser is not None else None
        stopped = line.stopped if line is not None else None
        return Answer(data=dict(
            version=VERSION, language=i18n.language(), archive=store is not None,
            state=glance.state, program=glance.program, minutes=glance.minutes, microphone=glance.microphone,
            recording=self._recording(store),
            problem=_(glance.problem) if glance.problem else "",
            recognising=glance.recognising, waiting=glance.waiting, failed=glance.failed, gateway=glance.gateway,
            own=self._anew() is OWN,        # who is asked for the text: a server of the user's own, or Tolmach
            making=list(line.making) if line is not None else [], queued=list(line.queued) if line is not None else [],
            stopped=dict(reason=str(stopped.reason), failed=stopped.failed, waiting=list(stopped.waiting)) if stopped else None,
            failures={str(call_id): str(why) for call_id, why in line.failures.items()} if line is not None else {},
            summary=dict(kind=kind, name=name), keep_audio_days=self._app.settings().storage.keep_audio_days,
            stamp=self._stamp(store)))

    def _recording(self, store: Store | None) -> int:
        """The number of the conversation that is being recorded now, or 0."""
        now = store.calls(RECORDING) if store is not None else []
        return now[-1].id if now else 0

    def _stamp(self, store: Store | None) -> str:
        """A few letters that change when anything a list of conversations shows has changed."""
        if store is None:
            return ""
        kinds = store.kinds()
        # asked to be recognised anew, and how that ended - also when both happened between two looks of the page
        seen = [(call.id, call.state, call.topic, call.audio_deleted_at, kinds.get(call.id, ""), store.sound(call).is_file(),
                 call.redo_model, call.redo_error, call.text_at)
                for call in store.calls()]
        return format(zlib.crc32(repr(seen).encode("utf-8")), "08x")

    def _calls(self, query: dict, given: dict) -> Answer:
        store = self._app.archive()
        if store is None:
            return Answer(data=dict(calls=[], ids=[]))
        if "q" in query:
            return Answer(data=dict(ids=sorted(store.search(query["q"]))))
        kinds = store.kinds()
        return Answer(data=dict(calls=[self._row(store, call, kinds.get(call.id, "")) for call in store.calls()]))

    def _row(self, store: Store, call: Call, kind: str) -> dict:
        if call.state == DONE and self._remarks.get(call.id, ("", 0))[0] != call.text_at:
            self._remarks[call.id] = (call.text_at, store.remarks(call.id))
        return dict(id=call.id, started=call.started_at, ended=call.ended_at, program=call.program,
                    seconds=round(call.duration_s or 0.0), state=call.state, remarks=self._remarks.get(call.id, ("", 0))[1],
                    topic=call.topic or "", about=("summary" if kind else "start") if call.topic else "",
                    summary=kind, sound=self._sound(store, call), redo=call.redo_model or "")

    def _sound(self, store: Store, call: Call) -> dict:
        """Where the sound of a conversation is: kept (how large, until when), deleted (when), still two
        recordings in the working folder, or not there at all."""
        if call.audio_deleted_at:
            return dict(state="deleted", at=call.audio_deleted_at)
        try:
            size = store.sound(call).stat().st_size
        except OSError:
            raw = any((store.folder(call) / name).is_file() for name in TRACKS)
            return dict(state="raw" if raw else "none")
        days = self._app.settings().storage.keep_audio_days
        try:
            until = None if days < 0 or not call.ended_at else \
                (datetime.fromisoformat(call.ended_at) + timedelta(days=days)).isoformat(timespec="seconds")
        except OverflowError:       # more days than a date can hold: for ever
            until = None
        return dict(state="kept", bytes=size, until=until)

    def _one(self, call_id: int) -> tuple[Store, Call] | Answer:
        store = self._app.archive()
        call = store.get(call_id) if store is not None else None
        return (store, call) if call is not None else _no(HTTPStatus.NOT_FOUND, Text("такой записи нет"))

    def _call(self, call_id: int, query: dict, given: dict) -> Answer:
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        phrases, both = store.phrases(call_id), store.summaries(call_id)
        speech: dict[str, list[list[float]]] = {"me": [], "them": []}
        for phrase in phrases:
            side = speech[phrase.speaker]
            if side and phrase.start_s - side[-1][1] <= PAUSE_S:
                side[-1][1] = max(side[-1][1], phrase.end_s)
            else:
                side.append([phrase.start_s, phrase.end_s])
        row = self._row(store, call, MINE if MINE in both else MADE if MADE in both else "")
        return Answer(data=dict(
            row, error=call.error or "", attempts=call.attempts, model=call.model or "", name=call.folder,
            text_at=call.text_at or "", redo_error=str(self._anew().get(call.redo_error, call.redo_error or "")),
            remarks_list=[dict(who=who, at=round(at, 1), text=text) for who, at, text in transcript.remarks(phrases)],
            speech=speech,
            summaries={kind: dict(text=s.text, saved=s.saved_at, maker=s.maker or "", seconds=s.seconds,
                                  stale=_older(s.saved_at, call.text_at))
                       for kind, s in both.items()}))

    def _audio(self, call_id: int, query: dict, given: dict) -> Answer:
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        if not store.sound(call).is_file():
            return _no(HTTPStatus.NOT_FOUND, Text("звука этой записи нет"))
        return Answer(file=store.sound(call))

    def _text(self, call_id: int, query: dict, given: dict) -> Answer:
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        return Answer(text=transcript.render(call, store.phrases(call_id)), name=f"{call.folder} - {_('текст')}.md")

    def _summary(self, call_id: int, query: dict, given: dict) -> Answer:
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        wanted = store.summaries(call_id).get(query["kind"]) if query.get("kind") else store.summary(call_id)
        if wanted is None:
            return _no(HTTPStatus.NOT_FOUND, Text("такой выжимки нет"))
        return Answer(text=wanted.text, name=f"{call.folder} - {_('выжимка')}.md")

    def _choice(self, query: dict, given: dict) -> Answer:
        """The models a conversation can be recognised anew with - none, if the gateway does not say."""
        return Answer(data=dict(models=[dict(name=m.name, language=m.language, main=m.main, installed=m.installed)
                                        for m in self._gateway_models() or []]))

    def _anew(self) -> dict:
        """Why a conversation could not be recognised anew, in words that name whoever recognises:
        Tolmach, or the server of the user's own that the settings name."""
        return OWN if self._app.settings().gateway.url else ANEW

    def _gateway_models(self) -> list[gateway.Model] | None:
        with self._asking:
            if self._known is None or self._clock() - self._known[0] >= MODELS_S:
                told = self._models(self._app.settings().gateway)
                self._known = (self._clock(), told)     # counted from the answer: a gateway slow to answer is not asked again at once
            return self._known[1]

    # ---- what is asked to be done

    def _recognise(self, call_id: int, query: dict, given: dict) -> Answer:
        """Put a conversation into the line of those to be recognised anew, with the model the page names.
        Its sound goes where the first text came from: to the gateway, and nowhere else."""
        model = given.get("model")
        if not isinstance(model, str) or not model:
            return Answer(HTTPStatus.BAD_REQUEST, {"error": "with which model?"})
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        if call.state != DONE:
            return _no(HTTPStatus.CONFLICT, Text("этот разговор ещё не распознан"))
        if not store.sound(call).is_file():
            return _no(HTTPStatus.CONFLICT, Text("звука этой записи нет"))
        known, own = self._gateway_models(), self._anew() is OWN
        if known is None:
            return _no(HTTPStatus.CONFLICT, Text("сервис распознавания не отвечает или не говорит, какие у него модели") if own
                       else Text("Толмач не отвечает или в нём нет выбора моделей"))
        wanted = [m for m in known if m.name == model]
        if not wanted:
            return _no(HTTPStatus.CONFLICT, OWN[transcriber.NO_MODEL] if own else Text("такой модели в Толмаче нет"))
        if not wanted[0].installed:
            return _no(HTTPStatus.CONFLICT, self._anew()[transcriber.NO_MODEL])
        if not store.redo(call_id, model):
            return _no(HTTPStatus.CONFLICT, Text("этот разговор уже распознаётся заново"))
        log.info("call %d is to be recognised anew with %s", call_id, model)
        return Answer(HTTPStatus.ACCEPTED, {})

    def _unask(self, call_id: int, query: dict, given: dict) -> Answer:
        """The request is taken back: the conversation keeps the text it has."""
        store = self._app.archive()
        if store is None or not store.cancel_redo(call_id):
            return _no(HTTPStatus.CONFLICT, Text("этот разговор не ждёт распознавания заново"))
        log.info("call %d is not to be recognised anew after all", call_id)
        return Answer(data={})

    def _make(self, call_id: int, query: dict, given: dict) -> Answer:
        """This is where the text of a conversation is sent away: on this request, for this one. It
        is the next one to be made, whatever waits already."""
        summariser = self._app.summariser()
        if summariser is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        if given.get("old", "") not in OLD:
            return Answer(HTTPStatus.BAD_REQUEST, {"error": "what of the line that stands still?"})
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        if not self._said(found[0], call_id):
            return _no(HTTPStatus.CONFLICT, Text("у разговора нет текста"))
        taken = summariser.send([call_id], first=True, anew=bool(given.get("anew")), old=given.get("old", ""))
        if taken is None:
            return _stands()
        if not taken:
            return _no(HTTPStatus.CONFLICT, Text("выжимка этого разговора уже делается"))
        return Answer(HTTPStatus.ACCEPTED, {})

    def _send(self, query: dict, given: dict) -> Answer:
        """...and here the texts of several: on this request, for these, one after another."""
        summariser, store = self._app.summariser(), self._app.archive()
        if summariser is None or store is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        ids, old = given.get("ids"), given.get("old", "")
        if not isinstance(ids, list) or not 0 < len(ids) <= MANY or old not in OLD \
                or not all(type(call_id) is int and 0 < call_id < 10 ** 15 for call_id in ids):
            return Answer(HTTPStatus.BAD_REQUEST, {"error": "which conversations, and what of the line that stands still?"})
        with_text = [call_id for call_id in ids if self._said(store, call_id)]
        if not with_text:
            return Answer(HTTPStatus.ACCEPTED, {"taken": []})
        taken = summariser.send(with_text, old=old)
        return _stands() if taken is None else Answer(HTTPStatus.ACCEPTED, {"taken": taken})

    def _said(self, store: Store, call_id: int) -> bool:
        """A recognised conversation in which something was said: only such a one has a text to send."""
        call = store.get(call_id)
        return call is not None and call.state == DONE and store.remarks(call_id) > 0

    def _restart(self, query: dict, given: dict) -> Answer:
        summariser = self._app.summariser()
        if summariser is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        if not summariser.restart():
            return _no(HTTPStatus.CONFLICT, Text("очередь выжимок не остановлена"))
        return Answer(HTTPStatus.ACCEPTED, {})

    def _reset(self, query: dict, given: dict) -> Answer:
        summariser = self._app.summariser()
        if summariser is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        if not summariser.reset():
            return _no(HTTPStatus.CONFLICT, Text("очередь выжимок не остановлена"))
        return Answer(data={})

    def _halt(self, query: dict, given: dict) -> Answer:
        summariser = self._app.summariser()
        if summariser is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        return Answer(data={"dropped": summariser.halt()})

    def _retry(self, call_id: int, query: dict, given: dict) -> Answer:
        store = self._app.archive()
        if store is None or not store.retry(call_id):
            return _no(HTTPStatus.CONFLICT, Text("этот разговор не ждёт повтора"))
        return Answer(data={})

    def _forget(self, call_id: int, query: dict, given: dict) -> Answer:
        store = self._app.archive()
        if store is None:
            return _no(HTTPStatus.SERVICE_UNAVAILABLE, Text("архив не открыт"))
        call = store.get(call_id)
        try:
            gone = store.forget(call_id)
        except OSError as e:
            log.warning("call %d: its files could not be deleted (%s): it stays", call_id, type(e).__name__)
            return _no(HTTPStatus.CONFLICT, Text("файлы записи сейчас заняты — закройте то, что их держит, и повторите"))
        if not gone:
            busy = Text("этот разговор сейчас записывается") if call is not None and call.state == RECORDING else \
                Text("этот разговор сейчас распознаётся")
            return _no(HTTPStatus.CONFLICT, busy)
        summariser = self._app.summariser()
        if summariser is not None:
            summariser.drop(call_id)        # it waits for no summary any more
        if call is not None:
            log.info("call %d (%s) is deleted from the panel", call_id, call.folder)
        return Answer(data={})

    def _show(self, call_id: int, query: dict, given: dict) -> Answer:
        found = self._one(call_id)
        if isinstance(found, Answer):
            return found
        store, call = found
        if not store.sound(call).is_file():
            return _no(HTTPStatus.NOT_FOUND, Text("звука этой записи нет"))
        self._reveal(store.sound(call))
        return Answer(data={})

    def _discard(self, query: dict, given: dict) -> Answer:
        """Throw away the recording that goes on - the one the page means: while the user looked for the
        button that conversation may have ended and another begun."""
        if "id" in given and given["id"] != self._recording(self._app.archive()):
            return _no(HTTPStatus.CONFLICT, Text("эта запись уже закончилась"))
        self._app.discard_current()
        return Answer(data={})
