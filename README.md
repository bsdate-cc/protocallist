# Protocallist

A recorder of your calls for Windows. When a program on your computer takes the microphone of your
headset - Zoom, Telegram, a browser, a corporate messenger - Protocallist records the conversation:
your voice and what you hear, as two separate sides. After the call it turns the recording into text
with [Tolmach](https://github.com/bsdate-cc/tolmach), a speech recognition service that runs on your
own computer, and keeps the text, the sound and - if you ask for one - a summary in an archive. The
archive opens in the browser: the conversations can be read, listened to and searched.

What is said stays on your computer unless you ask for a summary: see
[What leaves the computer](#what-leaves-the-computer).

*The name is the Russian word "протоколист" - the one who keeps the minutes of a meeting - spelled so
that it also reads "call list".* Out of the box the text is **Russian** (Tolmach's main model); a call
in **English** can be recognised anew with Tolmach's English model. The tray menu and the panel are
available in Russian and English.

**[Читать по-русски](README.ru.md)**

## Before you record anybody

A recording holds the voices and the words of other people. Whether you have to tell them, have to
ask them, or may not record at all depends on the law where you and they live and on the rules of
your employer. The program does not decide this for you and warns nobody: the recording is yours,
and so is the responsibility. Recording is **off** until you switch it on yourself.

## What it does

- **Notices a call by itself.** A conversation begins when a program takes the microphone you chose,
  and is over five seconds after the microphone is free again. There is no button to press and
  nothing appears on the screen - a notification would show up on a screen you are sharing.
- **Records the two sides apart:** the microphone, and what plays in the earphones of the same
  headset. In the archive the sound is one small stereo file (Opus, about 26 MB an hour): you on
  the left, the others on the right.
- **Turns the call into text** when it is over, through Tolmach on the same computer or through a
  server of your own: who said what and when, "Me" and "Others". Nothing is recognised while a call
  is being recorded.
- **A panel in the browser:** the list of the conversations with a search in what was said, and a
  page for each of them - the text, a player for both sides or for one of them, the summary.
- **A summary on a click:** the text of that one conversation is given to
  [Claude Code](https://claude.com/claude-code) - or to a command of your own - and the summary is
  kept with the conversation. Several conversations can be ticked and summarised one after another.
- **Another model for another language:** while its sound is kept, a conversation can be recognised
  anew with any model your Tolmach has - the English one, for instance.
- **Old sound goes, the text stays:** the sound is kept for 30 days (a setting); the text and the
  summaries are kept for good.
- **A tray icon** shows the state: not recording, waiting for a conversation, recording, turning a
  conversation into text, an error.
- **Updates** through git: when the repository has a newer version, the menu offers
  "Update to X.Y.Z". Nothing installs by itself.

## How it works

One process without a window holds the tray icon, watches the microphone, records, sends the
recordings to be recognised and serves the panel.

Once a second it asks Windows which programs hold the microphone you chose. The programs listed in
`recording.ignore` do not count: by default these are Python (dictation programs such as Tolmach run
under it) and the sound settings of Windows. The two sides are written to disk as the sound comes,
so a recording cut short - the program was ended, the power went - is put in order at the next start.

When the call is over, each side is cut at quiet places into pieces of at most ten minutes, and the
pieces go to the gateway of Tolmach on `127.0.0.1`. The phrases that come back, each with its time,
are put in order on the one clock of the conversation and kept in a SQLite database; the two
recordings then become one Opus file. While Tolmach is not running the conversation waits, and is
recognised when Tolmach is there.

The panel is a page that the program itself serves on `127.0.0.1`, on a port that belongs to your
Windows account (40701 for the first account of a computer). It is opened from the tray icon: the
link carries a key kept in your user folder, and without the key the server gives nothing away.
Requests made by other sites, or from other ports of the computer, are refused. The page loads
nothing from the network.

## What leaves the computer

- **Of your conversations, by itself, nothing.** Recording, recognition and the archive all stay on
  this computer. The log holds the names of programs, times and errors - never what was said, never
  the title of a window.
- **To a recognition server of your own, if you set one, the sound of every conversation.** It goes
  to the address in `gateway.url` by itself, as soon as a call is over. Over `http://` neither the
  sound nor the key is hidden from whoever stands on the way: keep such a server on this computer or
  in a network that is yours.
- **A summary, on your click, for that one conversation.** Its text goes to Claude Code - the
  `claude` command that you installed and signed in to - which sends it to Anthropic; or to the
  command you set in `summary.command`. Claude is run without tools and keeps no session.
- **A check for updates** asks GitHub for the state of this repository: when the program starts,
  every six hours, and when you choose the row in the menu. Nothing of yours is in the question.

## Requirements

- Windows 10 or 11, 64-bit.
- [Python 3.12](https://www.python.org/downloads/) (64-bit) and [git](https://git-scm.com/).
- A headset. With a microphone that is not part of a headset - a webcam, a microphone on the desk -
  set `recording.speaker` to the name of the device you hear the call through, or only your side is
  recorded.
- [Tolmach](https://github.com/bsdate-cc/tolmach) 0.8.0 or newer, running on the same computer -
  for the text; or a server of your own that takes the same request, see "A recognition service of
  your own" below. Without either the calls are recorded and wait: the icon says how many, and
  until they are recognised they are plain sound, about 230 MB an hour, that nothing deletes. To
  recognise a conversation anew with another model, Tolmach 0.10.0 or newer is needed.
- [Claude Code](https://claude.com/claude-code) - for summaries only, and only if you do not set a
  command of your own.

## Installation

```
git clone https://github.com/bsdate-cc/protocallist.git
cd protocallist
.\protocallist.cmd
```

`protocallist.cmd` (a double click on the file works too) creates the `.venv` environment and checks
Python, the libraries, the data folder and the settings; missing libraries it offers to install
with pip. Then it starts the program without a window, and the icon appears in the tray - Windows
may keep it among the hidden icons, behind the arrow beside the clock. Every step is printed in the
window and written to `logs\startup.log`.

After the first start, in the menu of the icon:

1. **Microphone for conversations** - choose the microphone of your headset.
2. **Record conversations** - switch it on.

From then on a call is recorded by itself.

## Usage

The menu of the tray icon:

| Row | What it does |
|---|---|
| **Open the panel** | opens the archive in the browser; a plain click on the icon does the same |
| **Record conversations** | switches recording on and off |
| **Stop and delete this recording** | shown while a conversation is being recorded - for a call that should not have been |
| **Microphone for conversations** | the microphones of this computer; the chosen one is marked |
| **Open the conversations folder** | the folder with the sound files |
| **Retry recognition** | shown when a conversation could not be turned into text |
| **Open the settings file** | `config.json` in Notepad |
| **Open the log** | the folder with the logs |
| **Start with Windows** | starts the program when you log in |
| **Check for updates** | asks the repository now |
| **Exit** | ends the program; a conversation that is being recorded ends there and is kept |

When the code on disk has changed - after an update, or after a `git pull` made by hand - the menu
shows a row that restarts the program. It is greyed while a conversation is being recorded.

**The panel.** The list shows the time of each conversation, the program, what it was about (the
beginning of the summary or, while there is none, of the conversation itself), its length, its
state, whether there is a summary and how long its sound will be kept. Above the list: a search in
what was said, a period, and "Hide shorter than" - ticked at first, with 30 seconds, so a short
trial call is not in the list until the tick is taken off. A click on a conversation opens its page:

- the text, remark by remark, with the times; a search in it; "save as a file";
- the player: both sides, only you, or only the others;
- the summary: "Make a summary", and "make again" for another try;
- "recognise anew…" with another model of Tolmach, while the sound is kept - the text is replaced
  when the new one is ready;
- "Delete the conversation" - the sound, the text and the summary, for good.

**Summaries.** Out of the box a summary is made by Claude Code with the model set in
`summary.model`. What it should say, and in which form, is written in a file that comes with the
program; `summary.prompt` sets a file of your own to use in its place. `summary.command` sets a
command of your own in place of Claude Code: the whole task comes on its input - the instructions,
then the text - and what it prints is the summary, both in UTF-8:

```
{
  "summary": {"command": ["python", "C:\\tools\\my_summary.py"]}
}
```

Conversations ticked in the list are summarised one after another; the line stops at the first one
that could not be made, and says why.

**A recognition service of your own.** Tolmach is what the program asks out of the box, and it need
not be Tolmach. `gateway.url` sets the address of another server - on this computer or in your own
network - and the program then sends it the same request:

- `POST <gateway.url>/v1/audio/transcriptions`, a form with `file` - one side of the conversation as
  WAV, 16 kHz, mono, ten minutes at most - with `response_format` set to `verbose_json` and, if
  `gateway.model` is set, with `model`;
- the answer is JSON with `segments`: the phrases, each with its `start` and `end` in seconds from
  the beginning of the file and its `text`.

That is the request of the OpenAI transcription API, so a server that speaks it will do. If
`gateway.key_file` names a file, what is in it goes along as `Authorization: Bearer`. If the server
answers `GET <gateway.url>/v1/models` with the list of its models, "recognise anew…" offers them.
The sound goes straight to that address, never through a proxy, and is never sent on to another
one: a server that answers with a redirect gets nothing more.

Before the pieces of a conversation go out, the program asks `GET <gateway.url>`. No answer at all
means the server is not there: the conversation waits, and why is written in `logs\protocallist.log`.
An error in answer to a piece means the conversation is tried three times and then marked as not
recognised, with the reason on its page. Write the address without `/v1` at its end; an `https`
address needs a certificate that Windows trusts. The program was tried with Tolmach only - with a
server of your own, begin with a short call.

```
{
  "gateway": {"url": "http://127.0.0.1:8000", "model": "large"}
}
```

**From the command line.** `.venv\Scripts\python -m protocallist --microphones` prints the names of
the microphones as Windows gives them. `--console` runs the recorder alone in a console window - no
icon, no recognition, no panel - and needs `recording.enabled` and `recording.microphone` set in the
settings file.

## Settings

`%USERPROFILE%\.protocallist\config.json` (the menu row "Open the settings file"). The file is read
when the program starts; what the menu changes is written to it at once.

| Key | Default | What it does |
|---|---|---|
| `language` | `"auto"` | the language of the menu and of the panel: `"ru"`, `"en"`, or `"auto"` - Russian when Windows or its regional format is Russian, English otherwise |
| `recording.enabled` | `false` | record conversations; the menu row "Record conversations" |
| `recording.microphone` | `""` | the microphone a conversation is noticed by - a program has taken it - as Windows names it; chosen in the menu |
| `recording.speaker` | `""` | the playback device that "what is heard" is recorded from; empty - the one of the same headset as the microphone |
| `recording.ignore` | `["python.exe", "pythonw.exe", "SystemSettings.exe", "rundll32.exe"]` | programs whose use of the microphone is not a conversation, by file name |
| `recording.min_seconds` | `5` | a recording shorter than this is thrown away: a program touched the microphone and nobody spoke |
| `recording.linger_seconds` | `5` | how long the microphone may stay free before the conversation is over |
| `recording.poll_seconds` | `1` | how often Windows is asked who holds the microphone; 0.2 to 10 |
| `storage.keep_audio_days` | `30` | days the sound of a recognised conversation is kept; 0 - it is not kept, below zero - for ever |
| `gateway.url` | `""` | the address of a recognition server of your own; empty - the Tolmach of this computer, found by itself |
| `gateway.key_file` | `""` | the file with the key the server is asked with; empty - the key of Tolmach, and for a server of your own no key |
| `gateway.model` | `""` | the model a server of your own is asked to read with; empty - none is named |
| `summary.model` | `"opus"` | the model Claude Code is asked for: an alias such as "opus" or "sonnet", or a full name |
| `summary.command` | `[]` | a command of your own that makes the summaries, as a list - the program, then its arguments - in place of Claude Code |
| `summary.prompt` | `""` | a file with your own instructions for the summary; a name without a folder is looked for beside the settings file |
| `panel.port` | `0` | the port of the panel; 0 - the program chooses the port of your Windows account at its first start and writes it here |

A setting with a mistake in it falls back to its default, and the window of `protocallist.cmd` says
which one. One setting does not: with a mistake in `storage.keep_audio_days` no sound is deleted.
A file that is not JSON at all is not used: nothing is recorded until it is put right, and the icon
says that the settings cannot be read.

## Where things are

- `%USERPROFILE%\.protocallist\` holds the settings, the archive - the database `protocallist.db`
  and the folder `calls` with the sound of each conversation and a copy of its summary beside it -
  the key of the panel (`panel.key`) and the logs (`logs\protocallist.log`, `logs\startup.log`).
  The `PROTOCALLIST_HOME` variable points it elsewhere.
- What was said is never written to the logs.
- To remove the program: switch off "Start with Windows", choose "Exit", then delete the folder of
  the program and, if the archive is not wanted either, `%USERPROFILE%\.protocallist\`.

## Updating

The program compares itself with the repository when it starts, every six hours, and on "Check for
updates". When there is a newer version, the menu shows "Update to X.Y.Z": on a click the code is
updated (git, fast-forward only) and the program restarts - not while a conversation is being
recorded. An update runs code from the repository with your rights, which is why it is only ever
installed on a click. A copy with commits of its own is never offered updates; a copy with edited
files is offered them and is not updated - the menu says why.

Version history: [CHANGELOG.md](CHANGELOG.md) (in Russian).

## Development

```
.venv\Scripts\python -m pytest -q
```

The tests record nothing and need neither a microphone nor Tolmach. Those that read the page of the
panel run it in Edge or Chrome without a window, and are skipped when neither is installed.

## Licence and credits

The code is under the [MIT licence](LICENSE).

- [Tolmach](https://github.com/bsdate-cc/tolmach) turns the sound into text, with
  [GigaAM](https://github.com/salute-developers/GigaAM) by SberDevices as its Russian model.
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) records the microphone and what is heard,
  [pycaw](https://github.com/AndreMiras/pycaw) tells who holds the microphone,
  [soundfile](https://github.com/bastibe/python-soundfile) writes Opus,
  [pystray](https://github.com/moses-palmer/pystray) and [Pillow](https://github.com/python-pillow/Pillow)
  draw the tray icon.
