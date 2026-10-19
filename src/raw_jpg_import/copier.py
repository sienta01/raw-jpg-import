"""Execute an import plan: copy, verify, rename into place, record.

Per file: write to a hidden ``.part`` temp file next to the destination, fsync, re-read the
copy and compare its SHA-256 with the bytes read from the card (one retry), set the card's
timestamps, then rename into place without ever replacing an existing file. Only then is
the file recorded in the ledger. Cards are only ever opened for reading.

Backup twins (the same shot on both cards) are not taken on trust: after the copy each twin
is hashed and compared with its main member's copy; a twin whose bytes differ is kept too,
under a ``-vN`` name next to the main.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from .errors import (
    Cancelled,
    DestFull,
    HardwareSuspect,
    PlanBlocked,
    SourceChanged,
    VerifyMismatch,
)
from .events import (
    NEVER_CANCELLED,
    CancelToken,
    Emit,
    FileDone,
    Notice,
    Progress,
    Throttle,
    no_emit,
)
from .fsops import FileOps, RealFileOps, is_online_only, keep_awake
from .ledger import Ledger, Row, root_key
from .models import SourceFile
from .naming import key
from .planner import Action, ImportPlan, PlanItem
from .volumes import no_error_dialogs

log = logging.getLogger(__name__)

CHUNK = 4 * 1024 * 1024
STALE_PART_AGE_S = 3600
MAX_MISMATCHES = 3

_DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
_DISK_FULL_WINERRORS = {112, 39}          # ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL
_TOO_BIG_WINERRORS = {223}                # ERROR_FILE_TOO_LARGE
FAT32_MAX_FILE = 0xFFFFFFFF                # 4 GiB - 1 byte
MAX_TWIN_VARIANTS = 999
_VARIANT = re.compile(r"-v\d+$", re.IGNORECASE)


class Outcome(Enum):
    COPIED = "copied"
    ALREADY_THERE = "already there"
    DUPLICATE = "duplicate"
    CULLED = "removed from library"
    NOT_IMPORTED = "not imported"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ItemResult:
    item_id: int
    outcome: Outcome
    dest: Path | None = None
    sha256: str | None = None
    error: str | None = None
    ledger_id: int | None = None


Status = Literal["ok", "partial", "cancelled", "aborted"]


@dataclass
class ImportReport:
    plan: ImportPlan
    results: dict[int, ItemResult]
    status: Status
    started: datetime
    finished: datetime
    abort_reason: str | None = None
    bytes_copied: int = 0
    created_dirs: list[Path] = field(default_factory=list)
    log_path: Path | None = None
    warnings: list[str] = field(default_factory=list)    # e.g. backup twins that differ

    def counts(self) -> Counter[Outcome]:
        return Counter(r.outcome for r in self.results.values())

    def item(self, item_id: int) -> PlanItem:
        return self.plan.items[item_id]

    def failed(self) -> list[tuple[PlanItem, ItemResult]]:
        return [(self.item(i), r) for i, r in self.results.items() if r.outcome is Outcome.FAILED]

    @property
    def renamed(self) -> int:
        return sum(1 for i, r in self.results.items()
                   if r.outcome is Outcome.COPIED and self.item(i).renamed)

    @property
    def folders(self) -> list[Path]:
        """Event folders that hold files from this import (copied or already there)."""
        seen: dict[str, Path] = {}
        for i, r in self.results.items():
            it = self.item(i)
            if r.outcome in (Outcome.COPIED, Outcome.ALREADY_THERE) and it.target is not None:
                seen.setdefault(key(str(it.target.path)), it.target.path)
        return sorted(seen.values())


_SKIP_OUTCOMES = {
    Action.SKIP_IMPORTED: Outcome.ALREADY_THERE,
    Action.SKIP_EXISTS: Outcome.ALREADY_THERE,
    Action.SKIP_DUPLICATE: Outcome.DUPLICATE,
    Action.SKIP_CULLED: Outcome.CULLED,
    Action.SKIP_DESELECTED: Outcome.NOT_IMPORTED,
    Action.UNKNOWN: Outcome.NOT_IMPORTED,
    Action.ORPHAN: Outcome.NOT_IMPORTED,
}


_OK = (Outcome.COPIED, Outcome.ALREADY_THERE)
# Main members whose copy is in the library after the run, so their twins can be checked.
_TWIN_MAIN_ACTIONS = (Action.COPY, Action.SKIP_IMPORTED, Action.SKIP_EXISTS)


def _card_names(a: SourceFile, b: SourceFile) -> tuple[str, str]:
    """Labels of two sources, with their roots when the labels are the same."""
    if a.source.label != b.source.label:
        return a.source.label, b.source.label
    return f"{a.source.label} ({a.source.root})", f"{b.source.label} ({b.source.root})"


def _variant_names(name: str) -> Iterator[str]:
    """``DSC_0001.JPG`` -> ``DSC_0001-v2.JPG``, ``-v3``, ... (the stem ends at the first dot)."""
    head, dot, tail = name.partition(".")
    head = _VARIANT.sub("", head)
    for n in range(2, MAX_TWIN_VARIANTS + 1):
        yield f"{head}-v{n}{dot}{tail}"


class _ReadError(Exception):
    def __init__(self, err: OSError):
        self.err = err


class _WriteError(Exception):
    def __init__(self, err: OSError):
        self.err = err


def _is_disk_full(e: OSError) -> bool:
    return e.errno in _DISK_FULL_ERRNOS or getattr(e, "winerror", None) in _DISK_FULL_WINERRORS


def _is_too_big(e: OSError) -> bool:
    return e.errno == errno.EFBIG or getattr(e, "winerror", None) in _TOO_BIG_WINERRORS


def _describe(e: OSError) -> str:
    return e.strerror or str(e)


class _Session:
    def __init__(self, plan: ImportPlan, ledger: Ledger, ops: FileOps, cancel: CancelToken,
                 emit: Emit) -> None:
        self.plan = plan
        self.ledger = ledger
        self.ops = ops
        self.cancel = cancel
        self.emit = emit
        self.sid6 = plan.session_id[-6:]
        self.mismatches = 0
        self.created: list[Path] = []
        self.known_dirs: set[Path] = set()
        self.gone_sources: set[str] = set()
        self.files_done = 0
        self.bytes_done = 0
        copies = plan.to_copy
        self.files_total = len(copies)
        self.bytes_total = sum(i.src.size for i in copies)
        self.throttle = Throttle()
        self.current = ""
        self.buf = bytearray(CHUNK)
        self.warnings: list[str] = []
        # Backup twins and their main member, plus the bytes their check will read.
        self.twins: list[tuple[PlanItem, PlanItem]] = []
        self.twin_bytes: dict[int, int] = {}
        self.checked_before: dict[int, Row] = {}
        self.main_digests: dict[int, str] = {}
        self._plan_twins()

    # -- backup twins -----------------------------------------------------------------------

    def _plan_twins(self) -> None:
        """Find the twins to check after the copy and add their reads to the totals."""
        hashed_mains: set[int] = set()
        for item in self.plan.items:
            if item.action is not Action.SKIP_DUPLICATE or item.dup_of is None:
                continue
            main = self.plan.items[item.dup_of]
            if main.action not in _TWIN_MAIN_ACTIONS:
                continue
            self.twins.append((item, main))
            self.files_total += 1
            row = self._checked_before(item, main) if main.action is not Action.COPY else None
            if row is not None:
                self.checked_before[item.id] = row
                continue
            n = item.src.size
            if main.action is not Action.COPY and main.sha256 is None \
                    and main.id not in hashed_mains:
                hashed_mains.add(main.id)
                n += main.src.size
            self.twin_bytes[item.id] = n
            self.bytes_total += n

    def _checked_before(self, twin: PlanItem, main: PlanItem) -> Row | None:
        """A history row showing this twin was found identical to the main's library file."""
        if main.target is None or main.dest_rel is None:
            return None
        cap = twin.capture
        try:
            rows = self.ledger.find(key(twin.src.name), twin.src.size,
                                    cap.local if cap else None)
        except Exception as e:
            log.debug("history lookup failed for %s: %s", twin.src.path, e)
            return None
        src = twin.src
        for row in rows:
            if (row.sha256 and row.dest_root == root_key(self.plan.dest_root)
                    and key(row.event) == key(main.target.folder)
                    and key(row.dest_rel) == key(main.dest_rel.as_posix())
                    and row.src_rel == src.rel.as_posix() and row.mtime_ns == src.mtime_ns
                    and row.card_label == src.source.label
                    and row.card_serial == src.source.serial
                    and (main.sha256 is None or row.sha256 == main.sha256)):
                return row
        return None

    def skip_twin(self, twin: PlanItem) -> None:
        """A planned twin check will not run: take it out of the progress totals."""
        self.bytes_total -= self.twin_bytes.pop(twin.id, 0)
        self.files_total -= 1

    def main_digest(self, main: PlanItem, res: ItemResult) -> str:
        """SHA-256 of the main member's library copy (hashed once when not yet known)."""
        if res.sha256:
            return res.sha256
        if main.id in self.main_digests:
            return self.main_digests[main.id]
        assert res.dest is not None
        st = self.ops.stat(res.dest)
        if is_online_only(st):
            # Do not download a cloud placeholder: compare with the main's card file instead.
            digest = self.hash_card_file(main.src)
        else:
            digest = self.ops.sha256(res.dest)
            self.progress(st.st_size)
            res.sha256 = digest
        self.main_digests[main.id] = digest
        return digest

    def check_twin(self, twin: PlanItem, main: PlanItem, main_res: ItemResult) -> ItemResult:
        """Confirm a backup twin by content before it is reported identical to its main.

        Equal hashes: DUPLICATE (and a history row, so later runs need not read it again).
        Different content: the twin is kept too, under a free ``-vN`` name next to the main.
        """
        assert main_res.dest is not None
        row = self.checked_before.get(twin.id)
        if row is not None:
            return ItemResult(twin.id, Outcome.DUPLICATE, dest=main_res.dest,
                              sha256=row.sha256, ledger_id=row.id)
        digest = self.hash_card_file(twin.src)
        try:
            main_sha = self.main_digest(main, main_res)
        except OSError as e:
            return ItemResult(twin.id, Outcome.FAILED,
                              error=f"could not compare with {main_res.dest.name} in the "
                                    f"library: {_describe(e)}")
        if digest == main_sha:
            row_id = None
            if twin.dest == main_res.dest:
                row_id = self.record(twin, digest, "adopted")
            return ItemResult(twin.id, Outcome.DUPLICATE, dest=main_res.dest, sha256=digest,
                              ledger_id=row_id)
        return self.keep_different_twin(twin, main, main_res, digest)

    def keep_different_twin(self, twin: PlanItem, main: PlanItem, main_res: ItemResult,
                            digest: str) -> ItemResult:
        """Put a twin whose bytes differ from its main next to it as ``-v2``, ``-v3``, ...

        A library file of that name with the twin's bytes (from an earlier run) is reused.
        The copy gets no history row: its card name, size and time equal the main's, so a
        row would let later runs take it for the main's copy.
        """
        assert main_res.dest is not None
        folder = main_res.dest.parent
        for name in _variant_names(main_res.dest.name):
            final = folder / name
            for _attempt in (1, 2):
                if self.ops.lexists(final):
                    try:
                        same = self.holds(final, twin.src.size, digest)
                    except OSError as e:
                        return ItemResult(twin.id, Outcome.FAILED,
                                          error=f"differs from its twin; {final.name} in the "
                                                f"library could not be compared: {_describe(e)}")
                    if not same:
                        break
                    note = self.twin_note(twin, main, main_res, final)
                    return ItemResult(twin.id, Outcome.ALREADY_THERE, dest=final, sha256=digest,
                                      error=note)
                self.bytes_total += twin.src.size
                try:
                    copied = self.write_verified(twin.src, final)
                except FileExistsError:
                    self.bytes_total -= twin.src.size
                    continue        # appeared meanwhile: compare it
                note = self.twin_note(twin, main, main_res, final)
                return ItemResult(twin.id, Outcome.COPIED, dest=final, sha256=copied, error=note)
        return ItemResult(twin.id, Outcome.FAILED,
                          error="differs from its twin, and no free -vN name is left for it")

    def holds(self, path: Path, size: int, digest: str) -> bool:
        """True when the library file ``path`` has exactly these bytes."""
        st = self.ops.stat(path)
        if st.st_size != size:
            return False
        if is_online_only(st):
            raise OSError(errno.EAGAIN, "it is online-only (cloud placeholder)", str(path))
        return self.ops.sha256(path) == digest

    def twin_note(self, twin: PlanItem, main: PlanItem, main_res: ItemResult,
                  final: Path) -> str:
        assert main_res.dest is not None
        a, b = _card_names(main.src, twin.src)
        text = (f"{twin.src.name}: the copies on {a} and {b} differ (same name and size, "
                f"different content); both are kept: {main_res.dest.name} and {final.name}")
        if text not in self.warnings:
            self.warnings.append(text)
            log.warning("%s (%s, %s)", text, main.src.path, twin.src.path)
            self.emit(Notice("warning", text))
        return f"differs from its twin on {a}; kept as {final.name}"

    # -- progress ---------------------------------------------------------------------------

    def progress(self, delta: int, force: bool = False) -> None:
        self.bytes_done += delta
        self.throttle.sample(self.bytes_done)
        if self.throttle.ready(force):
            self.emit(Progress(
                phase="copy", files_done=self.files_done, files_total=self.files_total,
                bytes_done=self.bytes_done, bytes_total=self.bytes_total, current=self.current,
                rate_bps=self.throttle.rate(),
                eta_s=self.throttle.eta(self.bytes_done, self.bytes_total)))

    # -- directories ------------------------------------------------------------------------

    def ensure_dir(self, d: Path) -> None:
        if d in self.known_dirs:
            return
        self.created.extend(self.ops.makedirs(d))
        self.known_dirs.add(d)

    def clean_stale_parts(self) -> None:
        now = datetime.now().timestamp()
        dirs = {i.dest.parent for i in self.plan.to_copy if i.dest is not None}
        for d in sorted(dirs):
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for p in entries:
                if not (p.name.startswith(".") and ".rji-" in p.name and p.name.endswith(".part")):
                    continue
                try:
                    if now - self.ops.stat(p).st_mtime > STALE_PART_AGE_S:
                        self.ops.unlink_quiet(p)
                except OSError:
                    pass

    # -- one file ---------------------------------------------------------------------------

    def copy_one(self, item: PlanItem, final: Path) -> ItemResult:
        try:
            digest = self.write_verified(item.src, final)
        except FileExistsError:
            return self.reresolve(item, final)
        row_id = self.record(item, digest, "copied")
        return ItemResult(item.id, Outcome.COPIED, dest=final, sha256=digest, ledger_id=row_id)

    def write_verified(self, f: SourceFile, final: Path) -> str:
        """Copy card file ``f`` to ``final`` (temp file, verify, rename); return its SHA-256.

        Raises FileExistsError when ``final`` exists before the copy or at the rename; an
        existing file is never touched.
        """
        src = f.path
        try:
            st0 = self.ops.stat(src)
        except OSError as e:
            raise _ReadError(e) from e
        if (st0.st_size, st0.st_mtime_ns) != (f.size, f.mtime_ns):
            raise SourceChanged("changed since the card was scanned; scan again")
        try:
            self.ensure_dir(final.parent)
        except OSError as e:      # no permission, disk full, a file where a folder should be
            raise _WriteError(e) from e
        if self.ops.lexists(final):
            raise FileExistsError(errno.EEXIST, "exists", str(final))
        tmp = final.with_name(f".{final.name}.rji-{self.sid6}.part")
        mv = memoryview(self.buf)
        digest = ""
        for _attempt in (1, 2):
            h = hashlib.sha256()
            n = 0
            try:
                try:
                    fi = self.ops.open_read(src)
                except OSError as e:
                    raise _ReadError(e) from e
                with fi:
                    try:
                        fo = self.ops.open_excl(tmp)
                    except OSError as e:
                        raise _WriteError(e) from e
                    with fo:
                        while True:
                            try:
                                k = fi.readinto(mv)
                            except OSError as e:
                                raise _ReadError(e) from e
                            if not k:
                                break
                            self.cancel.check()
                            chunk = mv[:k]
                            h.update(chunk)
                            try:
                                fo.write(chunk)
                            except OSError as e:
                                raise _WriteError(e) from e
                            n += k
                            self.progress(k)
                        try:
                            fo.flush()
                            self.ops.fsync(fo)
                        except OSError as e:
                            raise _WriteError(e) from e
                try:
                    st1 = self.ops.stat(src)
                except OSError as e:
                    raise _ReadError(e) from e
                if not (n == st0.st_size == st1.st_size and st1.st_mtime_ns == st0.st_mtime_ns):
                    raise SourceChanged("changed while copying; scan again")
                digest = h.hexdigest()
                try:
                    copy_digest = self.ops.sha256(tmp)
                except OSError as e:
                    raise _WriteError(e) from e
                if copy_digest == digest:
                    break
                self.ops.unlink_quiet(tmp)
                self.progress(-n)
                self.mismatches += 1
                log.warning("checksum mismatch copying %s (attempt %d)", src, _attempt)
                if self.mismatches >= MAX_MISMATCHES:
                    raise HardwareSuspect(
                        "Copies keep failing verification; check the card reader and cable")
            except BaseException:
                self.ops.unlink_quiet(tmp)
                raise
        else:
            raise VerifyMismatch("the copy did not match the card after 2 attempts")

        try:
            self.ops.utime(tmp, (st0.st_atime_ns, st0.st_mtime_ns))
        except OSError as e:
            self.ops.unlink_quiet(tmp)
            raise _WriteError(e) from e
        try:
            self.ops.rename_noreplace(tmp, final)
        except FileExistsError:
            self.ops.unlink_quiet(tmp)
            raise
        except OSError as e:
            self.ops.unlink_quiet(tmp)
            raise _WriteError(e) from e
        self.ops.fsync_dir(final.parent)
        return digest

    def hash_card_file(self, f: SourceFile) -> str:
        """SHA-256 of a card file, read like a copy: cancellable and counted in the progress."""
        try:
            st0 = self.ops.stat(f.path)
        except OSError as e:
            raise _ReadError(e) from e
        if (st0.st_size, st0.st_mtime_ns) != (f.size, f.mtime_ns):
            raise SourceChanged("changed since the card was scanned; scan again")
        mv = memoryview(self.buf)
        h = hashlib.sha256()
        n = 0
        try:
            fi = self.ops.open_read(f.path)
        except OSError as e:
            raise _ReadError(e) from e
        with fi:
            while True:
                try:
                    k = fi.readinto(mv)
                except OSError as e:
                    raise _ReadError(e) from e
                if not k:
                    break
                self.cancel.check()
                h.update(mv[:k])
                n += k
                self.progress(k)
        try:
            st1 = self.ops.stat(f.path)
        except OSError as e:
            raise _ReadError(e) from e
        if not (n == st0.st_size == st1.st_size and st1.st_mtime_ns == st0.st_mtime_ns):
            raise SourceChanged("changed while reading; scan again")
        return h.hexdigest()

    def reresolve(self, item: PlanItem, final: Path) -> ItemResult:
        """The destination name appeared after planning: skip it if it holds the card file's
        bytes (compared by hash, never by size and time alone), else fail."""
        unchecked = ItemResult(item.id, Outcome.FAILED,
                               error=f"{final.name} could not be checked; run the import again")
        try:
            st = self.ops.stat(final)
        except OSError:
            return unchecked
        if st.st_size == item.src.size:
            if is_online_only(st):
                return unchecked
            try:
                existing = self.ops.sha256(final)
            except OSError:
                return unchecked
            digest = self.hash_card_file(item.src)
            if digest == existing:
                row_id = self.record(item, digest, "adopted")
                return ItemResult(item.id, Outcome.ALREADY_THERE, dest=final, sha256=digest,
                                  ledger_id=row_id)
        return ItemResult(item.id, Outcome.FAILED,
                          error=f"a different file named {final.name} appeared since planning; "
                                "run the import again")

    def record(self, item: PlanItem, digest: str | None, kind: str) -> int | None:
        assert item.target is not None and item.dest_rel is not None
        cap = item.capture
        row = Row(
            id=None, session=self.plan.session_id, kind=kind,
            dest_root=root_key(self.plan.dest_root), event=item.target.folder,
            dest_rel=item.dest_rel.as_posix(), orig_key=key(item.src.name),
            size=item.src.size, mtime_ns=item.src.mtime_ns,
            capture=cap.local.replace(microsecond=0).isoformat() if cap else None,
            capture_exact=bool(cap and cap.exact), subsec=cap.subsec_ms if cap else None,
            shot_stem_key=key(item.stem_used or item.src.stem), sha256=digest,
            card_label=item.src.source.label, card_serial=item.src.source.serial,
            src_rel=item.src.rel.as_posix())
        try:
            return self.ledger.record(row)
        except Exception as e:     # the copy is safe on disk; the ledger only adds knowledge
            log.error("could not record %s in the import history: %s", item.dest_rel, e)
            return None

    # -- whole plan -------------------------------------------------------------------------

    def run_item(self, item: PlanItem) -> ItemResult:
        """Copy one item, translating errors into a FAILED result (or re-raising aborts)."""
        final = item.dest
        assert final is not None
        return self.guarded(item, final, lambda: self.copy_one(item, final))

    def guarded(self, item: PlanItem, final: Path,
                work: Callable[[], ItemResult]) -> ItemResult:
        """Run ``work`` for ``item``; errors become a FAILED result (aborts are re-raised)."""
        src_key = item.src.source.key
        if src_key in self.gone_sources:
            return ItemResult(item.id, Outcome.FAILED, error="card removed")
        try:
            return work()
        except _ReadError as e:
            if not _exists(item.src.source.root):
                self.gone_sources.add(src_key)
                return ItemResult(item.id, Outcome.FAILED, error="card removed")
            return ItemResult(item.id, Outcome.FAILED,
                              error=f"could not read from the card: {_describe(e.err)}")
        except _WriteError as e:
            if _is_too_big(e.err) or (_is_disk_full(e.err) and self.past_fat_limit(item, final)):
                return ItemResult(item.id, Outcome.FAILED,
                                  error="file is too large for the destination drive "
                                        "(FAT32 has a 4 GB file limit)")
            if _is_disk_full(e.err):
                raise DestFull("The destination drive is full") from e.err
            return ItemResult(item.id, Outcome.FAILED,
                              error=f"could not write {final}: {_describe(e.err)}")
        except (SourceChanged, VerifyMismatch) as e:
            return ItemResult(item.id, Outcome.FAILED, error=str(e))

    def past_fat_limit(self, item: PlanItem, final: Path) -> bool:
        """"Disk full" for a file over 4 GiB on a drive with room for it: the FAT32 limit.

        Windows' FAT driver reports a write past 4 GiB - 1 as ERROR_DISK_FULL, not as
        "file too large" like Linux and macOS do.
        """
        if item.src.size <= FAT32_MAX_FILE:
            return False
        try:
            free = self.ops.disk_free(final.parent)    # the failed temp file is gone again
        except (OSError, ValueError):
            return False
        return free >= item.src.size


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except OSError:
        return False


def execute(plan: ImportPlan, *, ledger: Ledger, ops: FileOps | None = None,
            cancel: CancelToken = NEVER_CANCELLED, emit: Emit = no_emit) -> ImportReport:
    """Run ``plan``. The caller holds the import lock. Never touches files on the cards
    except to read them."""
    if plan.problems:
        raise PlanBlocked(plan.problems)
    ops = ops or RealFileOps()
    s = _Session(plan, ledger, ops, cancel, emit)
    started = datetime.now()
    results: dict[int, ItemResult] = {}
    for item in plan.items:
        if item.action is not Action.COPY:
            results[item.id] = ItemResult(item.id, _SKIP_OUTCOMES[item.action],
                                          dest=item.existing or item.dest,
                                          sha256=item.sha256, ledger_id=item.ledger_id)
    status: Status = "ok"
    abort_reason: str | None = None
    copies = sorted(plan.to_copy, key=lambda i: (i.src.source.order, i.src.rel.as_posix()))

    with keep_awake(), no_error_dialogs():
        s.clean_stale_parts()
        s.progress(0, force=True)
        for n, item in enumerate(copies):
            s.current = item.src.name
            try:
                cancel.check()
                res = s.run_item(item)
            except Cancelled:
                status = "cancelled"
                for rest in copies[n:]:
                    results[rest.id] = ItemResult(rest.id, Outcome.CANCELLED)
                break
            except (DestFull, HardwareSuspect) as e:
                status, abort_reason = "aborted", str(e)
                results[item.id] = ItemResult(item.id, Outcome.FAILED, error=str(e))
                for rest in copies[n + 1:]:
                    results[rest.id] = ItemResult(rest.id, Outcome.CANCELLED, error=str(e))
                break
            results[item.id] = res
            s.files_done += 1
            emit(FileDone(item.id, res.outcome.name, res.error or ""))
            s.progress(0, force=s.files_done == s.files_total)

        if status == "ok":
            try:
                _rescue_with_duplicates(plan, s, results, emit)
            except Cancelled:
                status = "cancelled"
            except (DestFull, HardwareSuspect) as e:
                status, abort_reason = "aborted", str(e)
        _adopt_existing(plan, s, results)
        _finish_duplicates(plan, results)
        try:
            _check_twins(s, results, emit, read=status == "ok")
        except Cancelled:
            status = "cancelled"
        except (DestFull, HardwareSuspect) as e:
            status, abort_reason = "aborted", str(e)
        s.progress(0, force=True)
        for d in reversed(s.created):
            ops.rmdir_quiet(d)

    if status == "ok" and any(r.outcome is Outcome.FAILED for r in results.values()):
        status = "partial"
    created_left = [d for d in s.created if _exists(d)]
    return ImportReport(plan=plan, results=results, status=status, started=started,
                        finished=datetime.now(), abort_reason=abort_reason,
                        bytes_copied=sum(plan.items[i].src.size for i, r in results.items()
                                         if r.outcome is Outcome.COPIED),
                        created_dirs=created_left, warnings=list(s.warnings))


def _rescue_with_duplicates(plan: ImportPlan, s: _Session, results: dict[int, ItemResult],
                            emit: Emit) -> None:
    """A main file failed to copy: try its identical twin from the other card.

    Re-raises Cancelled, DestFull and HardwareSuspect (after recording the twin being
    copied) so the caller can end the session like the main copy loop does.
    """
    for item in plan.items:
        if item.action is not Action.SKIP_DUPLICATE or item.dup_of is None:
            continue
        main = results.get(item.dup_of)
        if main is None or main.outcome is not Outcome.FAILED:
            continue
        if any(r.outcome is Outcome.COPIED and plan.items[i].dup_of == item.dup_of
               for i, r in results.items() if i != item.id):
            continue      # another twin already rescued this shot
        if item.dest is None:
            continue
        s.current = item.src.name
        try:
            res = s.run_item(item)
        except Cancelled:
            results[item.id] = ItemResult(item.id, Outcome.CANCELLED)
            raise
        except (DestFull, HardwareSuspect) as e:
            results[item.id] = ItemResult(item.id, Outcome.FAILED, error=str(e))
            raise
        if res.outcome is Outcome.COPIED:
            res.error = None
        results[item.id] = res
        emit(FileDone(item.id, res.outcome.name, res.error or ""))


def _adopt_existing(plan: ImportPlan, s: _Session, results: dict[int, ItemResult]) -> None:
    """Identical files found in the library without a ledger record get one now."""
    for item in plan.items:
        if item.action is Action.SKIP_EXISTS:
            res = results[item.id]
            if res.ledger_id is None:
                res.ledger_id = s.record(item, None, "adopted")


def _check_twins(s: _Session, results: dict[int, ItemResult], emit: Emit, *,
                 read: bool) -> None:
    """Confirm every backup twin whose main member is in the library (see check_twin).

    With ``read`` False (the import was cancelled or stopped) only twins the history already
    vouches for are confirmed; the others are reported as not copied. Re-raises Cancelled,
    DestFull and HardwareSuspect after marking the twins left unchecked.
    """
    def main_ok(main: PlanItem) -> ItemResult | None:
        res = results.get(main.id)
        return res if res is not None and res.outcome in _OK and res.dest is not None else None

    def stop(rest: list[tuple[PlanItem, PlanItem]], error: str | None) -> None:
        for t, m in rest:
            if main_ok(m) is not None:
                results[t.id] = ItemResult(t.id, Outcome.CANCELLED, error=error)

    pending = [(t, m) for t, m in s.twins if results[t.id].outcome is Outcome.DUPLICATE]
    for n, (twin, main) in enumerate(pending):
        main_res = main_ok(main)
        if main_res is None:
            s.skip_twin(twin)       # nothing in the library to compare with: left as it was
            continue
        if not read and twin.id not in s.checked_before:
            s.skip_twin(twin)
            results[twin.id] = ItemResult(twin.id, Outcome.CANCELLED,
                                          error="not compared with its twin (import stopped)")
            continue
        assert main_res.dest is not None
        s.current = twin.src.name
        try:
            s.cancel.check()
            res = s.guarded(twin, main_res.dest.parent,
                            lambda t=twin, m=main, r=main_res: s.check_twin(t, m, r))
        except Cancelled:
            stop(pending[n:], None)
            raise
        except (DestFull, HardwareSuspect) as e:
            stop(pending[n + 1:], str(e))
            results[twin.id] = ItemResult(twin.id, Outcome.FAILED, error=str(e))
            raise
        s.twin_bytes.pop(twin.id, None)
        results[twin.id] = res
        s.files_done += 1
        emit(FileDone(twin.id, res.outcome.name, res.error or ""))
        s.progress(0)


def _finish_duplicates(plan: ImportPlan, results: dict[int, ItemResult]) -> None:
    for item in plan.items:
        if item.action is not Action.SKIP_DUPLICATE or item.dup_of is None:
            continue
        res = results[item.id]
        if res.outcome is not Outcome.DUPLICATE:
            continue
        main = results.get(item.dup_of)
        if main is not None:
            res.dest = main.dest
            res.sha256 = main.sha256
