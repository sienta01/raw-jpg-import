from pathlib import Path

from raw_jpg_import import appdirs
from raw_jpg_import.settings import Settings, load_settings, save_settings


def test_round_trip(isolated_home):
    s = Settings(dest_root="D:/Photos", day_start="01:00")
    s.remember_name("Sunset Beachwalk")
    save_settings(s)
    assert (isolated_home / "settings.json").is_file()
    back = load_settings()
    assert back.dest_root == "D:/Photos"
    assert back.day_start == "01:00"
    assert back.recent_names == ["Sunset Beachwalk"]


def test_missing_gives_defaults():
    assert load_settings() == Settings()


def test_corrupt_file_set_aside(isolated_home):
    p = isolated_home / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_settings() == Settings()
    assert (isolated_home / "settings.json.bad").is_file()
    assert not p.exists()


def test_invalid_fields_fall_back(isolated_home):
    (isolated_home / "settings.json").write_text(
        '{"day_start": "25:00", "recent_names": [1], "dest_root": "X", "extra": 5}',
        encoding="utf-8")
    s = load_settings()
    assert s.day_start == "00:00" and s.recent_names == [] and s.dest_root == "X"


def test_recent_lists_are_bounded():
    s = Settings()
    for i in range(15):
        s.remember_name(f"n{i}")
        s.remember_source(f"s{i}")
    s.remember_name("n3")
    assert len(s.recent_names) == 10 and s.recent_names[0] == "n3"
    assert len(s.manual_sources) == 5 and s.manual_sources[0] == "s14"


def test_platform_dirs(monkeypatch, tmp_path):
    monkeypatch.delenv("RAW_JPG_IMPORT_HOME")
    monkeypatch.setattr(appdirs, "_platform", lambda: "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert appdirs.config_dir() == tmp_path / "Roaming" / "raw-jpg-import"
    assert appdirs.data_dir() == tmp_path / "Local" / "raw-jpg-import"
    assert appdirs.log_dir() == tmp_path / "Local" / "raw-jpg-import" / "logs"

    monkeypatch.setattr(appdirs, "_platform", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert appdirs.config_dir() == tmp_path / "cfg" / "raw-jpg-import"
    assert appdirs.data_dir() == tmp_path / "data" / "raw-jpg-import"
    assert appdirs.log_dir() == tmp_path / "state" / "raw-jpg-import" / "logs"

    monkeypatch.setattr(appdirs, "_platform", lambda: "darwin")
    support = Path.home() / "Library" / "Application Support"
    assert appdirs.config_dir() == support / "raw-jpg-import"
    assert appdirs.log_dir() == Path.home() / "Library" / "Logs" / "raw-jpg-import"


def test_home_override(isolated_home):
    assert appdirs.config_dir() == isolated_home
    assert appdirs.ledger_file() == isolated_home / "history.sqlite3"
    assert appdirs.log_dir() == isolated_home / "logs"
