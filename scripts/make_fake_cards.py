"""Build fake Nikon Zf memory cards for a manual end-to-end check (no real camera needed).

    python scripts/make_fake_cards.py C:\\rjitest --mode raw_jpg --big-video-mb 1500
    python scripts/make_fake_cards.py --verify C:\\rjitest

Creates, inside the (new or empty) folder:

- ``SD`` and ``MICROSD``: the two slots of a Zf in ``--mode`` (default raw_jpg: NEFs, the
  clip, one protected NEF and ``NOTES.XYZ`` on SD; JPGs on MICROSD; camera junk on both);
- ``BACKUP_A`` and ``BACKUP_B``: another day recorded identically on both cards (backup mode);
- ``Photos``: an empty destination;
- ``snapshot.json``: size, time and SHA-256 of every card file, kept OUTSIDE the cards.

``--verify`` compares the cards with the snapshot (exit 0 = unchanged, 1 = changed). The
files come from the test fakes in ``tests/fakes``; they are tiny but carry real EXIF dates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "tests"), str(REPO / "src")]

from fakes.card import (  # noqa: E402  (needs the sys.path entries above)
    DEST_FOLDER,
    EVENT,
    EVENT_NAME,
    MODES,
    FakeCard,
    FakeShot,
    expected_files,
    make_zf_cards,
    sunset_shots,
    zf_cards,
)

CARDS = ("SD", "MICROSD", "BACKUP_A", "BACKUP_B")
DEST = "Photos"
SNAPSHOT = "snapshot.json"
GENERATOR = "raw-jpg-import scripts/make_fake_cards.py"
BACKUP_NAME = "Backup Test"
BACKUP_SHOTS = [
    FakeShot("DSC_0201", datetime(2026, 2, 14, 10, 0, 0)),
    FakeShot("DSC_0202", datetime(2026, 2, 14, 10, 5, 30)),
    FakeShot("DSC_0203", datetime(2026, 2, 14, 10, 6, 0), quality="MOV"),
    FakeShot("DSC_0204", datetime(2026, 2, 14, 10, 20, 15)),
]
MAX_LISTED = 20


class ScriptError(Exception):
    """A usage problem; printed without a traceback (exit code 2)."""


# ---- safety -----------------------------------------------------------------------------------


def _check_target(base: Path) -> None:
    """Refuse anything that could be a real card or an earlier run's folder."""
    base = base.resolve()
    if base == Path(base.anchor):
        raise ScriptError(f"{base} is a drive root; choose a folder such as C:\\rjitest")
    if base.exists() and not base.is_dir():
        raise ScriptError(f"{base} exists and is not a folder")
    if base.is_dir() and any(p.name.casefold() == "dcim" for p in base.iterdir()):
        raise ScriptError(f"{base} has a DCIM folder; it looks like a memory card")
    try:
        from raw_jpg_import import volumes

        vol = volumes.volume_for_path(base)
    except Exception:
        vol = None
    if vol is not None and vol.is_card:
        raise ScriptError(f"{base} is on the memory card {vol.path} ({vol.label}); "
                          "choose a folder on your computer's own disk")
    taken = [n for n in (*CARDS, DEST, SNAPSHOT) if (base / n).exists()]
    if taken:
        raise ScriptError(f"{base} already has {', '.join(taken)}; delete them or choose a "
                          "new folder")


# ---- snapshot ---------------------------------------------------------------------------------


def _card_state(root: Path) -> dict[str, Any]:
    files, dirs, protected = FakeCard(root).state()
    return {"files": {rel: list(v) for rel, v in files.items()},
            "dirs": sorted(dirs), "protected": sorted(protected)}


def write_snapshot(base: Path, mode: str) -> Path:
    data = {"generator": GENERATOR, "mode": mode,
            "created": datetime.now().isoformat(timespec="seconds"),
            "cards": {name: _card_state(base / name) for name in CARDS}}
    path = base / SNAPSHOT
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


def _differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    out: list[str] = []
    fb, fa = before["files"], after["files"]
    for rel in sorted(fb.keys() - fa.keys()):
        out.append(f"missing: {rel}")
    for rel in sorted(fa.keys() - fb.keys()):
        out.append(f"new file: {rel}")
    for rel in sorted(fb.keys() & fa.keys()):
        (s0, m0, h0), (s1, m1, h1) = fb[rel], fa[rel]
        what = [w for w, changed in (("size", s0 != s1), ("time", m0 != m1),
                                      ("contents", h0 != h1)) if changed]
        if what:
            out.append(f"changed {' and '.join(what)}: {rel}")
    for rel in sorted(set(before["dirs"]) ^ set(after["dirs"])):
        out.append(f"{'missing' if rel in before['dirs'] else 'new'} folder: {rel}")
    for rel in sorted(set(before["protected"]) ^ set(after["protected"])):
        if rel in fa:
            out.append(f"protect flag {'removed' if rel in before['protected'] else 'added'}: "
                       f"{rel}")
    return out


def verify(base: Path) -> int:
    path = base / SNAPSHOT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ScriptError(f"cannot read {path}: {e}") from None
    if data.get("generator") != GENERATOR:
        raise ScriptError(f"{path} was not written by this script")
    changed = 0
    for name, before in data["cards"].items():
        root = base / name
        if not root.is_dir():
            print(f"{name:9} MISSING (the whole card folder is gone)")
            changed += 1
            continue
        diffs = _differences(before, _card_state(root))
        if not diffs:
            print(f"{name:9} unchanged ({len(before['files'])} files)")
            continue
        changed += 1
        print(f"{name:9} CHANGED ({len(diffs)} differences)")
        for line in diffs[:MAX_LISTED]:
            print(f"            {line}")
        if len(diffs) > MAX_LISTED:
            print(f"            ... and {len(diffs) - MAX_LISTED} more")
    print("All cards unchanged." if not changed else
          f"{changed} card(s) changed since {data.get('created', 'the snapshot')}.")
    return 1 if changed else 0


# ---- building ---------------------------------------------------------------------------------


def _describe(root: Path) -> str:
    card = FakeCard(root)
    files = card.snapshot()
    exts = Counter(rel.rpartition(".")[2].upper() for rel in files
                   if rel.startswith("DCIM/") and "/." not in rel and "NC_FLLST" not in rel)
    parts = [f"{n} {ext}" for ext, n in sorted(exts.items())]
    if card.protected():
        parts.append("protected: " + ", ".join(p.rpartition("/")[2] for p in card.protected()))
    return f"{len(files):3} files: {', '.join(parts)} + camera/system junk"


def _expected_lines(rels: set[str], indent: str) -> list[str]:
    by_folder: dict[str, list[str]] = {}
    for rel in rels:
        folder, _, name = rel.partition("/")
        by_folder.setdefault(folder, []).append(name)
    order = [f for f in dict.fromkeys(DEST_FOLDER.values()) if f in by_folder]
    lines = []
    for folder in order + sorted(set(by_folder) - set(order)):
        names = sorted(by_folder[folder])
        lines.append(f"{indent}{folder:4} {len(names):3}: {' '.join(names)}")
    return lines


def build(base: Path, mode: str, big_video_mb: int | None) -> None:
    _check_target(base)
    base.mkdir(parents=True, exist_ok=True)
    big = big_video_mb * 1024 * 1024 if big_video_mb else None
    shots = sunset_shots()
    if mode == "raw_heif":      # HLG tone mode: the Zf writes HEIF instead of JPEG
        shots = [s if s.quality == "MOV" else replace(s, quality="RAW+HIF") for s in shots]
    zf_cards(base, mode=mode, names=("SD", "MICROSD"), shots=shots, big_video=big)
    make_zf_cards(base, BACKUP_SHOTS, "backup", names=("BACKUP_A", "BACKUP_B"))
    (base / DEST).mkdir()
    snap = write_snapshot(base, mode)

    print(f"Fake cards in {base} (SD/MICROSD in {mode} mode):")
    for name in CARDS:
        print(f"  {name:9} {_describe(base / name)}")
    print(f"  {DEST:9} empty destination")
    print(f"  {snap.name}  card contents, kept outside the cards (check with --verify)")
    print()
    print(f"Expected: import SD + MICROSD into {DEST} with --day-start 01:00 and the name "
          f"\"{EVENT_NAME}\":")
    print(f"  {DEST}/{EVENT}/")
    print("\n".join(_expected_lines(expected_files(shots, mode), "    ")))
    print("  Not imported: NOTES.XYZ (unknown type, unless --copy-unknown puts it in OTHER/).")
    print("  The rollover DSC_0001 from 23:40 becomes DSC_0001-2 (NEF and JPG alike).")
    print("  With the day start at 00:00 the shots after midnight form a second day, 2026.01.24.")
    print("  Delete originals keeps the protected NEF, NOTES.XYZ, the junk and every folder.")
    print()
    backup = expected_files(BACKUP_SHOTS, "backup")
    print(f"Expected: import BACKUP_A + BACKUP_B with the name \"{BACKUP_NAME}\": "
          f"{len(backup)} files copied once, {len(backup)} identical on both cards:")
    print(f"  {DEST}/2026.02.14 - {BACKUP_NAME}/")
    print("\n".join(_expected_lines(backup, "    ")))
    print()
    print(f"Afterwards: python scripts/make_fake_cards.py --verify {base}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dir", type=Path, help="a new or empty folder (never a memory card)")
    p.add_argument("--mode", choices=MODES, default="raw_jpg",
                   help="how the Zf splits files between SD and MICROSD (default raw_jpg)")
    p.add_argument("--big-video-mb", type=int, metavar="N",
                   help="make the clip on SD N MB large (to test progress and cancel)")
    p.add_argument("--verify", action="store_true",
                   help="compare the cards in DIR with its snapshot.json")
    args = p.parse_args(argv)
    try:
        if args.verify:
            return verify(args.dir)
        if args.big_video_mb is not None and args.big_video_mb < 1:
            raise ScriptError("--big-video-mb must be at least 1")
        build(args.dir, args.mode, args.big_video_mb)
        return 0
    except ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
