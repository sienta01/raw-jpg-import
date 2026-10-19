"""The command line, driven through ``cli.main([...])`` on fake cards.

Card detection is mocked everywhere (see conftest), so a command without sources can only
ever see the fake cards a test hands it.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest
from fakes.card import (
    EVENT,
    JUNK_FILES,
    FakeCard,
    FakeShot,
    FaultyFileOps,
    ZfCards,
    files_under,
    zf_cards,
)

from raw_jpg_import import appdirs, cli, copier, engine
from raw_jpg_import.settings import load_settings
from raw_jpg_import.volumes import Volume

D = datetime
SHOTS = [
    FakeShot("DSC_0001", D(2026, 1, 23, 18, 0, 0)),
    FakeShot("DSC_0002", D(2026, 1, 23, 23, 2, 10), protect=True),
    FakeShot("DSC_0003", D(2026, 1, 23, 23, 20, 0), quality="MOV"),
    FakeShot("DSC_0001", D(2026, 1, 23, 23, 40, 0), folder="101NCZ_F"),
    FakeShot("DSC_0004", D(2026, 1, 24, 0, 15, 0), folder="101NCZ_F"),
]
LIBRARY = {f"{EVENT}/{p}" for p in (
    "RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG", "RAW/DSC_0002.NEF", "JPG/DSC_0002.JPG",
    "MOV/DSC_0003.MOV", "RAW/DSC_0001-2.NEF", "JPG/DSC_0001-2.JPG", "RAW/DSC_0004.NEF",
    "JPG/DSC_0004.JPG")}


@pytest.fixture(scope="module")
def cards_ro(tmp_path_factory) -> ZfCards:
    """Read-only fake cards shared by the commands that do not delete."""
    z = zf_cards(tmp_path_factory.mktemp("cli-cards"), shots=SHOTS)
    state = z.state()
    yield z
    assert z.state() == state, "a CLI command changed the cards"


@pytest.fixture
def cards(tmp_path) -> ZfCards:
    return zf_cards(tmp_path / "own", shots=SHOTS)


@pytest.fixture(autouse=True)
def _not_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


@pytest.fixture(autouse=True)
def _close_log_files():
    yield
    root = logging.getLogger("raw_jpg_import")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def run(capsys, *argv: object) -> tuple[int, str, str]:
    code = cli.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def srcs(z: ZfCards) -> list[str]:
    return [str(r) for r in z.roots]


# ---- cards and scan ---------------------------------------------------------------------------


def _volume(root: Path, label: str = "NIKON Z F") -> Volume:
    return Volume(path=root, label=label, serial="1A2B-3C4D", total=64_000_000_000,
                  free=60_000_000_000, is_card=True)


def test_cards(capsys, monkeypatch, cards_ro):
    monkeypatch.setattr(engine, "detect_cards", lambda: [_volume(cards_ro.roots[0])])
    code, out, _ = run(capsys, "cards")
    assert code == 0 and "NIKON Z F" in out and str(cards_ro.roots[0]) in out
    code, out, _ = run(capsys, "cards", "--json")
    data = json.loads(out)
    assert data == [{"path": str(cards_ro.roots[0]), "label": "NIKON Z F",
                     "serial": "1A2B-3C4D", "total": 64_000_000_000, "free": 60_000_000_000}]


def test_cards_none(capsys):
    code, out, _ = run(capsys, "cards")
    assert code == 0 and "No memory cards found" in out
    assert run(capsys, "cards", "--json")[1].strip() == "[]"


def test_scan_json(capsys, cards_ro, dest):
    code, out, _ = run(capsys, "scan", *srcs(cards_ro), "--json")
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"day_start", "days", "unknown", "errors"}
    assert data["day_start"] == "00:00"
    assert [d["date"] for d in data["days"]] == ["2026-01-23", "2026-01-24"]
    day = data["days"][0]
    assert set(day) == {"date", "first", "last", "counts", "bytes", "fallback_dates", "new",
                        "suggestions"}
    assert day["counts"] == {"RAW": 3, "JPG": 3, "MOV": 1}
    assert day["first"] == "2026-01-23 18:00:00" and day["new"] is None
    assert data["unknown"] == {"xyz": 1} and data["errors"] == []

    code, out, _ = run(capsys, "scan", *srcs(cards_ro), "--json", "--dest", dest,
                       "--day-start", "01:00")
    data = json.loads(out)
    assert [(d["date"], d["new"]) for d in data["days"]] == [("2026-01-23", 9)]


def test_scan_text(capsys, cards_ro):
    code, out, _ = run(capsys, "scan", *srcs(cards_ro))
    assert code == 0
    assert "2026.01.23" in out and "2026.01.24" in out
    assert "--day-start 01:00" in out
    assert ".xyz x1" in out


def test_scan_rejects_a_bad_day_start(capsys, cards_ro):
    code, _, err = run(capsys, "scan", *srcs(cards_ro), "--day-start", "07:00")
    assert code == 2 and "between 00:00 and 06:00" in err


# ---- import -----------------------------------------------------------------------------------


def test_dry_run_writes_nothing(capsys, cards_ro, dest):
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk", "--dry-run")
    assert code == 0
    assert "NEW      2026.01.23 - Sunset Beachwalk" in out and "Copy 9 files" in out
    assert list(dest.iterdir()) == []
    assert load_settings().dest_root is None          # a dry run remembers nothing either

    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk", "--dry-run", "--json")
    plan = json.loads(out)["plan"]
    assert plan["counts"]["copy"] == 9 and plan["counts"]["unknown"] == 1
    assert plan["targets"][0]["folder"] == EVENT and plan["problems"] == []
    assert list(dest.iterdir()) == []


def test_import_with_a_name(capsys, cards_ro, dest):
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "--name", "Sunset Beachwalk")
    assert code == 0, out
    assert files_under(dest) == LIBRARY
    assert "Import finished." in out and "Copied and verified: 9 files" in out
    s = load_settings()
    assert s.dest_root == str(dest) and s.day_start == "01:00"
    assert s.recent_names == ["Sunset Beachwalk"]

    # Run again: nothing new, nothing asked, exit 0.
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--day-start", "01:00")
    assert code == 0 and "Nothing new to import." in out
    assert files_under(dest) == LIBRARY


def test_import_json_result(capsys, cards_ro, dest):
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk", "--json")
    assert code == 0
    result = json.loads(out)["result"]
    assert result["status"] == "ok"
    assert result["counts"] == {"copied": 9, "not_imported": 1}
    assert result["folders"] == [str(dest / EVENT)]
    assert Path(result["log"]).is_file()


def planned(capsys, z: ZfCards, dest: Path, *args: object) -> dict[str, dict[str, int]]:
    """``import --dry-run --json``: ``{event folder: {subfolder: files to copy}}``."""
    code, out, err = run(capsys, "import", *srcs(z), "--dest", dest, "--dry-run", "--json",
                         *args)
    assert code == 0, err
    plan = json.loads(out)["plan"]
    assert plan["problems"] == [] and list(dest.iterdir()) == []
    return {t["folder"]: {f: v["files"] for f, v in t["per_folder"].items()}
            for t in plan["targets"]}


WHOLE_WALK = {EVENT: {"RAW": 4, "JPG": 4, "MOV": 1}}


def test_names_per_day(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "2026-01-23=Sunset Beachwalk",
                   "-n", "2026.01.24=After Midnight") == {
        EVENT: {"RAW": 3, "JPG": 3, "MOV": 1},
        "2026.01.24 - After Midnight": {"RAW": 1, "JPG": 1}}


def test_name_for_every_day(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Sunset Beachwalk") == {
        EVENT: {"RAW": 3, "JPG": 3, "MOV": 1},
        "2026.01.24 - Sunset Beachwalk": {"RAW": 1, "JPG": 1}}


def test_specific_name_beats_the_generic_one(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Walk", "-n", "2026-01-24=Night") == {
        "2026.01.23 - Walk": {"RAW": 3, "JPG": 3, "MOV": 1},
        "2026.01.24 - Night": {"RAW": 1, "JPG": 1}}


def test_two_generic_names_exit_2(capsys, cards_ro, dest):
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "-n", "A", "-n", "B")
    assert code == 2 and "only one --name" in err


def test_join(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Sunset Beachwalk",
                   "--join", "2026-01-24") == WHOLE_WALK


def test_merge(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Sunset Beachwalk", "--merge") == WHOLE_WALK


def test_day_start_keeps_the_walk_on_one_day(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Sunset Beachwalk",
                   "--day-start", "01:00") == WHOLE_WALK


def test_select_days(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "-n", "Night", "--days", "2026-01-24") == {
        "2026.01.24 - Night": {"RAW": 1, "JPG": 1}}
    assert planned(capsys, cards_ro, dest, "-n", "Walk", "--until", "2026.01.23") == {
        "2026.01.23 - Walk": {"RAW": 3, "JPG": 3, "MOV": 1}}
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "-n", "x",
                       "--days", "2026-03-01")
    assert code == 2 and "no photos on 2026-03-01" in err


def test_single_existing_folder_is_used(capsys, cards_ro, dest):
    (dest / "2026.01.23 - Beach").mkdir()
    code, out, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start",
                         "01:00", "--dry-run", "--json")
    assert code == 0, err
    targets = json.loads(out)["plan"]["targets"]
    assert [(t["folder"], t["exists"]) for t in targets] == [("2026.01.23 - Beach", True)]


def test_ambiguous_existing_folders_exit_2(capsys, cards_ro, dest):
    (dest / "2026.01.23 - Beach").mkdir()
    (dest / "2026.01.23 - Dinner").mkdir()
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00")
    assert code == 2
    assert "2026.01.23 - Beach" in err and "2026.01.23 - Dinner" in err
    assert files_under(dest) == set()


def test_missing_name_without_tty_exit_2(capsys, cards_ro, dest):
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00")
    assert code == 2 and "No event name for: 2026.01.23" in err
    assert list(dest.iterdir()) == []


def test_yes_without_name_makes_date_only_folders(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "--day-start", "01:00", "--yes") == {
        "2026.01.23": {"RAW": 4, "JPG": 4, "MOV": 1}}


def test_interactive_prompt(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY("Sunset Beachwalk\ny\n"))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00")
    assert code == 0, out
    assert "Event name" in out and "Import 9 files" in out
    assert files_under(dest) == LIBRARY


def test_interactive_end_of_input_exit_2(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY(""))
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00")
    assert code == 2 and "no event name" in err
    assert list(dest.iterdir()) == []


def test_interactive_join_with_caret(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY("Sunset Beachwalk\n^\n"))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--dry-run")
    assert code == 0, out
    assert "^ = same folder as the day above" in out
    assert f"NEW      {EVENT}" in out and "2026.01.24 -" not in out
    assert list(dest.iterdir()) == []


def test_interactive_no_copies_nothing(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY("Sunset Beachwalk\nn\n"))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00")
    assert code == 0 and "Nothing copied." in out
    assert list(dest.iterdir()) == []


def test_failures_exit_1(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr(copier, "RealFileOps",
                        lambda: FaultyFileOps(read_error={"DSC_0004.NEF": 0}))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk")
    assert code == 1
    assert "finished with 1 error" in out and "FAILED" in out and "DSC_0004.NEF" in out


def test_blocked_plan_exit_2(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr(engine, "disk_free", lambda p: 1000)
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk")
    assert code == 2 and "PROBLEM: Not enough space" in out
    assert list(dest.iterdir()) == []


def test_another_import_running_exit_3(capsys, cards_ro, dest):
    with engine.import_lock():
        code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", dest,
                           "--day-start", "01:00", "-n", "Sunset Beachwalk")
    assert code == 3 and "Another import" in err
    assert files_under(dest) == set()


def test_destination_problems_exit_2(capsys, cards_ro, tmp_path):
    code, _, err = run(capsys, "import", *srcs(cards_ro), "--dest", tmp_path / "missing")
    assert code == 2 and "does not exist" in err
    code, _, err = run(capsys, "import", *srcs(cards_ro))
    assert code == 2 and "No destination" in err
    code, _, err = run(capsys, "import", tmp_path / "not-a-card", "--dest", tmp_path)
    assert code == 2 and "not a folder" in err


def test_no_cards_exit_2(capsys, dest):
    code, _, err = run(capsys, "import", "--dest", dest)
    assert code == 2 and "No memory cards found" in err


def test_detected_fake_cards_are_used(capsys, cards_ro, dest, monkeypatch):
    vols = [_volume(r, label) for r, label in zip(cards_ro.roots, ("NIKON Z F", "NIKON Z F 2"),
                                                   strict=True)]
    monkeypatch.setattr(engine, "detect_cards", lambda: vols)
    code, out, _ = run(capsys, "import", "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk")
    assert code == 0, out
    assert "Cards found:" in out and "NIKON Z F 2" in out
    assert files_under(dest) == LIBRARY


# ---- delete originals (fake cards only) --------------------------------------------------------


def test_delete_originals_yes(capsys, cards, dest):
    code, out, _ = run(capsys, "import", *srcs(cards), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk", "--delete-originals", "--yes", "--json")
    assert code == 0, out
    result = json.loads(out)["result"]
    assert result["deleted"] == {"deleted": 8}
    assert files_under(dest) == LIBRARY
    # Kept: the protected NEF, the unknown file, system files and every folder.
    assert set(cards.slot1.snapshot()) == JUNK_FILES | {"DCIM/100NCZ_F/DSC_0002.NEF",
                                                        "DCIM/100NCZ_F/NOTES.XYZ"}
    assert set(cards.slot2.snapshot()) == JUNK_FILES
    assert cards.slot1.dirs() >= {"DCIM/100NCZ_F", "DCIM/101NCZ_F"}


def test_json_delete_never_prompts(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY("DELETE\n"))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start",
                       "01:00", "-n", "Sunset Beachwalk", "--delete-originals", "--json")
    assert code == 0
    assert "deleted" not in json.loads(out)["result"]       # stdout is pure JSON
    # (the module fixture checks that the cards are unchanged)


def test_delete_failure_exit_1(capsys, cards_ro, dest, monkeypatch):
    from raw_jpg_import import deleter
    from raw_jpg_import.fsops import RealFileOps

    class NoRemove(RealFileOps):
        def remove(self, p: Path) -> None:
            raise PermissionError(13, "Access is denied", str(p))

    monkeypatch.setattr(deleter, "RealFileOps", NoRemove)
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start",
                       "01:00", "-n", "Sunset Beachwalk", "--delete-originals", "--yes")
    assert code == 1 and "Could not delete: 8" in out


def test_delete_originals_needs_confirmation(capsys, cards_ro, dest):
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start",
                       "01:00", "-n", "Sunset Beachwalk", "--delete-originals")
    assert code == 0
    assert "Nothing deleted." in out and "formatting the card in the camera" in out


def test_delete_originals_typed_confirmation(capsys, cards, dest, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTTY("Sunset Beachwalk\ny\nDELETE\n"))
    code, out, _ = run(capsys, "import", *srcs(cards), "--dest", dest, "--day-start", "01:00",
                       "--delete-originals")
    assert code == 0, out
    assert "Deleted from cards: 8 files" in out
    assert FakeCard(cards.roots[1]).snapshot().keys().isdisjoint(
        {"DCIM/100NCZ_F/DSC_0001.JPG"})


# ---- config -----------------------------------------------------------------------------------


def test_config_path_init_show(capsys):
    code, out, _ = run(capsys, "config", "path")
    assert code == 0 and str(appdirs.config_file()) in out and "not created yet" in out
    code, out, _ = run(capsys, "config", "init")
    assert code == 0 and appdirs.config_file().is_file()
    code, _, err = run(capsys, "config", "init")
    assert code == 2 and "Already exists" in err
    code, out, _ = run(capsys, "config", "path")
    assert "not created yet" not in out
    code, out, _ = run(capsys, "config", "show")
    assert code == 0 and "RAW" in out and "nef" in out and "Unknown types: report" in out
    code, out, _ = run(capsys, "config", "show", "--json")
    data = json.loads(out)
    assert data["config_file"] == str(appdirs.config_file())
    assert {c["key"] for c in data["categories"]} >= {"raw", "jpg", "heif", "video", "sidecar"}
    assert data["unknown"] == "report" and data["other_folder"] == "OTHER"


def test_bad_config_exit_2(capsys, cards_ro):
    path = appdirs.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema = 1\n[categories.raw\n", encoding="utf-8")
    code, _, err = run(capsys, "scan", *srcs(cards_ro))
    assert code == 2 and "config error" in err


def test_copy_unknown_flag(capsys, cards_ro, dest):
    assert planned(capsys, cards_ro, dest, "--day-start", "01:00", "-n", "Sunset Beachwalk",
                   "--copy-unknown") == {EVENT: {"RAW": 4, "JPG": 4, "MOV": 1, "OTHER": 1}}


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "raw-jpg-import" in capsys.readouterr().out


def test_cancelled_import_exit_130(capsys, cards_ro, dest, monkeypatch):
    from raw_jpg_import.errors import Cancelled

    def cancel_at(path: Path) -> None:
        if path.name == "DSC_0004.NEF":
            raise Cancelled()

    monkeypatch.setattr(copier, "RealFileOps", lambda: FaultyFileOps(on_open_read=cancel_at))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk")
    assert code == 130 and "Import cancelled" in out
    assert not any(p.suffix == ".part" for p in dest.rglob("*"))


def test_disk_full_exit_1(capsys, cards_ro, dest, monkeypatch):
    monkeypatch.setattr(copier, "RealFileOps", lambda: FaultyFileOps(enospc=["*"]))
    code, out, _ = run(capsys, "import", *srcs(cards_ro), "--dest", dest, "--day-start", "01:00",
                       "-n", "Sunset Beachwalk")
    assert code == 1 and "Import stopped: The destination drive is full" in out
    assert files_under(dest) == set()
