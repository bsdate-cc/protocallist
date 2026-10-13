"""Updates through git for an install that is a clone.

An update is only ever a fast-forward of `main` from `origin`, and only for a clone
that is a pure consumer: on `main`, with no commits of its own and no edited files.
Anything else is somebody's working copy and is left alone. Nothing here installs by
itself - the tray shows a menu line and acts on a click.

Besides origin, the code on disk is compared with the code that runs: after a merge or a
pull made by hand the program offers to restart, and that needs no origin at all.

Standard library only: the launcher window imports this before the libraries exist.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from protocallist.i18n import Text, _

BRANCH = "main"
UPSTREAM = f"origin/{BRANCH}"
# A healthy fetch of this repository takes a second or two; a dead network must not
# hold the tray for minutes.
FETCH_TIMEOUT = 15.0
_VERSION = re.compile(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"', re.MULTILINE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Update(NamedTuple):
    kind: str                    # "none" | "behind" | "off" | "unknown" (origin did not answer)
    version: str | None = None   # the version waiting on origin, for the menu line
    note: Text | None = None     # why a clone gets no updates; None when there is nothing to say
    commit: str | None = None    # origin's commit this was seen at: what a click agrees to


class Pulled(NamedTuple):
    old: str
    new: str
    requirements_changed: bool


class UpdateError(Exception):
    """The update could not be taken; the message is for the user, in the language of the
    moment it is shown (it is made from a Text)."""


def _environment() -> dict:
    """git must fail rather than ask: every caller runs under pythonw, where a prompt has
    nowhere to appear. The console prompt and the credential manager's window are switched
    off; stored credentials still work. ssh is left exactly as the user has it: setting
    GIT_SSH_COMMAND here would override their core.sshCommand or GIT_SSH (an agent, plink)
    and break a fetch that works. An ssh that waits for a passphrase is ended by the timeout."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10.0,
                       stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def _run(args: list[str], cwd: Path, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Run a command without a console window and give its output back as UTF-8 text
    (git writes UTF-8; the console code page fails on a Cyrillic path).

    The output goes to files, not pipes, and a timeout ends the whole process tree. A
    fetch leaves helpers behind (git-remote-https, the credential manager); on Windows a
    pipe stays open while any of them holds it, and subprocess.run waits for that - long
    after its own timeout has passed, for as long as the helper hangs."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   env=_environment(), creationflags=_NO_WINDOW)
        try:
            code = process.wait(timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process.pid)
            process.kill()
            try:
                process.wait(5.0)
            except subprocess.TimeoutExpired:
                pass
            raise
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(args, code, out.read().decode("utf-8", "replace"),
                                           err.read().decode("utf-8", "replace"))


def _git(root: Path, *args: str, timeout: float = 120.0) -> tuple[bool, str]:
    """(succeeded, stdout) of one git command; a missing git or a timeout is a failure."""
    try:
        done = _run(["git", *args], root, timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return done.returncode == 0, done.stdout.strip()


def _is_clone_root(root: Path) -> bool:
    # A folder inside somebody else's repository is not an install of ours.
    ok, top = _git(root, "rev-parse", "--show-toplevel")
    try:
        return ok and Path(top).resolve() == Path(root).resolve()
    except OSError:
        return False


# ---- the code on disk and the code that runs

def parse_version(text: str) -> str | None:
    found = _VERSION.search(text)
    return found.group(1) if found else None


def disk_version(root: Path) -> str | None:
    """The version of the code on disk - which is not the running one after a merge or a pull."""
    try:
        return parse_version((Path(root) / "protocallist" / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return None


def code_digest(root: Path) -> str | None:
    """A fingerprint of the program's code on disk. When it is not the one the running
    program started with, the code that runs is no longer the code that is there - also
    after a change that left the version number alone. None if there is nothing to read."""
    digest = hashlib.sha256()
    try:
        files = sorted((Path(root) / "protocallist").rglob("*.py"))
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    except OSError:
        return None
    return digest.hexdigest() if files else None


# ---- the libraries and the requirements.txt they were installed from

def requirements_digest(root: Path) -> str | None:
    """A fingerprint of requirements.txt: when it differs from the one the running program
    started with, a restart needs the libraries installed first."""
    try:
        return hashlib.sha256((Path(root) / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return None


def stamp_file() -> Path:
    """Where the record lives: inside the environment it describes."""
    return Path(sys.prefix) / "protocallist-requirements.sha256"


def installed_digest() -> str | None:
    try:
        return stamp_file().read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeError):
        return None


def mark_installed(root: Path) -> None:
    """Record which requirements.txt the libraries were just installed from. Best effort."""
    digest = requirements_digest(root)
    if digest is None:
        return
    try:
        stamp_file().write_text(digest + "\n", encoding="ascii")
    except OSError:
        pass


def libraries_stale(root: Path) -> bool:
    """True when requirements.txt is not the one the libraries were last installed from -
    a library was added, a pinned version moved, or an install failed half-way. An import
    test cannot see all of that: an old version imports fine. With no record (an install
    older than the record) the answer is no, and the launcher makes one."""
    recorded, current = installed_digest(), requirements_digest(root)
    return recorded is not None and current is not None and recorded != current


# ---- origin

def version_at(root: Path, ref: str) -> str | None:
    ok, text = _git(root, "show", f"{ref}:protocallist/__init__.py")
    return parse_version(text) if ok else None


def status(root: Path) -> Update:
    """Ask origin whether there is something to take. An origin that does not answer is
    "unknown" - never "behind", and never "nothing new" either. A clone without an origin
    is "off" and is told nothing: there is nobody to ask."""
    if not _is_clone_root(root) or not _git(root, "remote", "get-url", "origin")[0]:
        return Update("off")
    ok, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        return Update("off")
    if branch != BRANCH:
        if branch == "HEAD":
            return Update("off", note=Text("без ветки — обновления выключены"))
        return Update("off", note=Text("ветка {branch} — обновления выключены", branch=branch))
    if not _git(root, "fetch", "--quiet", "origin", BRANCH, timeout=FETCH_TIMEOUT)[0]:
        return Update("unknown")
    head, theirs = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", UPSTREAM)
    if not (head[0] and theirs[0]):
        return Update("unknown")
    if head[1] == theirs[1]:
        return Update("none")
    if _git(root, "merge-base", "--is-ancestor", "HEAD", UPSTREAM)[0]:
        return Update("behind", version_at(root, UPSTREAM), commit=theirs[1])
    if _git(root, "merge-base", "--is-ancestor", UPSTREAM, "HEAD")[0]:
        return Update("off", note=Text("есть свои коммиты — обновления выключены"))
    return Update("off", note=Text("история разошлась с origin — обновления выключены"))


def pull(root: Path, expected: Update) -> Pulled:
    """Take the update the user clicked on: fast-forward main to origin's.

    The menu line this answers may be hours old, so nothing it was based on is trusted:
    origin is asked again, and the clone must still be a consumer on main, strictly
    behind, with the very commit of `expected` waiting there. A clone with edited tracked
    files is refused - an install is not edited, and somebody's edits are not ours to move."""
    ok, changed = _git(root, "status", "--porcelain", "--untracked-files=no")
    if not ok:
        raise UpdateError(Text("git не отвечает — обновление не выполнено"))
    if changed:
        raise UpdateError(Text("в папке программы есть изменённые файлы — обновление не выполнено"))
    update = status(root)
    if update.kind == "unknown":
        raise UpdateError(Text("нет связи с origin — обновление не выполнено"))
    if update.kind != "behind":
        raise UpdateError(update.note or Text("обновлять нечего: установлена последняя версия"))
    if update.commit != expected.commit:
        raise UpdateError(Text("на origin уже другое обновление (версия {version}) — нажмите «Обновить» ещё раз",
                               version=update.version or "?"))
    ok, old = _git(root, "rev-parse", "HEAD")
    if not ok:
        raise UpdateError(Text("git не отвечает — обновление не выполнено"))
    try:
        done = _run(["git", "merge", "--ff-only", "--quiet", update.commit], root)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(Text("git merge не выполнен: {reason}", reason=type(e).__name__)) from e
    if done.returncode != 0:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise UpdateError(Text("git merge не выполнен: {reason}", reason=said[-1] if said else _("неизвестная ошибка")))
    new = _git(root, "rev-parse", "HEAD")[1]
    touched = _git(root, "diff", "--name-only", old, new, "--", "requirements.txt")[1]
    return Pulled(old, new, bool(touched))


# ---- what the menu says

def menu_label(update: Update, running: str, on_disk: str | None, code_changed: bool = False,
               stale_libraries: bool = False) -> str | None:
    """The one menu line of the updater, or None when there is nothing to do. An update
    from origin comes first: taking it restarts the program anyway; then newer code on
    disk, by its version if that changed; then libraries that an earlier install left behind."""
    if update.kind == "behind":
        return _("Обновить до {version}").format(version=update.version) if update.version else _("Обновить")
    if on_disk and on_disk != running:
        return _("Перезапустить: на диске версия {version}").format(version=on_disk)
    if code_changed:
        return _("Перезапустить: код на диске изменился")
    if stale_libraries:
        return _("Поставить библиотеки: requirements.txt изменился")
    return None
