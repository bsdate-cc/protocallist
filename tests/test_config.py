import json
import logging

from protocallist import APP_NAME, VERSION, config, logsetup, paths


def write(text: str) -> None:
    paths.config_file().write_text(text, encoding="utf-8")


def test_the_data_folder_is_the_one_of_the_test(home, tmp_path):
    assert home == tmp_path / "home"
    assert paths.config_file() == home / "config.json"
    assert paths.database_file() == home / "protocallist.db"
    assert paths.calls_dir() == home / "calls" and paths.calls_dir().is_dir()
    assert paths.logs_dir().is_dir()


def test_the_log_goes_to_a_file_in_the_data_folder():
    log = logsetup.setup()
    log.info("%s %s is here", APP_NAME, VERSION)
    for handler in log.handlers:
        handler.flush()
    assert "Protocallist 0.1.0 is here" in (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")
    assert logsetup.setup() is log and len(log.handlers) == 1      # asked twice, one file still


def test_no_file_means_the_defaults_and_recording_is_off():
    loaded = config.load()
    assert loaded.problems == []
    cfg = loaded.config
    assert cfg.recording.enabled is False and cfg.recording.microphone == ""
    assert cfg.recording.min_seconds == 5 and cfg.recording.linger_seconds == 5
    assert "pythonw.exe" in cfg.recording.ignore
    assert cfg.storage.keep_audio_days == 30
    assert cfg.language == "auto"


def test_what_the_file_says_is_taken():
    write(json.dumps({"language": "en", "recording": {"enabled": True, "microphone": "Headset Microphone",
                                                       "ignore": ["notes.exe"], "min_seconds": 10},
                      "storage": {"keep_audio_days": -1}}))
    loaded = config.load()
    assert loaded.problems == []
    cfg = loaded.config
    assert (cfg.language, cfg.recording.enabled, cfg.recording.microphone) == ("en", True, "Headset Microphone")
    assert cfg.recording.ignore == ["notes.exe"] and cfg.recording.min_seconds == 10.0
    assert cfg.recording.linger_seconds == 5                       # what the file does not name keeps its default
    assert cfg.storage.keep_audio_days == -1


def test_a_wrong_value_keeps_the_default_and_says_where():
    write(json.dumps({"language": "de", "recording": {"enabled": "yes", "ignore": ["a.exe", 5], "poll_seconds": 0,
                                                       "surprise": 1}}))
    loaded = config.load()
    assert {p.where for p in loaded.problems} == {"language", "recording.enabled", "recording.ignore",
                                                  "recording.poll_seconds", "recording.surprise"}
    cfg = loaded.config
    assert cfg.language == "auto" and cfg.recording.enabled is False and cfg.recording.poll_seconds == 1.0
    assert "pythonw.exe" in cfg.recording.ignore


def test_a_file_that_is_not_json_does_not_stop_the_service():
    write("{ not json")
    loaded = config.load()
    assert config.unparsed(loaded)
    assert loaded.config == config.Config()


def test_a_file_saved_by_notepad_with_a_byte_order_mark_is_read():
    paths.config_file().write_text(json.dumps({"language": "ru"}), encoding="utf-8-sig")
    assert config.load().config.language == "ru"


def test_saving_and_loading_give_the_same_settings():
    cfg = config.Config()
    cfg.recording.microphone = "Микрофон гарнитуры"
    cfg.recording.enabled = True
    config.save(cfg)
    assert config.load().config == cfg
    assert "Микрофон гарнитуры" in paths.config_file().read_text(encoding="utf-8")   # readable in Notepad
    assert not paths.config_file().with_name("config.json.tmp").exists()


def test_the_log_is_named_protocallist():
    assert logsetup.setup().name == "protocallist" == logging.getLogger("protocallist").name


def test_what_the_tray_library_logs_is_kept_in_the_same_file():
    # Under a windowless Python a line logged by the library that draws the icon went nowhere.
    logsetup.setup()
    logging.getLogger("pystray._win32").error("An error occurred in the main loop")
    logsetup.setup()                                           # asked twice, written once
    said = (paths.logs_dir() / "protocallist.log").read_text(encoding="utf-8")
    assert said.count("pystray._win32: An error occurred in the main loop") == 1


def test_the_model_for_summaries_is_a_setting():
    assert config.load().config.summary.model == "opus"
    paths.config_file().write_text(json.dumps({"summary": {"model": "claude-sonnet-5-5"}}), encoding="utf-8")
    loaded = config.load()
    assert loaded.problems == [] and loaded.config.summary.model == "claude-sonnet-5-5"
    paths.config_file().write_text(json.dumps({"summary": {"model": 5}}), encoding="utf-8")
    wrong = config.load()
    assert wrong.config.summary.model == "opus" and wrong.problems[0].where == "summary.model"
    config.save(config.Config())
    assert json.loads(paths.config_file().read_text(encoding="utf-8"))["summary"] == {"model": "opus", "command": [], "prompt": ""}


def test_one_value_is_written_into_the_file_and_everything_else_there_stays_as_it_was():
    odd = {"storage": {"keep_audio_days": "7"}, "recording": {"min_seconds": "3"}, "note": "mine"}
    paths.config_file().write_text(json.dumps(odd), encoding="utf-8")
    assert config.put("panel", "port", 40701) is True
    assert json.loads(paths.config_file().read_text(encoding="utf-8")) == {**odd, "panel": {"port": 40701}}
    loaded = config.load()
    assert loaded.config.panel.port == 40701 and {p.where for p in loaded.problems} >= {"storage.keep_audio_days", "recording.min_seconds"}
    for broken in ('{"recording": {', "[1]", '{"panel": 5}'):          # what is wrong with it is still said at every start
        paths.config_file().write_text(broken, encoding="utf-8")
        assert config.put("panel", "port", 40701) is False and paths.config_file().read_text(encoding="utf-8") == broken
    paths.config_file().unlink()
    assert config.put("panel", "port", 40701) is True                   # no file yet: one is made, whole
    assert config.load().problems == [] and config.load().config.panel.port == 40701
    assert json.loads(paths.config_file().read_text(encoding="utf-8"))["recording"]["min_seconds"] == config.Config().recording.min_seconds


def test_instructions_of_ones_own_for_summaries_are_a_setting():
    assert config.load().config.summary.prompt == ""                    # none: the instructions the program comes with
    paths.config_file().write_text(json.dumps({"summary": {"prompt": "мои указания.md"}}), encoding="utf-8")
    loaded = config.load()
    assert loaded.problems == [] and loaded.config.summary.prompt == "мои указания.md"
    paths.config_file().write_text('{"summary": {"prompt": ["a.md"]}}', encoding="utf-8")
    loaded = config.load()
    assert loaded.config.summary.prompt == "" and loaded.problems[0].where == "summary.prompt"


def test_days_of_sound_that_cannot_be_read_delete_nothing():
    """Thirty days is a rule to delete by. It is the default of someone who wrote no rule - not of someone who
    wrote one that could not be read."""
    for wrong in ('{"keep_audio_days": "never"}', '{"keep_audio_days": null}', '{"keep_audio_days": 7.5}',
                  '{"keep_audio_days": true}', '{"keep_days": -1}', '"for ever"'):
        write('{"storage": %s}' % wrong)
        loaded = config.load()
        assert loaded.config.storage.keep_audio_days < 0, wrong
        assert loaded.problems[-1].where == "storage" and "no sound is deleted" in loaded.problems[-1].message
        assert len(loaded.problems) == 2 and not config.unparsed(loaded)
    write('{"storage": {"keep_audio_days": 7}, "summary": {"model": 5}, "storages": 1}')
    elsewhere = config.load()                                           # trouble elsewhere changes nothing here
    assert elsewhere.config.storage.keep_audio_days == 7 and len(elsewhere.problems) == 2


def test_a_command_of_ones_own_for_summaries_is_a_setting():
    assert config.load().config.summary.command == []                  # none: Claude Code is asked
    write(json.dumps({"summary": {"command": ["python", "C:\\scripts\\my_summary.py"]}}))
    loaded = config.load()
    assert loaded.problems == [] and loaded.config.summary.command == ["python", "C:\\scripts\\my_summary.py"]
    assert loaded.config.summary.model == "opus"
    for wrong in ('"codex exec -"', '["codex", 5]', "true"):
        write('{"summary": {"command": %s}}' % wrong)
        loaded = config.load()
        assert loaded.config.summary.command == [] and loaded.problems[0].where == "summary.command", wrong


def test_the_port_of_the_panel_is_a_setting():
    assert config.load().config.panel.port == 0                         # not chosen yet: the program does it at its first start
    write('{"panel": {"port": 9000}}')
    loaded = config.load()
    assert loaded.problems == [] and loaded.config.panel.port == 9000
    for wrong in ("80", "70000", '"8770"', "8770.5", "true"):
        write('{"panel": {"port": %s}}' % wrong)
        loaded = config.load()
        assert loaded.config.panel.port == 0 and loaded.problems[0].where == "panel.port", wrong
    config.save(config.Config())
    assert json.loads(paths.config_file().read_text(encoding="utf-8"))["panel"] == {"port": 0}


def test_a_server_of_ones_own_is_three_settings():
    assert config.GatewayConfig() == config.GatewayConfig(url="", key_file="", model="")       # none: Tolmach of this computer
    write(json.dumps({"gateway": {"url": "http://127.0.0.1:8000", "model": "large"}}))
    loaded = config.load()
    assert loaded.problems == [] and (loaded.config.gateway.url, loaded.config.gateway.model) == ("http://127.0.0.1:8000", "large")
    write(json.dumps({"gateway": {"url": "127.0.0.1:8000", "model": 5}}))    # an address that is none, a model that is no name
    loaded = config.load()
    assert sorted(p.where for p in loaded.problems) == ["gateway.model", "gateway.url"]
    assert loaded.config.gateway == config.GatewayConfig()               # ...and it is Tolmach again, said in the window
    write(json.dumps({"gateway": {"url": "https://recognise.home:8443/speech"}}))
    assert config.load().problems == []                                  # https is an address, and so is one with a path
    for wrong in ("http://", "https:///speech", "ftp://recognise.home"):
        write(json.dumps({"gateway": {"url": wrong}}))
        assert [p.where for p in config.load().problems] == ["gateway.url"], wrong
