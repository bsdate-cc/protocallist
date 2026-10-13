"""The tray icon: what the service is doing now, the few things one may tell it, and the way
into the panel - the page in the browser where the conversations are read and listened to.

Nothing ever pops up from here by itself: a notice "a conversation is being recorded" would
land on a screen that is being shown to the other side at that very moment. The icon, its
tooltip and its menu are all there is - but for one window, which is the answer to a click of
the user's own: the one that says the panel they asked for could not be started.
"""
from __future__ import annotations

import logging
import os
import subprocess
import ctypes
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

import pystray

from protocallist import VERSION, config, gateway, i18n, paths
from protocallist import app as core
from protocallist.i18n import N_, Text, _
from protocallist.panel import port as ports
from protocallist.tray import autostart, icons, relaunch, updater

log = logging.getLogger(__name__)

WATCH_S = 30.0          # how often the code on disk is compared with the code that runs
UPDATE_EVERY = 720      # looks at the disk between two looks at origin: 6 hours

TITLE = N_("Протоколист")
NO_MICROPHONES = N_("микрофонов нет")
CHECK_UPDATES = N_("Проверить обновления")
# Why conversations wait, when it is the gateway they wait for.
GATEWAY = {gateway.DOWN: N_("ждёт шлюз: {waiting}"), gateway.LOADING: N_("ждёт шлюз: {waiting}"),
           gateway.OLD: N_("нужен Толмач {version} или новее, ждут: {waiting}")}
TOOLTIP = 127       # Windows takes no more for the tooltip of an icon
# What the tooltip says in the states that need no more words than these.
LINES = {core.OFF: N_("запись выключена"), core.NO_MICROPHONE: N_("микрофон не выбран"),
         core.MISSING: N_("микрофон не найден"), core.WAITING: N_("ждёт разговора")}
# Nothing is watched without a microphone, whether it was never chosen or is unplugged: the icon of "off".
IMAGES = {core.OFF: icons.OFF, core.NO_MICROPHONE: icons.OFF, core.MISSING: icons.OFF,
          core.WAITING: icons.WAITING, core.RECORDING: icons.RECORDING, core.ERROR: icons.ERROR}


def queue(glance: core.Glance) -> str:
    """What goes on with recognition, or "" when nothing waits for it."""
    parts = []
    if glance.recognising:
        parts.append(_("распознаётся: 1, ждут: {waiting}").format(waiting=glance.waiting))
    elif glance.waiting:
        why = GATEWAY.get(glance.gateway, N_("ждут распознавания: {waiting}"))
        parts.append(_(why).format(waiting=glance.waiting, version=".".join(map(str, gateway.MIN_VERSION))))
    if glance.failed:
        parts.append(_("не распознано: {failed}").format(failed=glance.failed))
    return "; ".join(parts)


def line(glance: core.Glance) -> str:
    """One line about what is going on, in the current language."""
    if glance.state == core.RECORDING:
        now = _("идёт запись: {program}, {minutes} мин").format(program=glance.program, minutes=glance.minutes)
    elif glance.state == core.ERROR:
        now = _("ошибка: {problem}").format(problem=_(glance.problem))
    else:
        now = _(LINES[glance.state])
    waits = queue(glance)
    return f"{now}; {waits}" if waits else now


def title(glance: core.Glance) -> str:
    return f"{_(TITLE)} {VERSION}: {line(glance)}"[:TOOLTIP]


def picture(glance: core.Glance) -> str:
    """Which picture the icon wears. A recording and an error come first; otherwise the dots
    of recognition, while it is at work."""
    if glance.recognising and glance.state not in (core.RECORDING, core.ERROR):
        return icons.TRANSCRIBING
    return IMAGES[glance.state]


def plain(name: str) -> str:
    """A name from outside as a row of the menu shows it: Windows takes a single & for the mark of
    a hot key and does not draw it; a doubled one is drawn as one."""
    return name.replace("&", "&&")


def retry_or_cancel(text: str) -> bool:
    """Tell the user in a window, and ask whether to try again: True for "Retry". The one window
    this program ever shows - and only as the answer to a click of the user's own."""
    retry_cancel, warning, foreground, on_top, retry = 0x5, 0x30, 0x10000, 0x40000, 4     # on top: it has no window to belong to
    return ctypes.windll.user32.MessageBoxW(None, text, _(TITLE), retry_cancel | warning | foreground | on_top) == retry


def _menu_action(fn):
    """pystray calls a menu handler inside the icon window's message handling, so a
    handler that blocks there freezes the icon. Every handler therefore runs in its own
    thread, and one that fails is logged, not lost."""

    def run(icon, item):
        try:
            fn(icon, item)
        except Exception:
            log.exception("tray menu action failed")

    def handler(icon, item):
        threading.Thread(target=run, args=(icon, item), daemon=True).start()

    return handler


class TrayApp:
    """`make_app(on_change)` gives the service behind the icon; it calls `on_change` from
    its own thread whenever there is something new to show. `root` is the folder of the
    program - what is updated and restarted - and `interpreter` the windowless Python.
    `make_panel(app, port)` gives the panel over that service: `start()`, which raises
    OSError when the port cannot be had, `stop()` and `link()`, the address a browser is
    let in by. Without it there is no panel. `retry(text)` tells the user that the panel
    could not be opened and says whether they want it tried again."""

    def __init__(self, make_app: Callable[[Callable[[], None]], core.App], root: Path, interpreter: str,
                 make_panel: Callable | None = None, retry: Callable[[str], bool] = retry_or_cancel) -> None:
        self._root, self._interpreter, self._make_panel, self._again = root, interpreter, make_panel, retry
        self._panel = None                          # the panel, while it is served
        self._taken: int | None = None              # the port it could not have, when that is why there is none
        self._opening = threading.Lock()            # one click at a time starts or opens the panel
        self._ready = False
        self._stopping = threading.Event()
        self._refresh_lock = threading.Lock()
        self._image: str | None = None
        self._menu_shown: tuple | None = None
        self._autostart = False
        self._update = updater.Update("off")        # until the first look at origin says there is one
        self._on_disk = VERSION                     # the version of the code on disk; differs after a merge or a pull
        self._code = updater.code_digest(root)      # the code this program started with...
        self._code_changed = False                  # ...and whether the code on disk is still that
        self._requirements = updater.requirements_digest(root)      # the file this program started with
        self._stale = updater.libraries_stale(root)                 # libraries older than requirements.txt
        self._checked: tuple[str, str] | None = None                # (time, result) of the last check by hand
        self._failure: Text | None = None           # why the last click on the update line changed nothing
        # Re-entrant: taking an update asks origin again from the same thread.
        self._update_lock = threading.RLock()
        settings = config.load().config
        i18n.set_language(settings.language)
        self._port = settings.panel.port            # taken once, like everything edited by hand; 0: not chosen yet
        self._app = make_app(self.refresh)
        glance = self._app.glance()
        self.icon = pystray.Icon("Protocallist", icons.image(picture(glance)), title(glance), menu=self._menu())

    # -- menu ---------------------------------------------------------------

    def _menu(self) -> pystray.Menu:
        # Every text is a function: it is put into words when the menu is drawn.
        item, menu = pystray.MenuItem, pystray.Menu
        return menu(
            # The row marked as the main one is what a plain click on the icon does.
            item(lambda i: _("Открыть панель"), _menu_action(self._open_panel), default=True,
                 visible=lambda i: self._make_panel is not None),
            item(lambda i: _("Записывать разговоры"), _menu_action(self._toggle_recording),
                 checked=lambda i: self._app.glance().enabled),
            item(lambda i: _("Остановить и удалить эту запись"), _menu_action(self._discard),
                 visible=lambda i: self._recording()),
            item(lambda i: _("Микрофон для разговоров"), menu(self._microphones)),
            item(lambda i: _("Открыть папку разговоров"), _menu_action(self._open_calls)),
            item(lambda i: _("Повторить распознавание"), _menu_action(self._retry),
                 visible=lambda i: self._app.glance().failed > 0),
            menu.SEPARATOR,
            item(lambda i: _("Открыть файл настроек"), _menu_action(self._open_settings)),
            item(lambda i: _("Открыть журнал"), _menu_action(self._open_logs)),
            item(lambda i: _("Запускать вместе с Windows"), _menu_action(self._toggle_autostart),
                 checked=lambda i: self._autostart),
            menu.SEPARATOR,
            # Greyed while a conversation is being recorded: a restart would cut it in two.
            item(lambda i: self._update_line() or "", _menu_action(self._apply_update),
                 visible=lambda i: self._update_line() is not None, enabled=lambda i: not self._recording()),
            item(lambda i: _("Обновления: {note}").format(note=self._note()), None, enabled=False,
                 visible=lambda i: self._note() is not None),
            item(lambda i: self._check_line(), _menu_action(self._check_update_now),
                 visible=lambda i: self._update.kind != "off"),
            item(lambda i: _("Выход"), _menu_action(self._exit)),
        )

    def _recording(self) -> bool:
        return self._app.glance().state == core.RECORDING

    def _microphones(self):
        """Submenu rows: the microphones of this computer, the chosen one marked. A chosen
        one that is unplugged stays in the list, so that the setting can be seen."""
        glance = self._app.glance()
        names = list(glance.microphones)
        if glance.microphone and glance.microphone not in names:
            names.insert(0, glance.microphone)
        if not names:
            yield pystray.MenuItem(_(NO_MICROPHONES), None, enabled=False)
        for name in names:
            yield pystray.MenuItem(plain(name), _menu_action(self._pick_microphone(name)),
                                   checked=lambda i, name=name: name == glance.microphone, radio=True)

    # -- actions ------------------------------------------------------------

    def _open_panel(self, icon, item) -> None:
        """The user wants the panel: that is all they need to know. One that is not served is
        started first; only when that cannot be done are they told - in a window, the answer to
        their own click - and asked whether to try again. The address carries the key and so is
        never logged."""
        if self._make_panel is None or not self._opening.acquire(blocking=False):
            return
        try:
            while not self._serve_panel():
                if not self._again(str(self._why())):
                    return
            webbrowser.open(self._panel.link())
        finally:
            self._opening.release()

    def _why(self) -> Text:
        """Why there is no panel, in words for the user."""
        where = str(paths.logs_dir())
        if self._taken is None:
            return Text("Панель не запустилась.\n\nПодробности — в журнале: {log}", log=where)
        program = ports.holder(self._taken)
        if not program:
            return Text("Панель не запустилась: порт {port} занят. Освободите его и нажмите «Повтор».\n\n"
                        "Подробности — в журнале: {log}", port=self._taken, log=where)
        return Text("Панель не запустилась: порт {port} занят программой {program}. Закройте её и нажмите «Повтор».\n\n"
                    "Подробности — в журнале: {log}", port=self._taken, program=program, log=where)

    def _toggle_recording(self, icon, item) -> None:
        self._app.set_enabled(not self._app.glance().enabled)

    def _discard(self, icon, item) -> None:
        self._app.discard_current()

    def _pick_microphone(self, name: str):
        def pick(icon, item) -> None:
            self._app.set_microphone(name)
        return pick

    def _open_calls(self, icon, item) -> None:
        os.startfile(paths.calls_dir())

    def _retry(self, icon, item) -> None:
        self._app.retry_failed()

    def _open_settings(self, icon, item) -> None:
        if not paths.config_file().exists():
            config.save(config.Config())
        subprocess.Popen(["notepad.exe", str(paths.config_file())])

    def _open_logs(self, icon, item) -> None:
        os.startfile(paths.logs_dir())

    def _toggle_autostart(self, icon, item) -> None:
        if autostart.enabled():
            autostart.disable()
        else:
            autostart.enable(self._root, self._interpreter)
        self.refresh()

    def _exit(self, icon, item) -> None:
        self.icon.stop()

    # -- updates ------------------------------------------------------------

    def _update_line(self) -> str | None:
        line = updater.menu_label(self._update, VERSION, self._on_disk, self._code_changed, self._stale)
        if line is None and self._failed():
            # The tooltip says "restart it": this is what to restart it with.
            return _("Перезапустить: программа остановилась")
        return line

    def _failed(self) -> bool:
        """The service behind the icon died: nothing is watched until the program is started anew."""
        glance = self._app.glance()
        return glance.state == core.ERROR and glance.problem == core.PROBLEM_FAILED

    def _note(self) -> Text | None:
        """The remark under the update line: why the last click changed nothing, or why this
        copy gets no updates."""
        return self._failure or self._update.note

    def _check_line(self) -> str:
        """What the last check BY HAND found, with its own time; the automatic ones are silent."""
        if self._checked is not None and self._update.kind != "behind":
            at, kind = self._checked
            if kind == "unknown":
                return _("Нет связи с origin — проверено в {time}").format(time=at)
            if kind == "none" and self._update.kind == "none":
                return _("Обновлений нет — проверено в {time}").format(time=at)
        return _(CHECK_UPDATES)

    def _check_update(self) -> str | None:
        """Ask origin (a network call, up to 15 s): never on a thread the icon needs.
        Returns what was found, or None when nothing was asked - an update is being taken
        right now (two git commands at once trip over each other's locks) or git blew up."""
        if not self._update_lock.acquire(blocking=False):
            return None
        try:
            try:
                found = updater.status(self._root)
            except Exception:
                log.exception("could not check for updates")
                return None
            self._failure = None    # a new look at origin: the old complaint is about another moment
            # An origin that did not answer says nothing: an update we already know about stays.
            if found.kind != "unknown" or self._update.kind == "off":
                if found.kind == "behind" and found != self._update:
                    log.info("update available: %s", found.version)
                self._update = found
        finally:
            self._update_lock.release()
        self.refresh()
        return found.kind

    def _check_update_now(self, icon, item) -> None:
        kind = self._check_update()
        if kind is not None:
            self._checked = (time.strftime("%H:%M"), kind)
            self.refresh()

    def _apply_update(self, icon, item) -> None:
        """A click on the update line: take the update from origin, or only give way to the
        code that is on disk already. The click is the consent - nothing is asked. Never
        while a conversation is being recorded."""
        if self._recording():
            return
        if not self._update_lock.acquire(blocking=False):
            return  # a second click while the first is still asking or pulling
        try:
            self._failure = None
            update = self._update
            libraries_changed = False
            if update.kind == "behind":
                try:
                    pulled = updater.pull(self._root, update)
                except updater.UpdateError as e:
                    reason = e.args[0] if e.args and isinstance(e.args[0], Text) else Text(str(e))
                    log.warning("update not taken: %s", i18n.english(reason))
                    self._check_update()    # the menu line was out of date: ask origin again
                    self._failure = reason  # ...and say in the menu why nothing happened
                    self.refresh()
                    return
                log.info("updated %s -> %s", pulled.old[:8], pulled.new[:8])
                libraries_changed = pulled.requirements_changed
                # It is on disk now: from here on the line says "restart", whatever becomes of the hand-over.
                self._update = updater.Update("none")
                self._look_at_disk()
            elif self._update_line() is None:
                return
            if self._recording():
                # Origin took its time and a conversation began meanwhile: the restart waits for a click after it.
                log.info("a conversation is being recorded: the restart waits")
                self.refresh()
                return
            # The libraries are behind requirements.txt when the record says so (a library was
            # added, an install failed) or the file differs from the one this program started with.
            if updater.libraries_stale(self._root) or updater.requirements_digest(self._root) != self._requirements:
                libraries_changed = True
            self._hand_over(libraries_changed or self._stale)
        finally:
            self._update_lock.release()

    def _hand_over(self, install_libraries: bool) -> None:
        """Give way to a new process that runs the code on disk. What cannot be undone
        comes last: a successor that cannot be started leaves this program working."""
        try:
            if install_libraries:
                # pip needs this program gone (it holds the files pip replaces) and a window
                # that shows what went wrong: the launcher window does all of that.
                relaunch.spawn_console(self._root)
            else:
                relaunch.spawn(self._root)
        except OSError:
            log.exception("could not start the new copy of the program")
            self._failure = Text("не удалось запустить новую копию — см. журнал")
            self.refresh()
            return
        log.info("handing over to %s", "the launcher window: the libraries changed" if install_libraries
                 else "a new process")
        self._stopping.set()
        self.icon.stop()        # run() ends the service: the successor waits for the lock to come free

    def _look_at_disk(self) -> None:
        """Is the code on disk still the code that runs, and the libraries those it asks for?"""
        self._on_disk = updater.disk_version(self._root) or VERSION
        code = updater.code_digest(self._root)
        self._code_changed = None not in (code, self._code) and code != self._code
        self._stale = updater.libraries_stale(self._root)

    def _watch(self) -> None:
        tick = 0
        while not self._stopping.wait(WATCH_S):
            tick += 1
            try:
                self._look_at_disk()
                if tick % UPDATE_EVERY == 0:
                    # In its own thread: origin may take 15 s to say nothing.
                    threading.Thread(target=self._check_update, name="update-check", daemon=True).start()
                self.refresh()
            except Exception:
                log.exception("could not look for updates")

    def _start_watching(self) -> None:
        threading.Thread(target=self._watch, name="updates", daemon=True).start()
        threading.Thread(target=self._check_update, name="update-check", daemon=True).start()

    # -- state --------------------------------------------------------------

    def refresh(self) -> None:
        """Repaint the icon and rebuild the menu if what they show has changed. Safe from
        any thread; nothing happens until the icon is up."""
        if not self._ready:
            return
        with self._refresh_lock:
            try:
                glance = self._app.glance()
                self._autostart = autostart.enabled()
                text = title(glance)
                if self.icon.title != text:
                    self.icon.title = text
                if picture(glance) != self._image:
                    self._image = picture(glance)
                    self.icon.icon = icons.image(self._image)
                # The menu lambdas read these same values; the native menu is rebuilt only when one changes.
                shown = (glance.state == core.RECORDING, glance.enabled, glance.microphone, glance.microphones,
                         glance.failed > 0,
                         self._autostart, self._update_line(), str(self._note() or ""), self._check_line(),
                         self._update.kind)
                if shown != self._menu_shown:
                    self._menu_shown = shown
                    self.icon.update_menu()
            except Exception:
                log.exception("could not refresh the tray icon")

    def _setup(self, icon) -> None:
        """Runs in pystray's setup thread once the icon exists."""
        icon.visible = True
        self._ready = True
        self._app.start()
        with self._opening:             # a click that comes meanwhile starts no second panel
            self._serve_panel()
        self._start_watching()
        self.refresh()

    def _serve_panel(self) -> bool:
        """Have the panel served; True if it is. The port is the one of the settings; while none is
        written there - at the first start - the ports of this Windows account are tried in turn,
        and the one that could be had is written into the settings. Recording and recognition go
        on without a panel as if there had never been one."""
        if self._panel is not None:
            return True
        if self._make_panel is None:
            return False
        self._taken = None
        try:
            for port in ([self._port] if self._port else ports.candidates()):
                panel = self._make_panel(self._app, port)
                try:
                    panel.start()
                except OSError as e:
                    self._taken = self._taken or port
                    log.warning("the panel could not be started on port %d (%s: %s); it is held by %s", port,
                                type(e).__name__, e, ports.holder(port) or "a program Windows does not name")
                    continue
                self._panel, self._taken = panel, None
                if not self._port:
                    self._keep(port)
                return True
        except Exception:
            log.exception("the panel could not be started")
        return False

    def _keep(self, port: int) -> None:
        """Write the port that was chosen into the settings: from now on it is this account's.
        Nothing else in the file is touched. If it cannot be written, the panel is served all the
        same, and the port is chosen again at the next start."""
        self._port = port
        try:
            written = config.put("panel", "port", port)
        except OSError as e:
            log.warning("the panel's port is %d; it could not be written into the settings (%s): it is chosen again "
                        "at the next start", port, type(e).__name__)
            return
        if written:
            log.info("the panel's port is %d: it is written into the settings", port)
        else:
            log.warning("the panel's port is %d; the settings cannot be read, so it is not written there", port)

    def _close_panel(self) -> None:
        panel, self._panel = self._panel, None
        if panel is not None:
            try:
                panel.stop()
            except Exception:
                log.exception("the panel could not be stopped cleanly")

    def run(self) -> None:
        try:
            self.icon.run(setup=self._setup)    # an exception from the icon loop goes up to main(), which logs it
        finally:
            self._ready = False
            self._stopping.set()
            self._close_panel()                 # before the service goes: the panel reads its archive
            self._app.stop()                    # a conversation that is going on ends here and is kept
