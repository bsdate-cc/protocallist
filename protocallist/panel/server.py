"""The server of the panel: a page for the browser of this computer, served by the program itself.

It listens on 127.0.0.1 only. That alone protects nothing - any page open in the browser can
send requests to an address of this computer - and what the panel shows is whole conversations.
So there are three locks:

- the address: a request must be meant for 127.0.0.1 or localhost on this port (its Host), or
  a site whose name was made to point here could read the answers;
- the key: a secret kept in a file of the user's folder. The icon opens the panel by a link
  with the key; the browser keeps it as a cookie, so a tab that is open goes on working when
  the program is started again. Without it nothing is given but a page that says where to
  open the panel from. Other users of this computer cannot read the file - and their own
  copies of the program are served on ports of their own, see port.py;
- a request that changes anything must carry a header no other site can send, and come from
  this very address. And no request at all is answered if the browser says that another page
  made it - a page of another site, or of another port of this computer.

The page loads nothing from anywhere else, and nothing that was said is ever logged here.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from protocallist.i18n import N_, Text, _
from protocallist.panel.api import Answer

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/texts.json": "texts.json"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}
COOKIE, HEADER = "protocallist", "X-Protocallist"
POLICY = "default-src 'self'; frame-ancestors 'none'"
OURS = ("same-origin", "none")    # Sec-Fetch-Site of the panel's own requests, and of an address typed or opened
MOST = 1 << 20          # no request brings more than this: the numbers of a thousand conversations are some kilobytes
BLOCK = 1 << 16
STRANGER = N_("Откройте панель из значка Протоколиста: пункт «Открыть панель».")


def key(path: Path) -> str:
    """The secret of the panel: made once, kept in a file of the user's folder."""
    try:
        found = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        found = ""
    if len(found) < 32:
        found = secrets.token_urlsafe(32)
        path.write_text(found, encoding="ascii")
    return found


def _cookies(header: str, wanted: str) -> list[str]:
    """The values of the panel's cookie among whatever else the browser sends: cookies are kept by
    the name of a computer, not by its port, so those of every other local page come here as
    well - whatever their names and values look like, and one of them may bear the panel's name."""
    found = []
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == wanted:
            found.append(value)
    return found


class Panel:
    """`api.handle(method, path, query, body)` answers what the page asks; see api.py."""

    def __init__(self, api, port: int, secret: str, *, static: Path = STATIC) -> None:
        self.api, self._port, self.secret, self.static = api, port, secret, static
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        """Begin to serve. OSError if the port cannot be had."""
        server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler, bind_and_activate=False)
        server.allow_reuse_address = False      # a port another program listens on is not shared with it
        server.daemon_threads = True
        server.panel = self
        try:
            server.server_bind()
            server.server_activate()
        except OSError:
            server.server_close()
            raise
        self._server, self._port = server, server.server_address[1]
        threading.Thread(target=server.serve_forever, name="panel", daemon=True).start()
        log.info("the panel is served on port %d of this computer", self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def cookie(self) -> str:
        """The name of the panel's cookie. It bears the port: two panels of one computer - two copies of
        the program - do not write over each other's."""
        return f"{COOKIE}_{self._port}"

    def link(self) -> str:
        """What the icon opens: the address with the key, which the first answer turns into a cookie."""
        return f"http://127.0.0.1:{self._port}/?key={self.secret}"


class _Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "Protocallist", ""
    timeout = 30        # seconds a connection may say nothing - or take nothing - before it is let go of

    def log_message(self, *args) -> None:      # nothing a browser asked for goes to the log
        pass

    def do_GET(self) -> None:
        self._serve()

    do_POST = do_DELETE = do_GET

    def _serve(self) -> None:
        try:
            url = urlsplit(self.path)
        except ValueError:
            return self._plain(HTTPStatus.BAD_REQUEST, "this is not an address")
        try:
            self._answer(url.path, parse_qs(url.query))
        except (ConnectionError, TimeoutError):
            pass                                # the browser went away: a seek, a closed tab
        except Exception:
            log.exception("the panel could not answer %s %s", self.command, url.path)
            try:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(Text("внутренняя ошибка — см. журнал"))})
            except OSError:
                pass

    def _answer(self, path: str, query: dict[str, list[str]]) -> None:
        panel = self.server.panel
        ours = {f"127.0.0.1:{panel.port}", f"localhost:{panel.port}"}
        if self.headers.get("Host", "") not in ours:
            return self._plain(HTTPStatus.FORBIDDEN, "this address is not the panel")
        if self.headers.get("Sec-Fetch-Site", "none") not in OURS:
            return self._stranger()             # a page of another site, or of another port of this computer
        given = (query.get("key") or [""])[0]
        if self.command == "GET" and path == "/" and given:
            if not hmac.compare_digest(given.encode(), panel.secret.encode()):
                return self._stranger()
            self.send_response(HTTPStatus.FOUND)        # the key leaves the address line and becomes a cookie
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{panel.cookie}={panel.secret}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        brought = _cookies(self.headers.get("Cookie", ""), panel.cookie)
        if not any(hmac.compare_digest(value.encode("utf-8", "replace"), panel.secret.encode()) for value in brought):
            return self._stranger()
        if self.command != "GET":
            origin = self.headers.get("Origin")
            if self.headers.get(HEADER) != "1" or (origin is not None and origin not in {f"http://{host}" for host in ours}):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "not a request of the panel"})
        if self.command == "GET" and path in PAGES:
            return self._page(panel.static / PAGES[path])
        if not path.startswith("/api/"):
            return self._plain(HTTPStatus.NOT_FOUND, "there is no such page")
        if self.headers.get("Transfer-Encoding"):      # a body without a length must not pass for an empty one
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a body without a length"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a length that is not one"})
        if length > MOST:
            while length > 0:                   # taken and thrown away, so that the answer can be heard
                length -= len(self.rfile.read(min(BLOCK, length))) or length
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too much"})
        body = self.rfile.read(length) if length else b""
        answer: Answer = panel.api.handle(self.command, path, {name: values[0] for name, values in query.items()}, body)
        if answer.file is not None:
            return self._file(answer.file, answer.kind)
        if answer.text is not None:
            return self._download(answer.text, answer.name)
        self._json(answer.status, answer.data if answer.data is not None else {})

    # ---- what goes back

    def _head(self, status: int, kind: str, length: int, **more: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        for name, value in more.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()

    def _plain(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self._head(status, "text/plain; charset=utf-8", len(data))
        self.wfile.write(data)

    def _stranger(self) -> None:
        data = f'<!doctype html><meta charset="utf-8"><title>Protocallist</title><p>{_(STRANGER)}</p>'.encode("utf-8")
        self._head(HTTPStatus.FORBIDDEN, "text/html; charset=utf-8", len(data))
        self.wfile.write(data)

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._head(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _page(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return self._plain(HTTPStatus.NOT_FOUND, "there is no such page")
        self._head(HTTPStatus.OK, TYPES.get(path.suffix, "application/octet-stream"), len(data))
        self.wfile.write(data)

    def _download(self, text: str, name: str) -> None:
        data = text.encode("utf-8")
        self._head(HTTPStatus.OK, "text/markdown; charset=utf-8", len(data),
                   Content_Disposition=f"attachment; filename=\"conversation.md\"; filename*=UTF-8''{quote(name)}")
        self.wfile.write(data)

    def _file(self, path: Path, kind: str) -> None:
        """A file, or the part of it the browser asks for: that is how it seeks in a sound. The file
        is opened for one block at a time: a sound that is being listened to can still be deleted."""
        try:
            size = path.stat().st_size
            open(path, "rb").close()
        except OSError:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "the file cannot be opened"})
        first, last, wanted = 0, size - 1, self.headers.get("Range", "")
        part = wanted.startswith("bytes=") and "," not in wanted
        if part:
            begin, _, end = wanted[6:].partition("-")
            try:
                if begin:
                    first, last = int(begin), min(int(end), size - 1) if end else size - 1
                else:
                    first = max(0, size - int(end))
            except ValueError:
                part = False
            if part and not 0 <= first <= last:
                return self._head(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, kind, 0, Content_Range=f"bytes */{size}")
        if part:
            self._head(HTTPStatus.PARTIAL_CONTENT, kind, last - first + 1, Accept_Ranges="bytes",
                       Content_Range=f"bytes {first}-{last}/{size}")
        else:
            first, last = 0, size - 1
            self._head(HTTPStatus.OK, kind, size, Accept_Ranges="bytes")
        at, left = first, last - first + 1
        while left > 0:
            try:
                with open(path, "rb") as source:
                    source.seek(at)
                    block = source.read(min(BLOCK, left))
            except OSError:
                break                           # deleted meanwhile: the browser gets what there was
            if not block:
                break
            self.wfile.write(block)
            at, left = at + len(block), left - len(block)
