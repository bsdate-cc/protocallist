"""The summary: what is sent to Claude or to a command of the user's own, how, and what comes back. Claude
itself is a stand-in."""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from protocallist import summary
from protocallist.summary import SummaryError

TRANSCRIPT = "# Разговор 06.10.2026 15:55 — Discord, 74 мин\n\n**[00:00:05] Я:** начнём с плана\n"
ANSWER = "# Выжимка: 06.10.2026, Discord, 74 мин\n\n## Коротко\nГоворили о плане.\n"


class Claude:
    """Stands in for `claude -p`: remembers how it was run and answers what the test tells it to."""

    def __init__(self, answer=None, stdout: bytes | None = None, code: int = 0, error: Exception | None = None,
                 stderr: bytes = b"") -> None:
        self.answer = {"is_error": False, "subtype": "success", "result": ANSWER} if answer is None else answer
        self.stdout, self.code, self.error, self.stderr = stdout, code, error, stderr
        self.runs: list[SimpleNamespace] = []

    def __call__(self, command, **how):
        self.runs.append(SimpleNamespace(command=command, **how))
        if self.error is not None:
            raise self.error
        out = self.stdout if self.stdout is not None else json.dumps(self.answer).encode("utf-8")
        return SimpleNamespace(returncode=self.code, stdout=out, stderr=self.stderr)


@pytest.fixture
def folder(tmp_path):
    """Where the command is run; its name is how the conversation is called in the log."""
    made = tmp_path / "2026-10-06_15-55-33_Discord"
    made.mkdir()
    return made


def ask(folder, model="opus", language="ru", **how):
    """(the summary, who made it) of the conversation of these tests."""
    return summary.ask(TRANSCRIPT, model, language, where=folder, label=folder.name, **how)


def make(folder, claude, language="ru", model="opus"):
    return ask(folder, model, language, run=claude, find=lambda: "C:/bin/claude.exe")


def flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def instructions(command: list[str]) -> str:
    """The standing instructions: Claude is pointed at a file, and no key of the command is longer than a line."""
    return Path(flag(command, "--system-prompt-file")).read_text(encoding="utf-8")


def test_the_text_of_the_conversation_goes_to_claude_and_the_answer_comes_back_with_who_made_it(folder):
    claude = Claude()
    assert make(folder, claude) == (ANSWER, "opus")
    assert list(folder.iterdir()) == []                                  # nothing is written where the command runs
    run, = claude.runs
    assert run.input == TRANSCRIPT.encode("utf-8")                       # the text, and nothing but the text
    assert run.command[:2] == ["C:/bin/claude.exe", "-p"] and flag(run.command, "--model") == "opus"
    assert instructions(run.command).startswith("Ты составляешь выжимку")
    assert run.command[2] == "Составь выжимку этого разговора. Расшифровка идёт следом."


def test_claude_is_given_nothing_to_act_with_and_keeps_nothing(folder):
    claude = Claude()
    make(folder, claude)
    command = claude.runs[0].command
    assert flag(command, "--tools") == ""                                # it can read the text and answer; nothing else
    assert flag(command, "--setting-sources") == "project,local"         # none of the user's settings, hooks or plugins
    assert {"--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"} <= set(command)
    assert flag(command, "--output-format") == "json"
    run = claude.runs[0]
    assert run.cwd == str(folder) and run.capture_output is True and run.timeout == summary.TIMEOUT_S
    assert run.creationflags == subprocess.CREATE_NO_WINDOW              # no console flashes up under the tray


def test_the_instructions_follow_the_language_of_the_interface(folder):
    claude = Claude()
    make(folder, claude, language="en")
    make(folder, claude, language="auto")                                # anything unknown: English
    for run in claude.runs:
        assert instructions(run.command).startswith("You make a summary")
        assert run.command[2] == "Make a summary of this conversation. The transcript follows."


def test_both_sets_of_instructions_ask_for_the_same_six_sections():
    for language in ("ru", "en"):
        path, ask = summary.prompt(language)
        system = path.read_text(encoding="utf-8")
        assert system.count("\n## ") == 6 and system.count("\n# ") == 1 and "=====" not in system
        assert system.count("\n- [") == 5                                # every list section shows a point with its time
        assert ask and "\n" not in ask                                   # the request is one line


def test_the_model_comes_from_the_settings(folder):
    claude = Claude()
    make(folder, claude, model="sonnet")
    assert flag(claude.runs[0].command, "--model") == "sonnet"


def test_without_claude_on_this_computer_it_is_said_so(folder):
    with pytest.raises(SummaryError, match="не найден Claude Code"):
        ask(folder, "opus", "ru", run=Claude(), find=lambda: None)


def test_a_claude_that_does_not_answer_or_does_not_start_is_said_so(folder):
    with pytest.raises(SummaryError, match="не ответил за 15 мин"):
        make(folder, Claude(error=subprocess.TimeoutExpired("claude", 900)))
    with pytest.raises(SummaryError, match="не запустился: PermissionError"):
        make(folder, Claude(error=PermissionError(13, "denied")))


def test_an_answer_that_is_an_error_or_empty_or_unreadable_is_no_summary(folder, caplog):
    answers = (Claude(answer={"is_error": True, "subtype": "error_during_execution", "result": "limit reached"}),
               Claude(answer={"is_error": False, "result": "   "}), Claude(answer={"is_error": False}),
               Claude(stdout=b"not json", code=1), Claude(stdout=b"[1, 2]"))
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        for claude in answers:
            with pytest.raises(SummaryError, match="не дал выжимку"):
                make(folder, claude)
    assert "error_during_execution" in caplog.text and "exit code 1" in caplog.text


def test_nothing_of_the_conversation_or_of_the_summary_reaches_the_log(folder, caplog):
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        make(folder, Claude())
    assert "summary of 2026-10-06_15-55-33_Discord: 80 characters sent to opus" in caplog.text
    assert "план" not in caplog.text and "Коротко" not in caplog.text


def test_the_command_is_looked_for_where_programs_are_found(monkeypatch):
    there = {"claude.exe": "C:/native/claude.exe", "claude": "C:/npm/claude.CMD"}
    monkeypatch.setattr(summary.shutil, "which", there.get)
    assert summary.find() == "C:/native/claude.exe"        # the program, when both are there: no cmd.exe in between
    del there["claude.exe"]
    assert summary.find() == "C:/npm/claude.CMD"
    there.clear()
    assert summary.find() is None


# --- claude as npm installs it: a file that cmd.exe runs


def test_the_command_arrives_whole_when_claude_is_a_file_that_cmd_runs(folder, tmp_path):
    """cmd.exe cuts a command at its first new line - and the keys that leave Claude nothing to act with were
    among what it cut. A stand-in for claude.cmd says what reached it."""
    seen, echo, shim = tmp_path / "seen.json", tmp_path / "echo.py", tmp_path / "claude.cmd"
    echo.write_text("import json, sys\n"
                    "said = {'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8')}\n"
                    f"open({str(seen)!r}, 'w', encoding='utf-8').write(json.dumps(said))\n"
                    "print(json.dumps({'is_error': False, 'result': 'made'}))\n", encoding="utf-8")
    shim.write_text(f'@echo off\nchcp 65001 >nul\n"{sys.executable}" "{echo}" %*\n', encoding="utf-8")
    for language in ("ru", "en"):
        pretended = Claude()
        ask(folder, "opus", language, run=pretended, find=lambda: str(shim))
        assert ask(folder, "opus", language, find=lambda: str(shim)) == ("made\n", "opus")   # for real, through cmd.exe
        said = json.loads(seen.read_text(encoding="utf-8"))
        assert said["argv"] == pretended.runs[0].command[1:]
        assert said["stdin"] == TRANSCRIPT


def test_a_command_that_cmd_would_cut_short_is_not_run_at_all(folder, caplog):
    claude = Claude()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        with pytest.raises(SummaryError, match="cmd.exe не передаст команду выжимки"):
            ask(folder, "opus&calc", "ru", run=claude, find=lambda: "C:/npm/claude.CMD")
    assert claude.runs == [] and "--model" in caplog.text
    ask(folder, "opus&calc", "ru", run=claude, find=lambda: "C:/bin/claude.exe")
    assert flag(claude.runs[0].command, "--model") == "opus&calc"          # a program gets its keys as they are


# --- the way to the network


def settings_of_claude_code(tmp_path, monkeypatch, text: str | None) -> None:
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    if text is None:
        (config / "settings.json").unlink(missing_ok=True)
    else:
        (config / "settings.json").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))


def test_claude_is_run_with_what_its_own_settings_tell_it_about_the_network(folder, tmp_path, monkeypatch, caplog):
    """From the tray Claude answered 403: the proxy it reaches the network through is written in the settings of
    Claude Code, and the user's settings - hooks, plugins - are kept out of a summary."""
    settings_of_claude_code(tmp_path, monkeypatch, json.dumps({
        "env": {"HTTPS_PROXY": "http://user:secret@10.0.0.1:8118", "no_proxy": "localhost", "RETRIES": 3},
        "hooks": {"SessionStart": []}}))
    monkeypatch.setenv("HTTPS_PROXY", "http://the-one-of-the-environment")
    monkeypatch.setenv("NO_PROXY", "everything")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    claude = Claude()
    with caplog.at_level(logging.DEBUG, logger="protocallist"):
        make(folder, claude)
    env = claude.runs[0].env
    assert env["HTTPS_PROXY"] == "http://user:secret@10.0.0.1:8118" and env["RETRIES"] == "3"
    assert env["NO_PROXY"] == "localhost" and "no_proxy" not in env    # Windows does not tell the two names apart
    assert env["SOMETHING_ELSE"] == "kept" and env["PATH"] == os.environ["PATH"]
    assert "secret" not in caplog.text and "HTTPS_PROXY" in caplog.text    # the names are logged, never the values
    assert flag(claude.runs[0].command, "--setting-sources") == "project,local"


def test_without_settings_of_claude_code_the_environment_is_as_it_is(folder, tmp_path, monkeypatch):
    for text in (None, "not json", "[]", '{"env": "a proxy"}', "{}"):
        settings_of_claude_code(tmp_path, monkeypatch, text)
        claude = Claude()
        make(folder, claude)
        assert claude.runs[0].env == dict(os.environ), text


def test_an_error_of_the_service_is_said_with_its_number_and_its_words_go_to_the_log(folder, caplog):
    claude = Claude(code=1, answer={"is_error": True, "subtype": "success", "api_error_status": 403,
                                    "result": "Failed to authenticate. API Error: 403 Request not allowed"})
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        with pytest.raises(SummaryError, match="Claude ответил ошибкой 403"):
            make(folder, claude)
    assert "the service answered 403: Failed to authenticate. API Error: 403 Request not allowed" in caplog.text


# --- what a command said of its failure


def test_what_claude_said_of_its_failure_goes_to_the_log(folder, caplog):
    claude = Claude(stdout=b"", code=1, stderr=b"\nerror: unknown option '--tools'\n(run with --help)\n")
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        with pytest.raises(SummaryError, match="не дал выжимку"):
            make(folder, claude)
    assert "it said: error: unknown option '--tools'" in caplog.text and "--help" not in caplog.text


def test_a_line_that_repeats_the_conversation_is_not_written_to_the_log(folder, caplog):
    said = "request failed, the body was: " + TRANSCRIPT.splitlines()[2]
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        with pytest.raises(SummaryError):
            make(folder, Claude(stdout=b"", code=1, stderr=said.encode("utf-8")))
    assert "план" not in caplog.text and "request failed" not in caplog.text
    assert "repeats the conversation" in caplog.text
    caplog.clear()
    flattened = "cannot send: Discord,  74 мин\t**[00:00"                # the end of one line and the start of the next
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        with pytest.raises(SummaryError):
            make(folder, Claude(stdout=b"", code=1, stderr=flattened.encode("utf-8")))
    assert "74 мин" not in caplog.text and "repeats the conversation" in caplog.text


# --- a command of one's own


def test_a_command_of_ones_own_gets_the_whole_task_and_its_answer_is_the_summary(folder, tmp_path):
    """Run for real: a little script stands in for whatever the user would rather ask than Claude."""
    script, got = tmp_path / "my summary.py", tmp_path / "task.txt"
    script.write_text("import sys\n"
                      "task = sys.stdin.read()\n"
                      "open(sys.argv[1], 'w', encoding='utf-8').write(task)\n"
                      "print('# Выжимка своими силами')\n"
                      "print()\n"
                      "print('Говорили о плане.')\n", encoding="utf-8")
    out = ask(folder, "opus", "ru", command=[sys.executable, str(script), str(got)], find=lambda: None)
    assert out == ("# Выжимка своими силами\n\nГоворили о плане.\n", f"{Path(sys.executable).name} my summary.py task.txt")
    standing, request = summary.prompt("ru")
    assert got.read_text(encoding="utf-8") == standing.read_text(encoding="utf-8").strip() + "\n\n" + request + "\n\n" + TRANSCRIPT


def test_a_command_of_ones_own_is_run_as_it_is_written(folder, tmp_path, monkeypatch, caplog):
    settings_of_claude_code(tmp_path, monkeypatch, json.dumps({"env": {"ONLY_FOR_CLAUDE": "yes"}}))
    monkeypatch.setattr(summary.shutil, "which", {"my-tool": "C:/npm/my-tool.CMD"}.get)
    tool = Claude(stdout="# Выжимка\r\n\r\nКоротко.\r\n".encode("utf-8"))
    with caplog.at_level(logging.INFO, logger="protocallist"):
        out = ask(folder, "opus", "en", command=["my-tool", "--brief", "-"], run=tool, find=lambda: None)
    assert out == ("# Выжимка\n\nКоротко.\n", "my-tool --brief -")
    run, = tool.runs
    assert run.command == ["C:/npm/my-tool.CMD", "--brief", "-"]         # found where programs are found, nothing added
    assert run.input.decode("utf-8").startswith("You make a summary") and run.input.decode("utf-8").endswith(TRANSCRIPT)
    assert run.cwd == str(folder) and run.capture_output is True and run.timeout == summary.TIMEOUT_S
    assert run.creationflags == subprocess.CREATE_NO_WINDOW
    assert run.env == {**os.environ, "PYTHONIOENCODING": "utf-8"}       # the settings of Claude Code are for Claude
    assert "characters sent to my-tool --brief -" in caplog.text and "Коротко" not in caplog.text


def test_a_command_of_ones_own_that_fails_or_says_nothing_is_no_summary(folder, caplog):
    silent = Claude(stdout=b"  \r\n")
    failed = Claude(stdout="# Выжимка".encode("utf-8"), code=3, stderr=b"Traceback (most recent call last):\n")
    foreign = Claude(stdout="# Выжимка".encode("cp1251"))
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        for tool, code in ((silent, 0), (failed, 3), (foreign, 0)):
            with pytest.raises(SummaryError, match=rf"команда выжимки не дала текста \(код {code}\)"):
                ask(folder, "opus", "ru", command=["my-tool"], run=tool)
    assert "exit code 3" in caplog.text and "it said: Traceback (most recent call last):" in caplog.text
    assert "its answer is not UTF-8" in caplog.text


def test_a_command_of_ones_own_that_does_not_answer_or_does_not_start_is_said_so(folder):
    with pytest.raises(SummaryError, match="команда выжимки не ответила за 15 мин"):
        ask(folder, "opus", "ru", command=["my-tool"], run=Claude(error=subprocess.TimeoutExpired("my-tool", 900)))
    with pytest.raises(SummaryError, match="команда выжимки не запустилась: FileNotFoundError"):
        ask(folder, "opus", "ru", command=["my-tool"], run=Claude(error=FileNotFoundError(2, "not found")))


def test_a_command_of_ones_own_that_cmd_would_cut_short_is_not_run_at_all(folder, monkeypatch):
    monkeypatch.setattr(summary.shutil, "which", {"my-tool": "C:/npm/my-tool.cmd"}.get)
    tool = Claude()
    with pytest.raises(SummaryError, match="cmd.exe не передаст команду выжимки"):
        ask(folder, "opus", "ru", command=["my-tool", "--prompt", "a & b"], run=tool)
    assert tool.runs == []


def test_a_command_of_ones_own_is_named_by_its_words_without_their_folders():
    assert summary.name(["python", "C:\\scripts\\my_summary.py"]) == "python my_summary.py"
    assert summary.name(["C:/tools/codex.exe", "exec", "-"]) == "codex.exe exec -"
    assert len(summary.name(["tool"] + ["--a-rather-long-key"] * 9)) == 60


# --- instructions of the user's own


def test_instructions_of_the_users_own_stand_in_for_those_the_program_comes_with(folder, tmp_path):
    own = tmp_path / "мои указания.md"
    own.write_text("Пиши три строки, не больше.\n", encoding="utf-8")
    claude = Claude()
    assert ask(folder, instructions=own, run=claude, find=lambda: "C:/bin/claude.exe") == (ANSWER, "opus")
    run, = claude.runs
    assert flag(run.command, "--system-prompt-file") == str(own)        # the file itself: Claude reads it anew every time
    assert run.command[2] == "Составь выжимку этого разговора. Расшифровка идёт следом."     # the request is the same
    assert run.input == TRANSCRIPT.encode("utf-8")


def test_a_command_of_ones_own_gets_the_users_instructions_as_they_are_at_that_moment(folder, tmp_path):
    own = tmp_path / "own.md"
    own.write_bytes("\ufeffПиши три строки, не больше.\r\n\r\n".encode("utf-8"))       # as Notepad saves it
    tool = Claude(stdout="# Выжимка\n".encode("utf-8"))
    ask(folder, command=["my-tool"], instructions=own, run=tool)
    request = summary.prompt("ru")[1]
    assert tool.runs[0].input.decode("utf-8") == "Пиши три строки, не больше.\n\n" + request + "\n\n" + TRANSCRIPT
    own.write_text("Теперь по-другому.\n", encoding="utf-8")            # changed between two summaries
    ask(folder, command=["my-tool"], instructions=own, run=tool)
    assert tool.runs[1].input.decode("utf-8").startswith("Теперь по-другому.\n\n" + request)


def test_instructions_that_cannot_be_used_send_nothing_anywhere(folder, tmp_path, caplog):
    missing, empty, foreign = tmp_path / "no such.md", tmp_path / "empty.md", tmp_path / "ansi.md"
    empty.write_text(" \n\n", encoding="utf-8")
    foreign.write_bytes("Пиши коротко.".encode("cp1251"))
    claude = Claude()
    with caplog.at_level(logging.WARNING, logger="protocallist"):
        for own, why in ((missing, "файл указаний не читается: FileNotFoundError"), (empty, "файл указаний пуст"),
                         (foreign, "файл указаний не в UTF-8")):
            for how in (dict(find=lambda: "C:/bin/claude.exe"), dict(command=["my-tool"])):
                with pytest.raises(SummaryError, match=why):
                    ask(folder, instructions=own, run=claude, **how)
    assert claude.runs == []
    assert f"the instructions of the user's own ({missing}) cannot be used: FileNotFoundError" in caplog.text
