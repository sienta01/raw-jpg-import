"""Opt-in: delete card files whose copies are verified.

The only module that removes files. A card file is deleted only when all of these hold:
- the import put it (or an identical twin) in the library: COPIED, ALREADY_THERE or a
  DUPLICATE of one of those;
- it is unchanged since the scan, not protected in the camera, and its library copy exists
  locally (not an online-only cloud placeholder) with the same size;
- a FRESH read of both the library copy and the card file gives equal SHA-256 hashes, also
  equal to the hash recorded at import time when there is one.
Folders and camera system files are never touched.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from .copier import ImportReport, Outcome
from .errors import Cancelled
from .events import NEVER_CANCELLED, CancelToken, Emit, FileDone, Progress, Throttle, no_emit
from .fsops import (
    FileOps,
    RealFileOps,
    is_online_only,
    is_readonly,
    same_identity,
    same_path,
)
from .ledger import Ledger
from .models import Source, SourceFile
from .planner import Action
from .volumes import destination_notes, no_error_dialogs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeleteCandidate:
    item_id: int
    src: SourceFile
    dest: Path
    recorded_sha: str | None
    ledger_id: int | None


@dataclass
class DeletionPlan:
    candidates: list[DeleteCandidate]
    excluded: list[tuple[SourceFile, str]]
    bytes: int
    # source name -> (files, bytes); the name is the label, plus the root when two sources
    # share a label (Windows gives both cards of a Zf the label "NIKON Z F")
    per_source: dict[str, tuple[int, int]]
    warnings: list[str] = field(default_factory=list)    # e.g. the library syncs to a cloud


class DeleteOutcome(Enum):
    DELETED = "deleted"
    KEPT = "kept"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DeleteResult:
    src: SourceFile
    outcome: DeleteOutcome
    reason: str = ""


@dataclass
class DeletionReport:
    results: list[DeleteResult] = field(default_factory=list)
    status: Literal["ok", "partial", "cancelled"] = "ok"
    bytes_freed: int = 0

    def counts(self) -> Counter[DeleteOutcome]:
        return Counter(r.outcome for r in self.results)


_EXCLUDED_REASONS = {
    Outcome.FAILED: "not deleted: the copy failed",
    Outcome.CANCELLED: "not deleted: not copied (import cancelled)",
    Outcome.CULLED: "not deleted: no copy in the library (you removed it)",
}

_NOT_IMPORTED_REASONS = {
    Action.UNKNOWN: "not deleted: unknown file type, not imported",
    Action.ORPHAN: "not deleted: sidecar without a photo, not imported",
    Action.SKIP_DESELECTED: "not deleted: its day was not imported",
}


def source_names(sources: Iterable[Source]) -> dict[str, str]:
    """``Source.key`` -> what to call the source: its label, or ``label (root)`` when
    another source has the same label."""
    by_label: dict[str, set[str]] = {}
    for s in sources:
        by_label.setdefault(s.label, set()).add(s.key)
    return {s.key: s.label if len(by_label[s.label]) == 1 else f"{s.label} ({s.root})"
            for s in sources}


def plan_deletion(report: ImportReport) -> DeletionPlan:
    """Which card files may be offered for deletion after ``report``."""
    plan = report.plan
    candidates: list[DeleteCandidate] = []
    excluded: list[tuple[SourceFile, str]] = []
    for item in plan.items:
        res = report.results.get(item.id)
        if res is None:
            continue
        reason: str | None = None
        dest: Path | None = None
        sha = res.sha256
        ledger_id = res.ledger_id
        if res.outcome in (Outcome.COPIED, Outcome.ALREADY_THERE):
            dest = res.dest or item.existing or item.dest
        elif res.outcome is Outcome.DUPLICATE:
            # The copier compared the twin's bytes with its main's copy (res.sha256).
            main = report.results.get(item.dup_of) if item.dup_of is not None else None
            if main is not None and main.outcome in (Outcome.COPIED, Outcome.ALREADY_THERE):
                dest, sha = res.dest or main.dest, res.sha256 or main.sha256
            else:
                reason = "not deleted: its twin on the other card was not copied"
        elif res.outcome is Outcome.NOT_IMPORTED:
            reason = _NOT_IMPORTED_REASONS.get(item.action, "not deleted: not imported")
        else:
            reason = _EXCLUDED_REASONS.get(res.outcome, "not deleted")
        if reason is None and dest is None:
            reason = "not deleted: copy location unknown"
        if reason is None and item.src.readonly:
            reason = "not deleted: protected in the camera"
        if reason is not None:
            excluded.append((item.src, reason))
            continue
        assert dest is not None
        candidates.append(DeleteCandidate(item.id, item.src, dest, sha, ledger_id))
    names = source_names([*plan.sources, *(c.src.source for c in candidates)])
    per_source: dict[str, tuple[int, int]] = {}
    for c in candidates:
        name = names[c.src.source.key]
        n, b = per_source.get(name, (0, 0))
        per_source[name] = (n + 1, b + c.src.size)
    return DeletionPlan(candidates=candidates, excluded=excluded,
                        bytes=sum(c.src.size for c in candidates), per_source=per_source,
                        warnings=destination_notes(plan.dest_root))


def _check_and_delete(c: DeleteCandidate, ops: FileOps,
                      on_bytes: Callable[[int], None] | None = None) -> DeleteResult:
    src = c.src
    try:
        st = ops.stat(src.path)
    except FileNotFoundError:
        return DeleteResult(src, DeleteOutcome.KEPT, "already gone from the card")
    except OSError as e:
        return DeleteResult(src, DeleteOutcome.FAILED, f"cannot read the card: {e.strerror or e}")
    if (st.st_size, st.st_mtime_ns) != (src.size, src.mtime_ns):
        return DeleteResult(src, DeleteOutcome.KEPT, "changed on the card since the import")
    if is_readonly(st) or src.readonly:
        return DeleteResult(src, DeleteOutcome.KEPT, "protected in the camera")
    try:
        dst = ops.stat(c.dest)
    except OSError:
        return DeleteResult(src, DeleteOutcome.KEPT, "the copy is missing from the library")
    # A folder source that reaches the library (junction, hard link, another spelling of
    # the path) makes the "card file" and its copy one file: removing it loses both.
    if same_identity(st, dst) or same_path(src.path, c.dest):
        return DeleteResult(src, DeleteOutcome.KEPT,
                            "the original and the copy are the same file")
    if dst.st_size != src.size:
        return DeleteResult(src, DeleteOutcome.KEPT, "the copy has a different size")
    if is_online_only(dst):
        return DeleteResult(src, DeleteOutcome.KEPT,
                            "the copy is online-only (cloud placeholder); make it available "
                            "offline first")
    try:
        dest_sha = ops.sha256(c.dest, on_bytes)
    except OSError as e:
        return DeleteResult(src, DeleteOutcome.KEPT, f"cannot read the copy: {e.strerror or e}")
    try:
        src_sha = ops.sha256(src.path, on_bytes)
    except OSError as e:
        return DeleteResult(src, DeleteOutcome.FAILED, f"cannot read the card: {e.strerror or e}")
    if src_sha != dest_sha or (c.recorded_sha is not None and c.recorded_sha != dest_sha):
        return DeleteResult(src, DeleteOutcome.KEPT,
                            "the copy does not match the card; not deleted. Delete the copy "
                            "and import again")
    try:
        ops.remove(src.path)
    except OSError as e:
        return DeleteResult(src, DeleteOutcome.FAILED, f"could not delete: {e.strerror or e}")
    return DeleteResult(src, DeleteOutcome.DELETED)


def delete_originals(dplan: DeletionPlan, *, confirmed: bool, ledger: Ledger | None = None,
                     ops: FileOps | None = None, cancel: CancelToken = NEVER_CANCELLED,
                     emit: Emit = no_emit) -> DeletionReport:
    """Delete verified card files. Raises ``ValueError`` unless ``confirmed`` is True.

    Cancellation is honoured between files only. The caller holds the import lock.
    """
    if not confirmed:
        raise ValueError("delete_originals needs confirmed=True")
    ops = ops or RealFileOps()
    report = DeletionReport()
    throttle = Throttle()
    total = len(dplan.candidates)
    bytes_total = 2 * dplan.bytes
    bytes_done = 0
    for n, c in enumerate(dplan.candidates):
        try:
            cancel.check()
        except Cancelled:
            report.status = "cancelled"
            for rest in dplan.candidates[n:]:
                report.results.append(DeleteResult(rest.src, DeleteOutcome.CANCELLED))
            break
        start = bytes_done
        read = 0

        def on_bytes(k: int, n: int = n, c: DeleteCandidate = c, start: int = start) -> None:
            # Both reads of a big file over a slow link can take minutes: keep the bar moving.
            nonlocal read
            read += k
            throttle.sample(start + read)
            if throttle.ready():
                emit(Progress(phase="delete", files_done=n, files_total=total,
                              bytes_done=start + read, bytes_total=bytes_total,
                              current=c.src.name, rate_bps=throttle.rate(),
                              eta_s=throttle.eta(start + read, bytes_total)))

        if throttle.ready():
            emit(Progress(phase="delete", files_done=n, files_total=total, bytes_done=bytes_done,
                          bytes_total=bytes_total, current=c.src.name))
        with no_error_dialogs():        # a pulled card fails; no "insert a disk" box
            res = _check_and_delete(c, ops, on_bytes)
        bytes_done = start + 2 * c.src.size
        throttle.sample(bytes_done)
        report.results.append(res)
        if res.outcome is DeleteOutcome.DELETED:
            report.bytes_freed += c.src.size
            log.info("deleted from card: %s (copy: %s)", c.src.path, c.dest)
            if ledger is not None and c.ledger_id is not None:
                try:
                    ledger.mark_deleted(c.ledger_id)
                except Exception as e:
                    log.error("could not mark %s deleted in the history: %s", c.src.path, e)
        else:
            log.info("kept on card: %s (%s)", c.src.path, res.reason)
        emit(FileDone(c.item_id, res.outcome.name, res.reason))
    emit(Progress(phase="delete", files_done=total, files_total=total, bytes_done=bytes_total,
                  bytes_total=bytes_total))
    if report.status == "ok" and any(r.outcome is DeleteOutcome.FAILED for r in report.results):
        report.status = "partial"
    return report
