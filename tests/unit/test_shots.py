from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from raw_jpg_import.formats import FormatTable, split_ext
from raw_jpg_import.models import Capture, Source, SourceFile
from raw_jpg_import.shots import Member, Shot, build_shots, same_shot

CARD_A = Source(root=Path("X:/"), label="A", kind="card", order=0)
CARD_B = Source(root=Path("Y:/"), label="B", kind="card", order=1)
CARD_C = Source(root=Path("Z:/"), label="C", kind="card", order=2)
DIR = "DCIM/100NCZ_F"
T0 = datetime(2026, 1, 23, 23, 2, 5)


def exact(t: datetime, subsec: int | None = None) -> Capture:
    return Capture(local=t, source="exif", exact=True, subsec_ms=subsec)


def fallback(t: datetime) -> Capture:
    return Capture(local=t, source="mtime", exact=False)


class Batch:
    """Scanned files from fake sources plus their capture times."""

    def __init__(self, fmt: FormatTable) -> None:
        self.fmt = fmt
        self.files: list[SourceFile] = []
        self.caps: dict[Path, Capture] = {}

    def add(self, name: str, cap: Capture | None = None, *, src: Source = CARD_A,
            size: int = 100, folder: str = DIR) -> SourceFile:
        rel = PurePosixPath(folder) / name
        stem, ext = split_ext(name)
        f = SourceFile(source=src, path=src.root / rel, rel=rel, name=name, stem=stem, ext=ext,
                       size=size, mtime_ns=1_700_000_000 * 10**9, readonly=False,
                       category=self.fmt.classify(name))
        self.files.append(f)
        if cap is not None:
            self.caps[f.path] = cap
        return f

    def build(self) -> tuple[list[Shot], list[SourceFile]]:
        return build_shots(self.files, self.caps)


@pytest.fixture
def batch(fmt) -> Batch:
    return Batch(fmt)


def only(shots: list[Shot]) -> Shot:
    assert len(shots) == 1, shots
    return shots[0]


def by_name(shot: Shot) -> dict[tuple[str, str], Member]:
    return {(m.file.source.label, m.file.name): m for m in shot.members}


# ---- same_shot -------------------------------------------------------------------------------


def test_same_shot_exact_needs_same_second():
    assert same_shot(exact(T0), exact(T0.replace(microsecond=900_000)))
    assert not same_shot(exact(T0), exact(T0 + timedelta(seconds=1)))


def test_same_shot_exact_compares_subseconds_when_both_known():
    assert same_shot(exact(T0, 340), exact(T0, 340))
    assert not same_shot(exact(T0, 340), exact(T0, 370))
    assert same_shot(exact(T0, 340), exact(T0))


def test_same_shot_fallback_within_120_s():
    assert same_shot(exact(T0), fallback(T0 + timedelta(seconds=120)))
    assert same_shot(fallback(T0 - timedelta(seconds=120)), exact(T0))
    assert not same_shot(exact(T0), fallback(T0 + timedelta(seconds=121)))
    assert same_shot(fallback(T0), fallback(T0 + timedelta(seconds=90)))


# ---- pairing ---------------------------------------------------------------------------------


def test_nef_on_card_a_and_jpg_on_card_b_make_one_shot(batch):
    nef = batch.add("DSC_0001.NEF", exact(T0, 340), size=30_000)
    jpg = batch.add("DSC_0001.JPG", exact(T0, 340), src=CARD_B, size=9_000)
    shots, orphans = batch.build()
    shot = only(shots)
    assert orphans == []
    assert (shot.id, shot.stem, shot.stem_key) == (0, "DSC_0001", "dsc_0001")
    assert shot.capture == exact(T0, 340)
    assert [(m.file, m.kind, m.role, m.folder, m.suffix) for m in shot.members] == [
        (nef, "primary", "main", "RAW", ".NEF"),
        (jpg, "primary", "main", "JPG", ".JPG"),
    ]
    assert [m.dest_name("DSC_0001-2") for m in shot.members] == ["DSC_0001-2.NEF",
                                                                "DSC_0001-2.JPG"]
    assert shot.bytes == 39_000
    assert shot.copyable() == shot.primaries() == shot.members


def test_counter_rollover_gives_two_shots(batch):
    late = batch.add("DSC_0001.NEF", exact(T0.replace(hour=23, minute=40)), folder="101NCZ_F")
    early = batch.add("DSC_0001.NEF", exact(T0.replace(hour=18, minute=0)))
    batch.add("DSC_0001.JPG", exact(T0.replace(hour=23, minute=40)), src=CARD_B,
              folder="101NCZ_F")
    batch.add("DSC_0001.JPG", exact(T0.replace(hour=18, minute=0)), src=CARD_B)
    shots, _ = batch.build()
    assert [s.id for s in shots] == [0, 1]
    assert [s.capture.local.hour for s in shots] == [18, 23]
    assert shots[0].members[0].file == early
    assert shots[1].members[0].file == late
    assert [[m.folder for m in s.members] for s in shots] == [["RAW", "JPG"], ["RAW", "JPG"]]
    assert all(m.role == "main" for s in shots for m in s.members)


def test_exact_times_with_different_subseconds_stay_apart(batch):
    batch.add("DSC_0001.NEF", exact(T0, 100))
    batch.add("DSC_0001.NEF", exact(T0, 500), folder="101NCZ_F")
    shots, _ = batch.build()
    assert len(shots) == 2


def test_hif_with_fallback_time_joins_the_exact_nef_as_partner(batch):
    batch.add("DSC_0001.NEF", exact(T0, 340))
    hif = batch.add("DSC_0001.HIF", fallback(T0 + timedelta(seconds=90)), src=CARD_B)
    shot = only(batch.build()[0])
    assert shot.capture.source == "exif"
    members = by_name(shot)
    nef_m, hif_m = members[("A", "DSC_0001.NEF")], members[("B", "DSC_0001.HIF")]
    assert nef_m.capture == exact(T0, 340)
    assert hif_m.file == hif
    assert hif_m.capture == Capture(local=T0, source="partner", exact=False, subsec_ms=340)
    assert hif_m.folder == "JPG"


def test_fallback_time_too_far_away_is_a_separate_shot(batch):
    batch.add("DSC_0001.NEF", exact(T0))
    batch.add("DSC_0001.HIF", fallback(T0 + timedelta(seconds=121)), src=CARD_B)
    shots, _ = batch.build()
    assert len(shots) == 2
    assert shots[1].capture.source == "mtime"


def test_all_fallback_shot_takes_the_lowest_rank_capture(batch):
    batch.add("DSC_0001.JPG", fallback(T0), src=CARD_B)
    batch.add("DSC_0001.NEF", fallback(T0 + timedelta(seconds=30)))
    shot = only(batch.build()[0])
    assert shot.capture == fallback(T0 + timedelta(seconds=30))
    members = by_name(shot)
    assert members[("A", "DSC_0001.NEF")].capture.source == "mtime"
    assert members[("B", "DSC_0001.JPG")].capture == Capture(
        local=T0 + timedelta(seconds=30), source="partner", exact=False)


def test_missing_capture_falls_back_to_mtime(batch):
    batch.add("DSC_0001.NEF")
    shot = only(batch.build()[0])
    assert shot.capture.source == "mtime"
    assert not shot.capture.exact


def test_case_different_stems_pair(batch):
    nef = batch.add("dsc_0001.nef", exact(T0))
    jpg = batch.add("DSC_0001.JPG", exact(T0), src=CARD_B)
    shot = only(batch.build()[0])
    assert (shot.stem, shot.stem_key) == ("dsc_0001", "dsc_0001")
    assert [(m.file, m.suffix) for m in shot.members] == [(nef, ".nef"), (jpg, ".JPG")]
    assert [m.dest_name(shot.stem) for m in shot.members] == ["dsc_0001.nef", "dsc_0001.JPG"]


def test_shot_stem_comes_from_the_lowest_rank_primary(batch):
    batch.add("DSC_0001.MOV", exact(T0))
    batch.add("dsc_0001.jpg", exact(T0), src=CARD_B)
    shot = only(batch.build()[0])
    assert shot.stem == "dsc_0001"
    assert [m.folder for m in shot.members] == ["JPG", "MOV"]


# ---- duplicates and variants -----------------------------------------------------------------


def test_backup_mode_identical_nef_is_a_duplicate_of_the_first_card(batch):
    dup = batch.add("DSC_0001.NEF", exact(T0), src=CARD_B, size=30_000)
    main = batch.add("DSC_0001.NEF", exact(T0), src=CARD_A, size=30_000)
    shot = only(batch.build()[0])
    assert [(m.file, m.role, m.dup_of, m.variant_no) for m in shot.members] == [
        (main, "main", None, 0),
        (dup, "duplicate", main, 0),
    ]
    assert [m.file for m in shot.copyable()] == [main]
    assert len(shot.primaries()) == 2
    assert shot.bytes == 30_000


def test_jpg_jpg_mode_smaller_copy_is_variant_v2(batch):
    small = batch.add("DSC_0001.JPG", exact(T0), src=CARD_A, size=2_000)
    big = batch.add("DSC_0001.JPG", exact(T0), src=CARD_B, size=5_000)
    shot = only(batch.build()[0])
    assert [(m.file, m.role, m.variant_no) for m in shot.members] == [
        (big, "main", 0),
        (small, "variant", 2),
    ]
    assert [m.dest_name("DSC_0001") for m in shot.members] == ["DSC_0001.JPG",
                                                              "DSC_0001-v2.JPG"]
    assert shot.bytes == 7_000


def test_variants_numbered_by_descending_size_with_duplicates(batch):
    a = batch.add("DSC_0001.JPG", exact(T0), src=CARD_A, size=5_000)
    b = batch.add("DSC_0001.JPG", exact(T0), src=CARD_B, size=2_000)
    c = batch.add("DSC_0001.JPG", exact(T0), src=CARD_C, size=3_000)
    d = batch.add("DSC_0001.jpg", exact(T0), src=CARD_C, size=5_000, folder="OTHER")
    shot = only(batch.build()[0])
    assert [(m.file, m.role, m.variant_no, m.dup_of) for m in shot.members] == [
        (a, "main", 0, None),
        (c, "variant", 2, None),
        (b, "variant", 3, None),
        (d, "duplicate", 0, a),
    ]


def test_different_extensions_of_one_category_are_not_variants(batch):
    batch.add("DSC_0001.JPG", exact(T0), size=5_000)
    batch.add("DSC_0001.JPEG", exact(T0), src=CARD_B, size=2_000)
    shot = only(batch.build()[0])
    assert [(m.suffix, m.role, m.variant_no) for m in shot.members] == [
        (".JPEG", "main", 0), (".JPG", "main", 0)]


# ---- sidecars --------------------------------------------------------------------------------


def test_wav_attaches_to_the_nef_in_the_same_directory(batch):
    batch.add("DSC_0001.NEF", exact(T0))
    batch.add("DSC_0001.JPG", exact(T0))
    wav = batch.add("DSC_0001.WAV", size=7)
    shot = only(batch.build()[0])
    m = by_name(shot)[("A", "DSC_0001.WAV")]
    assert (m.file, m.kind, m.role, m.folder, m.suffix) == (wav, "sidecar", "main", "RAW",
                                                            ".WAV")
    assert m.capture == Capture(local=T0, source="partner", exact=False)
    assert m.dest_name("DSC_0001-2") == "DSC_0001-2.WAV"
    assert shot.members[-1] is m        # sidecars after primaries
    assert m in shot.copyable() and m not in shot.primaries()
    assert shot.bytes == 207


def test_wav_on_the_jpg_card_follows_the_jpg(batch):
    batch.add("DSC_0001.NEF", exact(T0))
    batch.add("DSC_0001.JPG", exact(T0), src=CARD_B)
    batch.add("DSC_0001.WAV", src=CARD_B)
    shot = only(batch.build()[0])
    assert by_name(shot)[("B", "DSC_0001.WAV")].folder == "JPG"


def test_full_name_xmp_attaches_and_keeps_its_suffix(batch):
    batch.add("DSC_0001.NEF", exact(T0))
    batch.add("DSC_0001.JPG", exact(T0))
    batch.add("DSC_0001.NEF.xmp")
    batch.add("DSC_0001.JPG.XMP")
    shot = only(batch.build()[0])
    members = by_name(shot)
    nef_xmp, jpg_xmp = members[("A", "DSC_0001.NEF.xmp")], members[("A", "DSC_0001.JPG.XMP")]
    assert (nef_xmp.folder, nef_xmp.suffix) == ("RAW", ".NEF.xmp")
    assert (jpg_xmp.folder, jpg_xmp.suffix) == ("JPG", ".JPG.XMP")
    assert nef_xmp.dest_name("DSC_0001-2") == "DSC_0001-2.NEF.xmp"


def test_sony_clip_xml_attaches_to_its_video(batch):
    clip = "PRIVATE/M4ROOT/CLIP"
    batch.add("C0001.MP4", exact(T0), folder=clip)
    batch.add("C0001M01.XML", folder=clip)
    shot = only(batch.build()[0])
    xml = by_name(shot)[("A", "C0001M01.XML")]
    assert (xml.folder, xml.suffix) == ("MOV", "M01.XML")
    assert xml.dest_name("C0001-2") == "C0001-2M01.XML"


def test_orphan_sidecars(batch):
    batch.add("DSC_0001.NEF", exact(T0))
    lonely = batch.add("DSC_0099.xmp")
    other_dir = batch.add("DSC_0001.WAV", folder="DCIM/101NCZ_F")
    other_card = batch.add("DSC_0001.xmp", src=CARD_B)
    batch.add("DSC_0001.XYZ")                       # unknown: not part of shots
    batch.add("NC_FLLST.DAT", folder="")            # ignored: not part of shots
    shots, orphans = batch.build()
    assert [m.file.name for m in only(shots).members] == ["DSC_0001.NEF"]
    assert sorted(orphans, key=lambda f: f.name) == sorted([lonely, other_dir, other_card],
                                                           key=lambda f: f.name)


def test_only_sidecars_and_no_primaries(batch):
    wav = batch.add("DSC_0001.WAV")
    assert batch.build() == ([], [wav])


def test_identical_sidecars_of_backup_primaries_are_duplicates(batch):
    nef_a = batch.add("DSC_0001.NEF", exact(T0), size=30_000)
    wav_a = batch.add("DSC_0001.WAV", size=50)
    batch.add("DSC_0001.NEF", exact(T0), src=CARD_B, size=30_000)
    wav_b = batch.add("DSC_0001.WAV", src=CARD_B, size=50)
    shot = only(batch.build()[0])
    members = by_name(shot)
    assert members[("B", "DSC_0001.NEF")].dup_of == nef_a
    assert (members[("A", "DSC_0001.WAV")].role, members[("A", "DSC_0001.WAV")].dup_of) == (
        "main", None)
    assert (members[("B", "DSC_0001.WAV")].role, members[("B", "DSC_0001.WAV")].dup_of) == (
        "duplicate", wav_a)
    assert [m.file for m in shot.copyable()] == [nef_a, wav_a]
    assert wav_b in [m.file for m in shot.members]


def test_sidecars_with_same_name_and_different_size_become_variants(batch):
    batch.add("DSC_0001.JPG", exact(T0), size=5_000)
    batch.add("DSC_0001.WAV", size=40)
    batch.add("DSC_0001.JPG", exact(T0), src=CARD_B, size=2_000)
    batch.add("DSC_0001.WAV", src=CARD_B, size=60)
    shot = only(batch.build()[0])
    members = by_name(shot)
    big, small = members[("B", "DSC_0001.WAV")], members[("A", "DSC_0001.WAV")]
    assert (big.role, big.variant_no, big.dest_name("DSC_0001")) == ("main", 0,
                                                                     "DSC_0001.WAV")
    assert (small.role, small.variant_no, small.dest_name("DSC_0001")) == (
        "variant", 2, "DSC_0001-v2.WAV")
    assert [m.kind for m in shot.members] == ["primary", "primary", "sidecar", "sidecar"]


# ---- ordering --------------------------------------------------------------------------------


def _populate(batch: Batch) -> None:
    for n in range(1, 6):
        t = T0 + timedelta(seconds=7 * n)
        batch.add(f"DSC_{n:04d}.NEF", exact(t), size=30_000 + n)
        batch.add(f"DSC_{n:04d}.JPG", exact(t), src=CARD_B, size=9_000 + n)
    batch.add("DSC_0003.WAV", size=10)
    batch.add("DSC_0003.NEF", exact(T0 + timedelta(seconds=21)), src=CARD_B, size=30_003)
    batch.add("DSC_0003.JPG", exact(T0 + timedelta(seconds=21)), src=CARD_C, size=8_000)
    batch.add("DSC_0001.NEF", exact(T0 - timedelta(hours=5)), folder="DCIM/101NCZ_F")
    batch.add("DSC_0007.HIF", fallback(T0))
    batch.add("DSC_0007.NEF", exact(T0 + timedelta(seconds=7)), src=CARD_B)
    batch.add("DSC_0008.MOV", exact(T0 + timedelta(seconds=7)))
    batch.add("DSC_0099.xmp")


def _summary(shots: list[Shot], orphans: list[SourceFile]) -> list[object]:
    return [[(s.id, s.stem, s.capture, [(m.file.path, m.kind, m.role, m.variant_no,
                                         m.dup_of.path if m.dup_of else None, m.capture)
                                        for m in s.members]) for s in shots],
            [f.path for f in orphans]]


def test_ids_are_deterministic_when_input_order_is_shuffled(batch, fmt):
    _populate(batch)
    expected = _summary(*batch.build())
    for seed in range(10):
        shuffled = Batch(fmt)
        shuffled.files = list(batch.files)
        shuffled.caps = dict(batch.caps)
        random.Random(seed).shuffle(shuffled.files)
        assert _summary(*shuffled.build()) == expected


def test_shots_are_ordered_by_time_then_stem(batch):
    _populate(batch)
    shots, orphans = batch.build()
    assert [s.id for s in shots] == list(range(len(shots)))
    keys = [(s.capture.local, s.stem_key) for s in shots]
    assert keys == sorted(keys)
    assert [s.stem for s in shots] == ["DSC_0001", "DSC_0001", "DSC_0007", "DSC_0008",
                                       "DSC_0002", "DSC_0003", "DSC_0004", "DSC_0005"]
    assert shots[0].capture.local.hour == 18
    assert [f.name for f in orphans] == ["DSC_0099.xmp"]


def test_build_shots_does_not_mutate_inputs(batch):
    _populate(batch)
    files, caps = list(batch.files), dict(batch.caps)
    batch.build()
    assert batch.files == files and batch.caps == caps
