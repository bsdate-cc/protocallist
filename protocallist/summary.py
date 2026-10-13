"""A summary of one conversation - on a click, for that conversation only.

This is the one place where what was said leaves the computer: the text of the
conversation goes to Claude Code (`claude -p`), which the user has installed and signed in
to - or to a command of the user's own, if the settings name one. Nothing here runs by
itself, and nothing of the text is logged. The text is given and the summary is given back:
where they are kept is the business of whoever asks (summaries.py).

Claude is run with no tools, with none of the user's settings, hooks or skills, and keeps
no session on disk: it reads the text and answers, and that is all it can do.

The command may be a program (claude.exe) or, as npm installs it, a file that cmd.exe runs
(claude.cmd) - and cmd.exe cuts a command at its first new line, the keys above included.
So no key is longer than a line: the instructions go as a file, the request is one line.

One thing of the user's settings of Claude Code is taken all the same: their `env` - how
Claude Code reaches the network on this computer, a proxy as a rule. Without it Claude run
from the tray could not be reached at all.

A command of one's own (`summary.command`) is run as it is written. The whole task comes on
its input - the instructions, the request, the text - and what it prints is the summary;
both UTF-8. What it does with the text is the business of whoever wrote it into the settings.

The instructions - what a summary is to say, and in which form - are those the program comes
with, unless the settings name a file of the user's own (`summary.prompt`). That file is read
here, for every summary: what the user changes in it holds for the very next one.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from protocallist.i18n import Text

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
TIMEOUT_S = 900.0
BATCH = (".cmd", ".bat")                # a file that cmd.exe runs
MANGLED = frozenset('\r\n"%^&|<>')      # what cmd.exe does not pass on as it is - and what follows it is lost too
NAME = 60                               # so much of a command of one's own is shown where the page names it
ECHO = 20                               # so many characters in a row found in the conversation are its words


class SummaryError(Exception):
    """The summary was not made; the argument is a Text for the user."""


def find() -> str | None:
    """The command of Claude Code: the program when there is one, else whatever `claude` is - as npm
    installs it, a file that cmd.exe runs."""
    return shutil.which("claude.exe") or shutil.which("claude")


def name(command: Sequence[str]) -> str:
    """A command of one's own as the page and the log name it: its words, without their folders."""
    return " ".join(Path(word).name or word for word in command)[:NAME]


def environment() -> dict[str, str]:
    """The environment Claude is run in: the one of this program and, over it, the `env` of the
    settings of Claude Code. Their names are upper case here: Windows does not tell cases apart."""
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        told = json.loads((folder / "settings.json").read_text(encoding="utf-8-sig")).get("env")
    except (OSError, ValueError, AttributeError):
        told = None
    told = {str(name).upper(): str(value) for name, value in told.items()} if isinstance(told, dict) else {}
    if told:
        log.debug("Claude is run with these of the settings of Claude Code: %s", ", ".join(sorted(told)))
    return {**os.environ, **told}


def prompt(language: str) -> tuple[Path, str]:
    """(the file of the standing instructions, the request - one line) in the language of the interface."""
    name = f"summary_{language if language in ('ru', 'en') else 'en'}"
    return PROMPTS / f"{name}.md", (PROMPTS / f"{name}_request.md").read_text(encoding="utf-8").strip()


def _standing(label: str, language: str, own: Path | None) -> tuple[Path, str, str]:
    """(the file of the standing instructions, what it says, the request - one line). The file is
    the user's own when there is one - and if that one cannot be used, nothing is sent anywhere."""
    built_in, request = prompt(language)
    if own is None:
        return built_in, built_in.read_text(encoding="utf-8").strip(), request
    try:
        said = own.read_text(encoding="utf-8-sig").strip()      # utf-8-sig: Notepad saves a mark before the text
        wrong = None if said else (Text("файл указаний пуст"), "it is empty")
    except UnicodeDecodeError as e:
        wrong = Text("файл указаний не в UTF-8"), type(e).__name__
    except OSError as e:
        wrong = Text("файл указаний не читается: {reason}", reason=type(e).__name__), type(e).__name__
    if wrong is not None:
        log.warning("summary of %s: the instructions of the user's own (%s) cannot be used: %s: nothing was sent",
                    label, own, wrong[1])
        raise SummaryError(wrong[0])
    return own, said, request


def ask(text: str, model: str, language: str, *, where: Path, label: str, command: Sequence[str] = (),
        instructions: Path | None = None, run: Callable = subprocess.run, find: Callable[[], str | None] = find,
        timeout: float = TIMEOUT_S) -> tuple[str, str]:
    """(the summary of the conversation whose text this is, who made it). The text goes to
    `command` when there is one, else to Claude Code. `instructions` is the file of the user's
    own instructions, if they have one. `where` is the folder the command is run in; `label` is
    what the conversation is called in the log."""
    began = time.monotonic()
    if command:
        result, maker = _by_command(label, where, text, list(command), language, instructions, run, timeout), name(command)
    else:
        result, maker = _by_claude(label, where, text, model, language, instructions, run, find, timeout), model
    log.info("summary of %s: %d characters sent to %s, %d came back in %.0f s", label, len(text), maker,
             len(result), time.monotonic() - began)
    return result.strip() + "\n", maker


def _whole(label: str, command: list[str]) -> None:
    """Refuse a command that cmd.exe would not pass on as it is, before anything is sent."""
    if not command[0].lower().endswith(BATCH):
        return
    cut = [command[i - 1] for i in range(1, len(command)) if MANGLED & set(command[i])]
    if cut:
        log.warning("summary of %s: the command is a file that cmd.exe runs, and cmd.exe would not pass on what "
                    "follows %s as it is: nothing was sent", label, cut[0])
        raise SummaryError(Text("cmd.exe не передаст команду выжимки как есть — см. журнал"))


def _said(stderr: bytes | None, text: str) -> str:
    """What a command said of its failure, for the log: the first line of it - unless that line
    repeats the conversation, which is never written there."""
    line = " ".join((stderr or b"").decode("utf-8", errors="replace").strip().partition("\n")[0].split())[:200]
    if not line:
        return ""
    said = " ".join(text.split())       # however the words of the conversation were spaced and broken into lines
    if any(line[i:i + ECHO] in said for i in range(max(1, len(line) - ECHO + 1))):
        return "; what it said repeats the conversation and is not written here"
    return f"; it said: {line}"


def _by_command(label: str, where: Path, text: str, command: list[str], language: str, own: Path | None,
                run: Callable, timeout: float) -> str:
    """The summary by a command of the user's own."""
    _, standing, request = _standing(label, language, own)
    task = f"{standing}\n\n{request}\n\n{text}"
    command = [shutil.which(command[0]) or command[0], *command[1:]]       # as a terminal would find it
    _whole(label, command)
    try:
        # A script in Python reads and prints in the encoding of Windows unless told otherwise.
        done = run(command, input=task.encode("utf-8"), capture_output=True, cwd=str(where), timeout=timeout,
                   env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as e:
        raise SummaryError(Text("команда выжимки не ответила за {minutes} мин", minutes=round(timeout / 60))) from e
    except OSError as e:
        raise SummaryError(Text("команда выжимки не запустилась: {reason}", reason=type(e).__name__)) from e
    try:
        result, why = done.stdout.decode("utf-8").replace("\r\n", "\n"), ""
    except UnicodeDecodeError:
        result, why = "", ", its answer is not UTF-8"
    if done.returncode != 0 or not result.strip():
        log.warning("summary of %s: the command gave none (exit code %s%s)%s", label, done.returncode, why,
                    _said(done.stderr, text))
        raise SummaryError(Text("команда выжимки не дала текста (код {code}) — см. журнал", code=done.returncode))
    return result


def _by_claude(label: str, where: Path, text: str, model: str, language: str, own: Path | None, run: Callable,
               find: Callable[[], str | None], timeout: float) -> str:
    """The summary by Claude Code."""
    claude = find()
    if claude is None:
        raise SummaryError(Text("не найден Claude Code (команда claude)"))
    instructions, _, request = _standing(label, language, own)
    command = [claude, "-p", request, "--model", model, "--system-prompt-file", str(instructions), "--tools", "",
               "--output-format", "json", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
               "--setting-sources", "project,local"]
    _whole(label, command)
    try:
        done = run(command, input=text.encode("utf-8"), capture_output=True, cwd=str(where), timeout=timeout,
                   env=environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as e:
        raise SummaryError(Text("Claude не ответил за {minutes} мин", minutes=round(timeout / 60))) from e
    except OSError as e:
        raise SummaryError(Text("Claude не запустился: {reason}", reason=type(e).__name__)) from e
    answer: dict = {}
    try:
        answer = json.loads(done.stdout.decode("utf-8"))
        result = "" if answer.get("is_error") else str(answer.get("result") or "")
    except (ValueError, AttributeError):
        result = ""
    if not result.strip():
        status = answer.get("api_error_status") if isinstance(answer, dict) else None
        if status is not None:      # the service refused: the words are its own, not those of the conversation
            log.warning("summary of %s: the service answered %s: %s", label, status, str(answer.get("result"))[:200])
            raise SummaryError(Text("Claude ответил ошибкой {status} — см. журнал", status=status))
        log.warning("summary of %s: Claude gave none (exit code %s, %s)%s", label, done.returncode,
                    answer.get("subtype") if isinstance(answer, dict) else "an answer that cannot be read",
                    _said(getattr(done, "stderr", b""), text))
        raise SummaryError(Text("Claude не дал выжимку — см. журнал"))
    return result
