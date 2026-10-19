"""Pair files from all sources into shots.

A shot is one press of the shutter (or one video clip): e.g. DSC_0001.NEF on card A plus
DSC_0001.JPG on card B, plus any sidecars (DSC_0001.WAV voice memo, DSC_0001.NEF.xmp).
Pure functions over scanned files and their capture times; no file I/O.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

from .metadata import capture_from_mtime
from .models import Capture, Source, SourceFile
from .naming import key

MemberKind = Literal["primary", "sidecar"]
MemberRole = Literal["main", "duplicate", "variant"]

MAX_GAP = timedelta(seconds=120)   # fallback times this close to a partner join its shot

_SONY_SIDECAR = re.compile(r"(.+)[Mm][0-9]{2}")   # C0001M01.XML belongs to C0001.MP4
_ROLE_ORDER = {"main": 0, "variant": 1, "duplicate": 2}


@dataclass
class Member:
    """One file of a shot and how it is named at the destination."""

    file: SourceFile
    capture: Capture            # fallback members inherit the shot's capture as "partner"
    kind: MemberKind
    role: MemberRole
    dup_of: SourceFile | None   # for duplicates: the member it duplicates
    variant_no: int             # 0 for main/duplicate; 2, 3, ... for variants (-v2, -v3)
    folder: str                 # destination subfolder ("RAW", "JPG", ...)
    suffix: str                 # name after the stem, original case: ".NEF", ".NEF.xmp"

    def dest_name(self, stem: str) -> str:
        return stem + (f"-v{self.variant_no}" if self.variant_no else "") + self.suffix


@dataclass
class Shot:
    id: int                     # 0..n-1 in deterministic order
    stem: str                   # stem of the lowest-rank main primary, original case
    stem_key: str               # naming.key(stem)
    capture: Capture            # best member capture (exact beats fallback)
    members: list[Member]       # primaries first, then sidecars

    def copyable(self) -> list[Member]:
        """Members that are copied: main and variant (duplicates are not)."""
        return [m for m in self.members if m.role != "duplicate"]

    def primaries(self) -> list[Member]:
        """All primary members, duplicates included."""
        return [m for m in self.members if m.kind == "primary"]

    @property
    def bytes(self) -> int:
        return sum(m.file.size for m in self.copyable())


def same_shot(a: Capture, b: Capture) -> bool:
    """True if two captures can belong to the same shutter press.

    Two exact times must agree to the second (and in sub-seconds when both have them);
    otherwise a fallback time within 120 s is close enough.
    """
    if a.exact and b.exact:
        if a.local.replace(microsecond=0) != b.local.replace(microsecond=0):
            return False
        return a.subsec_ms is None or b.subsec_ms is None or a.subsec_ms == b.subsec_ms
    return abs(a.local - b.local) <= MAX_GAP


def build_shots(files: Sequence[SourceFile],
                caps: Mapping[Path, Capture]) -> tuple[list[Shot], list[SourceFile]]:
    """Group ``files`` into shots; return ``(shots, orphan sidecars)``.

    Only primary and sidecar files take part; unknown and ignored files are left out.
    ``caps`` should hold a Capture for every primary (a missing one falls back to mtime).
    """
    primaries = sorted((f for f in files if _role(f) == "primary"), key=_file_order)
    sidecars = sorted((f for f in files if _role(f) == "sidecar"), key=_file_order)
    cap_of = {f.path: caps.get(f.path) or capture_from_mtime(f.mtime_ns) for f in primaries}

    shots = [_make_shot(cluster, cap_of) for cluster in _clusters(primaries, cap_of)]
    orphans = _attach_sidecars(shots, sidecars)
    for shot in shots:
        _mark_same_names(_sidecars(shot), _sidecar_name_key)
        shot.members.sort(key=_member_order)

    shots.sort(key=_shot_order)
    for n, shot in enumerate(shots):
        shot.id = n
    return shots, orphans


# ---- clustering ------------------------------------------------------------------------------


def _clusters(primaries: list[SourceFile],
              cap_of: Mapping[Path, Capture]) -> list[list[SourceFile]]:
    """Primaries split into shots: same stem key, linked by :func:`same_shot` (union-find)."""
    buckets: dict[str, list[SourceFile]] = defaultdict(list)
    for f in primaries:
        buckets[key(f.stem)].append(f)
    out: list[list[SourceFile]] = []
    for bucket in buckets.values():
        items = sorted(bucket, key=lambda f: (cap_of[f.path].local, _file_order(f)))
        parent = list(range(len(items)))
        for i, a in enumerate(items):
            ca = cap_of[a.path]
            for j in range(i + 1, len(items)):
                cb = cap_of[items[j].path]
                if cb.local - ca.local > MAX_GAP:
                    break   # sorted by time: nothing later can match either
                if same_shot(ca, cb):
                    parent[_root(parent, j)] = _root(parent, i)
        groups: dict[int, list[SourceFile]] = defaultdict(list)
        for i, f in enumerate(items):
            groups[_root(parent, i)].append(f)
        out.extend(groups.values())
    return out


def _root(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _make_shot(cluster: list[SourceFile], cap_of: Mapping[Path, Capture]) -> Shot:
    best = min(cluster, key=lambda f: (not cap_of[f.path].exact, _rank(f), _file_order(f)))
    shot_cap = cap_of[best.path]
    members: list[Member] = []
    for f in cluster:
        own = cap_of[f.path]
        cap = own if f is best or own.exact else _partner(shot_cap)
        members.append(Member(file=f, capture=cap, kind="primary", role="main", dup_of=None,
                              variant_no=0, folder=_folder(f), suffix=f.name[len(f.stem):]))
    _mark_same_names(members, lambda m: (m.file.category.key if m.file.category else "",
                                         m.file.ext))
    members.sort(key=_member_order)
    lead = next(m for m in members if m.role == "main")   # lowest rank sorts first
    return Shot(id=-1, stem=lead.file.stem, stem_key=key(lead.file.stem), capture=shot_cap,
                members=members)


def _partner(best: Capture) -> Capture:
    return replace(best, source="partner", exact=False)


def _mark_same_names(members: list[Member], name_key: Callable[[Member], Hashable]) -> None:
    """Set duplicate/variant roles among members that would share a destination name.

    Equal size presumably means the same file recorded twice (backup mode): the member from
    the lowest source order stays main. This is only a guess made without reading the files;
    the copier hashes each duplicate and keeps it as a variant if its bytes differ. Different
    sizes are variants (JPEG/JPEG slot mode): the largest keeps the plain name, the others
    get -v2, -v3, ... by descending size.
    """
    groups: dict[Hashable, list[Member]] = defaultdict(list)
    for m in members:
        groups[name_key(m)].append(m)
    for group in groups.values():
        by_size: dict[int, list[Member]] = defaultdict(list)
        for m in group:
            by_size[m.file.size].append(m)
        for n, size in enumerate(sorted(by_size, reverse=True)):
            head, *dups = sorted(by_size[size], key=lambda m: _file_order(m.file))
            head.role = "main" if n == 0 else "variant"
            head.variant_no = 0 if n == 0 else n + 1
            head.dup_of = None
            for m in dups:
                m.role, m.dup_of, m.variant_no = "duplicate", head.file, 0


# ---- sidecars --------------------------------------------------------------------------------

_Where = tuple[Source, str, str]   # (source, parent dir key, name key)


def _attach_sidecars(shots: list[Shot], sidecars: list[SourceFile]) -> list[SourceFile]:
    """Add each sidecar to the shot of its primary; return the sidecars that have none."""
    owner: dict[Path, tuple[Shot, Member]] = {}
    by_stem: dict[_Where, list[SourceFile]] = defaultdict(list)
    by_name: dict[_Where, list[SourceFile]] = defaultdict(list)
    for shot in shots:
        for m in shot.members:
            owner[m.file.path] = (shot, m)
            by_stem[_where(m.file, m.file.stem)].append(m.file)
            by_name[_where(m.file, m.file.name)].append(m.file)

    orphans: list[SourceFile] = []
    for s in sidecars:
        found = _find_primary(s, by_stem, by_name)
        if found is None:
            orphans.append(s)
            continue
        primary, suffix = found
        shot, pm = owner[primary.path]
        shot.members.append(Member(file=s, capture=_partner(shot.capture), kind="sidecar",
                                   role="main", dup_of=None, variant_no=0, folder=pm.folder,
                                   suffix=suffix))
    return orphans


def _find_primary(s: SourceFile, by_stem: Mapping[_Where, list[SourceFile]],
                  by_name: Mapping[_Where, list[SourceFile]]) -> tuple[SourceFile, str] | None:
    """The primary a sidecar belongs to (same source and directory) and the sidecar's suffix.

    Matches, most specific first: full name (DSC_0001.NEF.xmp), same stem (DSC_0001.WAV),
    Sony clip metadata (C0001M01.XML). The lowest category rank wins.
    """
    cands: list[tuple[int, int, tuple[object, ...], SourceFile, str]] = []

    def add(prims: list[SourceFile], priority: int, stem_len: int) -> None:
        for p in prims:
            cands.append((_rank(p), priority, _file_order(p), p, s.name[stem_len:]))

    add(by_name.get(_where(s, s.stem), []), 0, len(s.stem.rpartition(".")[0]))
    add(by_stem.get(_where(s, s.stem), []), 1, len(s.stem))
    sony = _SONY_SIDECAR.fullmatch(s.stem)
    if sony:
        add(by_stem.get(_where(s, sony.group(1)), []), 2, len(sony.group(1)))
    if not cands:
        return None
    *_, primary, suffix = min(cands, key=lambda c: c[:3])
    return primary, suffix


def _where(f: SourceFile, name: str) -> _Where:
    return f.source, key(f.rel.parent.as_posix()), key(name)


def _sidecars(shot: Shot) -> list[Member]:
    return [m for m in shot.members if m.kind == "sidecar"]


def _sidecar_name_key(m: Member) -> tuple[str, str]:
    return key(m.folder), key(m.suffix)


# ---- ordering helpers ------------------------------------------------------------------------


def _role(f: SourceFile) -> str | None:
    return f.category.role if f.category is not None else None


def _rank(f: SourceFile) -> int:
    return f.category.rank if f.category is not None else 1 << 30


def _folder(f: SourceFile) -> str:
    return (f.category.folder if f.category is not None else None) or ""


def _file_order(f: SourceFile) -> tuple[int, str, str]:
    return f.source.order, f.rel.as_posix(), str(f.path)


def _member_order(m: Member) -> tuple[object, ...]:
    if m.kind == "primary":
        cat = m.file.category
        group: tuple[object, ...] = (0, _rank(m.file), cat.key if cat else "", m.file.ext)
    else:
        group = (1, key(m.folder), key(m.suffix), "")
    return (*group, _ROLE_ORDER[m.role], m.variant_no, _file_order(m.file))


def _shot_order(shot: Shot) -> tuple[object, ...]:
    files = [m.file for m in shot.primaries()]
    return (shot.capture.local, shot.stem_key, min(f.source.order for f in files),
            min(f.rel.as_posix() for f in files), min(str(f.path) for f in files))
