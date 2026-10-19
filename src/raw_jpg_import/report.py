"""Human-readable summaries and per-session log files."""

from __future__ import annotations

import logging
import logging.handlers
import os
from collections import Counter
from pathlib import Path

from . import appdirs
from .copier import ImportReport, Outcome
from .deleter import DeleteOutcome, DeletionPlan, DeletionReport
from .planner import Action, ImportPlan

KEEP_SESSION_LOGS = 50


def fmt_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {s:02d} s"
    hours, m = divmod(minutes, 60)
    return f"{hours} h {m:02d} min"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def plan_summary_lines(plan: ImportPlan) -> list[str]:
    c = plan.counts()
    lines = [f"Destination: {plan.dest_root}"]
    for t in plan.targets:
        state = "EXISTING" if t.target.exists else "NEW"
        parts = [f"{folder} {n}" for folder, (n, _) in t.per_folder.items()]
        extra = f", {t.renamed} renamed" if t.renamed else ""
        lines.append(f"  {state:8} {t.target.folder}   "
                     f"{' · '.join(parts) or 'nothing new'}{extra}")
    skipped = Counter({a: n for a, n in c.items() if a is not Action.COPY})
    lines.append(f"Copy {c.get(Action.COPY, 0)} files ({fmt_bytes(plan.bytes_to_copy)})"
                 + (f" of {fmt_bytes(plan.free_bytes)} free" if plan.free_bytes else ""))
    for a in Action:
        if a is not Action.COPY and skipped.get(a):
            lines.append(f"  {a.value}: {skipped[a]}")
    if plan.renamed:
        lines.append(f"  renamed to avoid a clash: {plan.renamed}")
    lines.extend(f"Note: {w}" for w in plan.warnings)
    lines.extend(f"PROBLEM: {p}" for p in plan.problems)
    return lines


def import_summary_lines(report: ImportReport) -> list[str]:
    c = report.counts()
    secs = (report.finished - report.started).total_seconds()
    head = {
        "ok": "Import finished.",
        "partial": f"Import finished with {plural(c.get(Outcome.FAILED, 0), 'error')}.",
        "cancelled": "Import cancelled. Files already copied stay; run the import again to finish.",
        "aborted": f"Import stopped: {report.abort_reason}",
    }[report.status]
    lines = [head,
             f"Copied and verified: {c.get(Outcome.COPIED, 0)} files "
             f"({fmt_bytes(report.bytes_copied)}) in {fmt_duration(secs)}"]
    labels = [
        (Outcome.ALREADY_THERE, "Already in the library"),
        (Outcome.DUPLICATE, "Identical on both cards (copied once)"),
        (Outcome.CULLED, "Removed from the library earlier (not re-imported)"),
        (Outcome.NOT_IMPORTED, "Not imported (unknown type / day not selected)"),
        (Outcome.CANCELLED, "Not copied (cancelled)"),
        (Outcome.FAILED, "Failed"),
    ]
    for outcome, label in labels:
        if c.get(outcome):
            lines.append(f"{label}: {c[outcome]}")
    if report.renamed:
        lines.append(f"Renamed to avoid a name clash: {report.renamed}")
    for item, res in report.failed()[:50]:
        lines.append(f"  FAILED {item.src.source.label}: {item.src.rel}: {res.error}")
    for folder in report.folders:
        lines.append(f"Folder: {folder}")
    return lines


def deletion_plan_lines(dplan: DeletionPlan) -> list[str]:
    lines = [f"Delete {len(dplan.candidates)} files ({fmt_bytes(dplan.bytes)}) from the cards:"]
    for label, (n, b) in sorted(dplan.per_source.items()):
        lines.append(f"  {label}: {n} files ({fmt_bytes(b)})")
    reasons = Counter(r for _, r in dplan.excluded)
    for reason, n in reasons.most_common():
        lines.append(f"  kept {n}: {reason}")
    lines.extend(f"Warning: {w}" for w in dplan.warnings)
    return lines


def deletion_summary_lines(dreport: DeletionReport) -> list[str]:
    c = dreport.counts()
    lines = [f"Deleted from cards: {c.get(DeleteOutcome.DELETED, 0)} files "
             f"({fmt_bytes(dreport.bytes_freed)} freed)"]
    if c.get(DeleteOutcome.KEPT):
        lines.append(f"Kept on cards: {c[DeleteOutcome.KEPT]}")
    if c.get(DeleteOutcome.FAILED):
        lines.append(f"Could not delete: {c[DeleteOutcome.FAILED]}")
    if c.get(DeleteOutcome.CANCELLED):
        lines.append(f"Not checked (cancelled): {c[DeleteOutcome.CANCELLED]}")
    for r in dreport.results:
        if r.outcome in (DeleteOutcome.KEPT, DeleteOutcome.FAILED):
            lines.append(f"  {r.outcome.value} {r.src.source.label}: {r.src.rel}: {r.reason}")
    return lines


def write_session_log(report: ImportReport, deletion: DeletionReport | None = None,
                      log_dir: Path | None = None) -> Path | None:
    """Write ``import-<session>.log`` with every file's outcome; keep the newest 50 logs."""
    d = log_dir or appdirs.log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"import-{report.plan.session_id}.log"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"raw-jpg-import session {report.plan.session_id}\n")
            f.write(f"started {report.started:%Y-%m-%d %H:%M:%S}, "
                    f"finished {report.finished:%Y-%m-%d %H:%M:%S}, status {report.status}\n")
            for src in report.plan.sources:
                f.write(f"source: {src.label} ({src.root})\n")
            f.write("\n".join(plan_summary_lines(report.plan)) + "\n\n")
            f.write("\n".join(import_summary_lines(report)) + "\n\n")
            for item in report.plan.items:
                res = report.results.get(item.id)
                outcome = res.outcome.value if res else "?"
                dest = res.dest if res and res.dest else item.dest
                err = f"  [{res.error}]" if res and res.error else ""
                f.write(f"{outcome:14} {item.src.source.label}:{item.src.rel} -> "
                        f"{dest or '-'}{err}\n")
            if deletion is not None:
                f.write("\n" + "\n".join(deletion_summary_lines(deletion)) + "\n")
        _rotate(d)
        return path
    except OSError as e:
        logging.getLogger(__name__).error("could not write the session log: %s", e)
        return None


def append_deletion_log(path: Path | None, deletion: DeletionReport) -> None:
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n" + "\n".join(deletion_summary_lines(deletion)) + "\n")
            for r in deletion.results:
                f.write(f"{r.outcome.value:10} {r.src.source.label}:{r.src.rel} {r.reason}\n")
    except OSError as e:
        logging.getLogger(__name__).error("could not append to the session log: %s", e)


def _rotate(d: Path) -> None:
    logs = sorted(d.glob("import-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[KEEP_SESSION_LOGS:]:
        try:
            os.remove(old)
        except OSError:
            pass


def configure_logging(verbose: bool = False, console: bool = False) -> Path | None:
    """App log (``raw-jpg-import.log``, rotated) plus optional console output."""
    root = logging.getLogger("raw_jpg_import")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()       # releases the previous log file (a leaked handle blocks deleting it)
    path: Path | None = None
    try:
        d = appdirs.log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "raw-jpg-import.log"
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        path = None
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(ch)
    return path
