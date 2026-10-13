"""What the user reads in the tray - the tooltip of the icon and its menu - in Russian or English.

Russian is the source language: the code says `_("Выход")`, and the English text is looked
up here. A text without a translation is shown as it is - a forgotten entry is a Russian
line in an English menu, never a crash; a test checks that there is none.

Not translated on purpose: the launcher window and the logs (always English). Standard
library only: the launcher imports modules that import this one.
"""
from __future__ import annotations

import ctypes

LANGUAGES = ("auto", "ru", "en")
LANG_RUSSIAN = 0x19  # the primary language identifier of Windows for Russian

EN = {
    "Протоколист": "Protocallist",
    # the tooltip: what the service is doing now
    "запись выключена": "recording is off",
    "микрофон не выбран": "no microphone chosen",
    "микрофон не найден": "the microphone is not there",
    "ждёт разговора": "waiting for a conversation",
    "идёт запись: {program}, {minutes} мин": "recording: {program}, {minutes} min",
    "ошибка: {problem}": "error: {problem}",
    "база не открывается": "the database cannot be opened",
    "запись остановилась, см. журнал": "the recording stopped, see the log",
    "разговор не записан, см. журнал": "a conversation was not recorded, see the log",
    "наблюдение не работает, см. журнал": "the microphone is not being watched, see the log",
    "сбой программы, перезапустите её": "the program failed, restart it",
    "настройки не читаются": "the settings cannot be read",
    # the tooltip: recognition
    "распознаётся: 1, ждут: {waiting}": "recognising: 1, waiting: {waiting}",
    "ждут распознавания: {waiting}": "waiting to be recognised: {waiting}",
    "ждёт шлюз: {waiting}": "waiting for the gateway: {waiting}",
    "нужен Толмач {version} или новее, ждут: {waiting}": "Tolmach {version} or newer is needed, waiting: {waiting}",
    "не распознано: {failed}": "not recognised: {failed}",
    # the menu
    "Повторить распознавание": "Retry recognition",
    "Открыть панель": "Open the panel",
    "Панель не запустилась.\n\nПодробности — в журнале: {log}": "The panel did not start.\n\nSee the log for details: {log}",
    "Панель не запустилась: порт {port} занят. Освободите его и нажмите «Повтор».\n\nПодробности — в журнале: {log}":
        "The panel did not start: port {port} is taken. Free it and press Retry.\n\nSee the log for details: {log}",
    "Панель не запустилась: порт {port} занят программой {program}. Закройте её и нажмите «Повтор».\n\nПодробности — в журнале: {log}":
        "The panel did not start: port {port} is taken by {program}. Close it and press Retry.\n\nSee the log for details: {log}",
    "Записывать разговоры": "Record conversations",
    "Остановить и удалить эту запись": "Stop and delete this recording",
    "Микрофон для разговоров": "Microphone for conversations",
    "микрофонов нет": "no microphones",
    "Открыть папку разговоров": "Open the conversations folder",
    "Открыть файл настроек": "Open the settings file",
    "Открыть журнал": "Open the log",
    "Запускать вместе с Windows": "Start with Windows",
    "Выход": "Exit",
    # updates: the one menu line, the remark under it, the check by hand
    "Обновить до {version}": "Update to {version}",
    "Обновить": "Update",
    "Перезапустить: на диске версия {version}": "Restart: version {version} is on disk",
    "Перезапустить: код на диске изменился": "Restart: the code on disk has changed",
    "Перезапустить: программа остановилась": "Restart: the program has stopped",
    "Поставить библиотеки: requirements.txt изменился": "Install the libraries: requirements.txt has changed",
    "Обновления: {note}": "Updates: {note}",
    "Проверить обновления": "Check for updates",
    "Нет связи с origin — проверено в {time}": "No answer from origin — checked at {time}",
    "Обновлений нет — проверено в {time}": "No updates — checked at {time}",
    "ветка {branch} — обновления выключены": "branch {branch} — updates are off",
    "без ветки — обновления выключены": "no branch — updates are off",
    "есть свои коммиты — обновления выключены": "local commits — updates are off",
    "история разошлась с origin — обновления выключены": "history diverged from origin — updates are off",
    "git не отвечает — обновление не выполнено": "git does not answer — the update was not taken",
    "в папке программы есть изменённые файлы — обновление не выполнено":
        "there are modified files in the program folder — the update was not taken",
    "нет связи с origin — обновление не выполнено": "no answer from origin — the update was not taken",
    "обновлять нечего: установлена последняя версия": "nothing to update: the latest version is installed",
    "на origin уже другое обновление (версия {version}) — нажмите «Обновить» ещё раз":
        "origin has another update by now (version {version}) — click Update again",
    "git merge не выполнен: {reason}": "git merge failed: {reason}",
    "неизвестная ошибка": "unknown error",
    "не удалось запустить новую копию — см. журнал": "the new copy could not be started — see the log",
    # the text of a conversation for reading
    "# Разговор {date} — {program}, {minutes} мин": "# Conversation {date} — {program}, {minutes} min",
    "Я": "Me",
    "Собеседники": "Others",
    "текст": "text",
    "выжимка": "summary",
    # the summary: why one was not made
    "у разговора нет текста": "the conversation has no text",
    "не найден Claude Code (команда claude)": "Claude Code was not found (the claude command)",
    "Claude не ответил за {minutes} мин": "Claude did not answer in {minutes} min",
    "Claude не запустился: {reason}": "Claude could not be started: {reason}",
    "Claude не дал выжимку — см. журнал": "Claude gave no summary — see the log",
    "Claude ответил ошибкой {status} — см. журнал": "Claude answered with error {status} — see the log",
    "cmd.exe не передаст команду выжимки как есть — см. журнал":
        "cmd.exe would not pass the summary command on as it is — see the log",
    "команда выжимки не запустилась: {reason}": "the summary command could not be started: {reason}",
    "выжимка этого разговора уже делается": "the summary of this conversation is being made already",
    "очередь выжимок остановлена": "the line of summaries stands still",
    "очередь выжимок не остановлена": "the line of summaries does not stand still",
    "файл указаний не читается: {reason}": "the file of instructions cannot be read: {reason}",
    "файл указаний не в UTF-8": "the file of instructions is not UTF-8",
    "файл указаний пуст": "the file of instructions is empty",
    "архив не открыт": "the archive is not open",
    "выжимка не получилась — см. журнал": "the summary was not made — see the log",
    # the panel: to one who came without the key, and why what the page asked for was not done
    "внутренняя ошибка — см. журнал": "an internal error — see the log",
    "Откройте панель из значка Протоколиста: пункт «Открыть панель».":
        "Open the panel from the icon of Protocallist: \"Open the panel\".",
    "такой записи нет": "there is no such conversation",
    "звука этой записи нет": "this conversation has no sound",
    "такой выжимки нет": "there is no such summary",
    "этот разговор не ждёт повтора": "this conversation does not wait to be tried again",
    "файлы записи сейчас заняты — закройте то, что их держит, и повторите":
        "the files of the conversation are in use — close what holds them and try again",
    "эта запись уже закончилась": "that recording is over already",
    "этот разговор сейчас записывается": "this conversation is being recorded now",
    "этот разговор сейчас распознаётся": "this conversation is being recognised now",
    # the panel: recognising a conversation anew, with another model
    "этот разговор ещё не распознан": "this conversation is not recognised yet",
    "этот разговор уже распознаётся заново": "this conversation is being recognised anew already",
    "Толмач не отвечает или в нём нет выбора моделей": "Tolmach does not answer, or has no choice of models",
    "такой модели в Толмаче нет": "Tolmach has no such model",
    "модель не установлена в Толмаче": "the model is not installed in Tolmach",
    "в этом Толмаче нет выбора моделей — нужен Толмач новее": "this Tolmach has no choice of models — a newer Tolmach is needed",
    "Толмач не смог загрузить модель — подробности в его журнале": "Tolmach could not load the model — see its log for details",
    "модель не услышала ни слова — прежний текст оставлен": "the model heard no words — the text of before is kept",
    "Толмач не ответил за {minutes} мин": "Tolmach did not answer in {minutes} min",
    "такой модели у сервиса распознавания нет": "the recognition service has no such model",
    "сервис распознавания не говорит, какие у него модели": "the recognition service does not say which models it has",
    "сервис распознавания не смог загрузить модель": "the recognition service could not load the model",
    "сервис распознавания не ответил за {minutes} мин": "the recognition service did not answer in {minutes} min",
    "сервис распознавания не отвечает или не говорит, какие у него модели":
        "the recognition service does not answer, or does not say which models it has",
    "звук этой записи не читается": "the sound of this conversation cannot be read",
    "этот разговор не ждёт распознавания заново": "this conversation does not wait to be recognised anew",
    "выжимка не сохранена: {reason}": "the summary was not kept: {reason}",
    "выжимка сохранена, но файл рядом со звуком не записан: {reason}":
        "the summary is kept, but the file beside the sound was not written: {reason}",
    "команда выжимки не ответила за {minutes} мин": "the summary command did not answer in {minutes} min",
    "команда выжимки не дала текста (код {code}) — см. журнал":
        "the summary command gave no text (code {code}) — see the log",
}

_language = "ru"


def pick_language(ui: int, regional: int) -> str:
    """'ru' when Windows itself or its regional format is Russian, else 'en'. The regional
    format counts because a Russian speaker on an English Windows is common among the
    people who run a program with a Russian name."""
    return "ru" if LANG_RUSSIAN in (ui & 0x3FF, regional & 0x3FF) else "en"


def system_language() -> str:
    try:
        kernel32 = ctypes.windll.kernel32
        return pick_language(kernel32.GetUserDefaultUILanguage(), kernel32.GetUserDefaultLCID())
    except Exception:
        return "en"


def set_language(setting: str) -> None:
    """'ru' or 'en'; anything else ('auto') follows Windows (see pick_language)."""
    global _language
    _language = setting if setting in ("ru", "en") else system_language()


def language() -> str:
    return _language


def _(text: str) -> str:
    """The text in the current language."""
    return EN.get(text, text) if _language == "en" else text


def english(message) -> str:
    """A message in English whatever the language is - for the log, which is always English."""
    if isinstance(message, Text):
        return EN.get(message.template, message.template).format(**message.values)
    return EN.get(str(message), str(message))


def N_(text: str) -> str:
    """Marks a text for translation where it is defined; it is translated where it is shown."""
    return text


class Text:
    """A message with values in it, put into words when it is shown - so it follows the
    language of that moment, not of the moment it was made."""

    def __init__(self, template: str, **values):
        self.template = template
        self.values = values

    def __str__(self) -> str:
        return _(self.template).format(**self.values)

    def __repr__(self) -> str:
        return f"Text({self.template!r}, {self.values!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Text) and (self.template, self.values) == (other.template, other.values)

    def __hash__(self) -> int:
        return hash((self.template, tuple(sorted(self.values.items()))))
