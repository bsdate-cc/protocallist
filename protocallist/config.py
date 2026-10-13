"""config.json: the settings of the service.

load() tolerates: it never raises, and every rejected field falls back to its default
with a Problem saying why - a recorder that will not start over a stray comma is worse
than one running on defaults and saying so.

One default is not fallen back to: the days the sound is kept. Sound is not deleted by a
rule its owner did not write.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import get_origin, get_type_hints
from urllib.parse import urlsplit

from protocallist import paths


@dataclass
class RecordingConfig:
    # Off until the user switches it on: a program that records conversations must not
    # start doing so by itself.
    enabled: bool = False
    # The microphone whose use by another program means a conversation, as Windows names it.
    microphone: str = ""
    # The playback device to record what is heard from; "" - the one of the same headset.
    speaker: str = ""
    # Programs whose use of the microphone is not a conversation, by file name.
    ignore: list[str] = field(default_factory=lambda: ["python.exe", "pythonw.exe", "SystemSettings.exe",
                                                       "rundll32.exe"])
    # Shorter than this is a program that touched the microphone, not a conversation. Kept low on
    # purpose: a short call that was thrown away cannot be brought back.
    min_seconds: float = 5.0
    # How long a microphone that was let go of may still be the same conversation: a program that
    # opens its device anew, or a call that dropped, is back within seconds. Longer, and two
    # meetings in a row in one program would be one recording.
    linger_seconds: float = 5.0
    poll_seconds: float = 1.0


@dataclass
class StorageConfig:
    # Days the sound of a recognised conversation is kept; 0 - not at all, below 0 - for ever.
    keep_audio_days: int = 30


@dataclass
class GatewayConfig:
    # A server of one's own that turns sound into words, by its address; "" - the gateway of Tolmach on this computer.
    url: str = ""
    # The file with the key the server is asked with; "" - the key file of Tolmach, and for a server of one's own no key.
    key_file: str = ""
    # The model a server of one's own is asked to read with; "" - none is named. Tolmach reads with its main model.
    model: str = ""


@dataclass
class SummaryConfig:
    # The model Claude Code is asked for when a summary is made: an alias ("opus", "sonnet") or a full name.
    model: str = "opus"
    # A command of one's own to make summaries with, word by word; none - Claude Code is asked. It gets the
    # whole task on its input - the instructions, then the text - and prints the summary; both UTF-8.
    command: list[str] = field(default_factory=list)
    # A file with instructions of one's own for whoever makes the summary - what to write and in which
    # form - in place of those the program comes with; none - those. A name without a folder is looked
    # for beside this file. It is read anew for every summary.
    prompt: str = ""


@dataclass
class PanelConfig:
    # The port of this computer on which the panel in the browser is served. 0: not chosen yet -
    # the program chooses the port of this Windows account at its first start and writes it here.
    port: int = 0


@dataclass
class Config:
    # The language of the tray menu: "ru", "en", or "auto" - by the language of Windows.
    language: str = "auto"
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)


@dataclass
class Problem:
    where: str
    message: str


@dataclass
class Loaded:
    config: Config
    problems: list[Problem]


_CHECKS = {
    "language": (lambda v: v in ("auto", "ru", "en"), "must be 'auto', 'ru' or 'en'"),
    "recording.min_seconds": (lambda v: v >= 0, "must be >= 0"),
    "recording.linger_seconds": (lambda v: v >= 0, "must be >= 0"),
    "recording.poll_seconds": (lambda v: 0.2 <= v <= 10, "must be 0.2..10"),
    "panel.port": (lambda v: v == 0 or 1024 <= v <= 65535, "must be 1024..65535, or 0 for the program to choose"),
    "gateway.url": (lambda v: v == "" or urlsplit(v).scheme in ("http", "https") and bool(urlsplit(v).netloc),
                    "must be an address that begins with http:// or https://, or be empty"),
}


def _coerce(value, hint):
    """(ok, value) for one leaf. A bool is not an int here; an int is a fine float."""
    if hint is bool:
        return isinstance(value, bool), value
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool), value
    if hint is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return ok, float(value) if ok else value
    if hint is str:
        return isinstance(value, str), value
    if get_origin(hint) is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value), value
    return False, value


def _name(hint) -> str:
    return "a list of strings" if get_origin(hint) is list else hint.__name__


def _merge(obj, data, where: str, problems: list[Problem]) -> None:
    if not isinstance(data, dict):
        problems.append(Problem(where or "<root>", "expected an object, defaults kept"))
        return
    hints = get_type_hints(type(obj))
    known = {f.name for f in fields(obj)}
    for key in data:
        if key not in known:
            problems.append(Problem(f"{where}.{key}" if where else str(key), "unknown key, ignored"))
    for f in fields(obj):
        if f.name not in data:
            continue
        path = f"{where}.{f.name}" if where else f.name
        value, hint = data[f.name], hints[f.name]
        current = getattr(obj, f.name)
        if is_dataclass(current):
            _merge(current, value, path, problems)
            continue
        ok, value = _coerce(value, hint)
        if not ok:
            problems.append(Problem(path, f"expected {_name(hint)}, default kept"))
            continue
        check = _CHECKS.get(path)
        if check is not None and not check[0](value):
            problems.append(Problem(path, f"{check[1]}, default kept"))
            continue
        setattr(obj, f.name, value)


def load(path: Path | None = None) -> Loaded:
    path = path or paths.config_file()
    cfg = Config()
    try:
        # utf-8-sig: Notepad saves a BOM, and json refuses one
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Loaded(cfg, [])
    except (OSError, UnicodeDecodeError) as e:
        return Loaded(cfg, [Problem("<file>", f"cannot read, defaults used: {e}")])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return Loaded(cfg, [Problem("<file>", f"not JSON, defaults used: {e}")])
    problems: list[Problem] = []
    _merge(cfg, data, "", problems)
    if any(p.where == "storage" or p.where.startswith("storage.") for p in problems):
        # How long the sound is kept was written, and not so that it can be read: thirty days is not what was meant.
        cfg.storage.keep_audio_days = -1
        problems.append(Problem("storage", "no sound is deleted until this is put right"))
    return Loaded(cfg, problems)


def unparsed(loaded: Loaded) -> bool:
    """The file exists but nothing in it could be used: writing it back would destroy the user's edit."""
    return any(p.where in ("<file>", "<root>") for p in loaded.problems)


def put(section: str, name: str, value, path: Path | None = None) -> bool:
    """Write one value into the file and leave everything else in it as it is - what could not be
    used and what is not a setting at all included: the file is the user's. False, and nothing
    written, if the file cannot be read as settings. A file that is not there is made, whole."""
    path = path or paths.config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        data = asdict(Config())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.setdefault(section, {}), dict):
        return False
    data[section][name] = value
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or paths.config_file()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
