"""The gateway: the program that turns sound into words - Tolmach on this computer, unless the
settings name a server of the user's own.

A piece of a recording goes there as a WAV file; its phrases come back, each with its
place in the piece. One request at a time, so the standard library is enough.

A server of one's own is asked the same way - the request is that of the OpenAI transcription
API, which Tolmach answers too - but it is not Tolmach: it is not asked for its version, it may
want no key, and it may want to be told which model to read with.

Two kinds of trouble are kept apart. `Unavailable`: there is nobody to ask right now -
nothing is wrong with the conversation, it waits. `Refused`: the gateway was asked and
did not give the text - that is an attempt that failed.

A newer gateway has more than one model: the main one, which reads everything unless told
otherwise, and others, which a piece may name - for a conversation in another language.
"""
from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from protocallist.config import GatewayConfig

MIN_VERSION = (0, 8, 0)     # the first that answers with the places of the phrases
DOWN, LOADING, OLD = "down", "loading", "old"

class _Stay(urllib.request.HTTPRedirectHandler):
    """A server that sends the asker on to another address is not followed: that is an answer, and an error."""

    def redirect_request(self, *request) -> None:
        return None


# The sound of a conversation, and the key, go to the address of the gateway and nowhere else: never by
# way of a proxy, whatever one this computer is told to use for everything else, and never on to
# wherever the answer points.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Stay())


class Unavailable(Exception):
    """The gateway is not running (DOWN), still loads its model (LOADING), or is too old (OLD)."""

    def __init__(self, kind: str, why: str = "") -> None:
        super().__init__(why or kind)
        self.kind = kind


class Refused(Exception):
    """The gateway was asked and did not give the text of the piece."""


class NoModel(Refused):
    """The model the piece named is not on this computer."""


class ModelFailed(Refused):
    """The gateway could not load the model the piece named."""


class TimedOut(Refused):
    """The gateway did not give the text of the piece in time."""


TIMEOUT_S = 600.0       # how long the text of one piece is waited for


@dataclass(frozen=True)
class Model:
    name: str
    language: str       # what it hears: "ru", "en"
    main: bool          # the one that reads a piece which names no model
    installed: bool     # its files are on this computer


@dataclass(frozen=True)
class Segment:
    start: float        # seconds from the beginning of the piece
    end: float
    text: str


def locate(cfg: GatewayConfig, home: Path | None = None) -> tuple[str, str]:
    """(address, key). A server of one's own is where the settings say, with the key of the file
    they name - or with none. Otherwise it is Tolmach on this computer: the port and the key it
    keeps in two files of its folder."""
    tolmach = home or Path.home() / ".tolmach"
    try:
        if cfg.url:
            return cfg.url.rstrip("/"), Path(cfg.key_file).read_text(encoding="utf-8-sig").strip() if cfg.key_file else ""
        key = (Path(cfg.key_file) if cfg.key_file else tolmach / "key").read_text(encoding="utf-8-sig").strip()
        port = int(json.loads((tolmach / "gateway.json").read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Unavailable(DOWN, f"{type(e).__name__}: {e}") from e
    return f"http://127.0.0.1:{port}", key


def _version(text: str) -> tuple[int, ...]:
    """The numbers of a version, whatever stands around them: "v0.10.0-beta" is 0.10.0."""
    return tuple(int(number) for number in re.findall(r"\d+", text))


def _said(error: urllib.error.HTTPError) -> tuple[str, str]:
    """(the code, the words) the gateway wrote into its refusal; it never repeats what was in the sound."""
    try:
        told = json.loads(error.read().decode("utf-8"))["error"]
        return str(told.get("code") or ""), str(told["message"])[:200]
    except Exception:
        return "", str(error.reason)[:200]


class Gateway:
    def __init__(self, url: str, key: str, timeout: float = TIMEOUT_S, *, own: bool = False, model: str = "") -> None:
        self._url, self._key, self._timeout = url, key, timeout
        self._own = own         # a server of the user's own, not Tolmach
        # Which model gives the text: Tolmach says it when it is checked; a server of one's own
        # reads with the one the settings name - if they name none, nobody knows.
        self.model = model if own else ""

    def _asked_with(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def check(self) -> None:
        """Is it there, ready, and new enough? Unavailable if not. A server of one's own says
        nothing of itself that could be relied on: it is there if anything answers at its address."""
        if self._own:
            try:
                with _direct.open(urllib.request.Request(self._url, headers=self._asked_with()), timeout=5.0):
                    pass
            except urllib.error.HTTPError:      # an answer, whatever it says: somebody is there
                pass
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise Unavailable(DOWN, f"{type(e).__name__}: {e}") from e
            return
        try:
            with _direct.open(self._url + "/health", timeout=5.0) as response:
                health = json.loads(response.read().decode("utf-8"))
        # nobody listens, or something else answers on that port
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise Unavailable(DOWN, f"{type(e).__name__}: {e}") from e
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise Unavailable(LOADING, str(health.get("error") or health.get("status")) if isinstance(health, dict) else "")
        if _version(str(health.get("version", ""))) < MIN_VERSION:
            raise Unavailable(OLD, str(health.get("version")))
        self.model = str(health.get("model") or "")

    def models(self) -> list[Model] | None:
        """The models of the gateway, the main one first - or None if it knows no such question:
        an older gateway has one model, and then no piece can name one. A gateway that stumbles
        over the question - no answer in time, an answer that is no list - is Unavailable, like
        one that is not there: it is asked again, not taken for an older one."""
        request = urllib.request.Request(self._url + "/v1/models", headers=self._asked_with())
        try:
            with _direct.open(request, timeout=5.0) as response:
                answer = response.read()
        except urllib.error.HTTPError as e:
            # A server of one's own that refuses the question, or sends the asker on, does not know it -
            # like an older Tolmach.
            if e.code == 404 or self._own and e.code < 500:
                return None
            raise Unavailable(DOWN, f"the gateway answered {e.code} when asked for its models") from e
        except (OSError, ValueError, http.client.HTTPException) as e:       # ValueError: a key that cannot be sent
            raise Unavailable(DOWN, f"{type(e).__name__} when the gateway was asked for its models") from e
        try:
            told = json.loads(answer.decode("utf-8"))["data"]
            if self._own:       # it lists what it has; what it does not say of a model is taken from the settings
                return [Model(str(m["id"]), str(m.get("language") or ""), bool(m.get("main", str(m["id"]) == self.model)),
                              bool(m.get("installed", True))) for m in told]
            return [Model(str(m["id"]), str(m.get("language") or ""), bool(m.get("main")), bool(m.get("installed")))
                    for m in told]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            if self._own:       # an answer to some other question: it has no choice of models to offer
                return None
            raise Unavailable(DOWN, f"{type(e).__name__} when the gateway was asked for its models") from e

    def transcribe(self, wav: bytes, model: str = "") -> list[Segment]:
        """The phrases of one piece of sound (a 16 kHz WAV file), in the order they were said.
        `model` names the model that is to read it; none - the main one of Tolmach, or the one
        the settings name for a server of one's own."""
        model = model or self.model if self._own else model
        boundary = uuid.uuid4().hex
        named = f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode() if model else b""
        body = b"".join([
            named,
            f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="piece.wav"\r\n'
            f'Content-Type: audio/wav\r\n\r\n'.encode(), wav, f"\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(
            self._url + "/v1/audio/transcriptions", data=body, method="POST",
            headers={**self._asked_with(), "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with _direct.open(request, timeout=self._timeout) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code, words = _said(e)
            if e.code == 503:
                raise Unavailable(LOADING, words) from e
            if code == "model_not_installed":
                raise NoModel(words) from e
            if code == "model_failed":
                raise ModelFailed(f"the gateway answered {e.code}: {words}") from e
            raise Refused(f"the gateway answered {e.code}: {words}") from e
        except TimeoutError as e:
            raise TimedOut(f"no answer in {self._timeout:.0f} s") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise TimedOut(f"no answer in {self._timeout:.0f} s") from e
            raise Unavailable(DOWN, str(e.reason)) from e
        except (OSError, http.client.HTTPException) as e:       # it went away in the middle of its answer
            raise Unavailable(DOWN, f"{type(e).__name__}: {e}") from e
        except ValueError as e:
            raise Refused("the answer of the gateway cannot be read") from e
        try:
            # No phrases may be said as nothing at all; and a phrase may come with blanks around it.
            return [Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip()) for s in answer["segments"] or []]
        except (KeyError, TypeError, ValueError) as e:
            raise Refused(f"the answer of the gateway has no phrases ({type(e).__name__})") from e


def connect(cfg: GatewayConfig) -> Gateway:
    """The gateway, found and checked - or Unavailable."""
    gateway = Gateway(*locate(cfg), own=bool(cfg.url), model=cfg.model)
    gateway.check()
    return gateway
