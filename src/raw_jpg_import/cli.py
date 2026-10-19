"""Command-line interface: ``raw-jpg-import <command>``.

Exit codes: 0 ok, 1 finished with failures, 2 usage/config error or blocked plan,
3 another import is running, 130 cancelled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, time
from pathlib import Path
from typing import Any, TextIO

from . import __version__, appdirs
from .errors import AnotherImportRunning, Cancelled, ConfigError, PlanBlocked

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_CANCELLED = 0, 1, 2, 3, 130


class UsageError(Exception):
    """Bad arguments or missing input; exit code 2."""


# ---- progress output ------------------------------------------------------------------------


class ProgressPrinter:
    """One ``\\r`` line on stderr, only when stderr is a terminal."""

    PHASES = {"scan": "Scanning", "metadata": "Reading dates", "plan": "Planning",
              "copy": "Copying", "verify": "Verifying", "delete": "Deleting"}

    def __init__(self, stream: TextIO | None = None, enabled: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.width = 0
        self.lock = threading.Lock()

    def __call__(self, event: object) -> None:
        from .events import Notice, Progress
        from .report import fmt_bytes, fmt_duration

        if isinstance(event, Notice) and event.level != "info":
            self.clear()
            print(f"{event.level}: {event.text}", file=self.stream)
            return
        if not self.enabled or not isinstance(event, Progress):
            return
        label = self.PHASES.get(event.phase, event.phase)
        if event.files_total:
            frac = (event.bytes_done / event.bytes_total) if event.bytes_total else \
                event.files_done / event.files_total
            frac = min(max(frac, 0.0), 1.0)
            bar = "#" * int(frac * 20) + "-" * (20 - int(frac * 20))
            text = f"{label} [{bar}] {event.files_done}/{event.files_total} files"
            if event.bytes_total:
                text += f" · {fmt_bytes(event.bytes_done)}/{fmt_bytes(event.bytes_total)}"
            if event.rate_bps:
                text += f" · {fmt_bytes(event.rate_bps)}/s"
            if event.eta_s is not None:
                text += f" · about {fmt_duration(event.eta_s)} left"
        else:
            text = f"{label}: {event.files_done} files"
            if event.current:
                text += f" · {event.current[-40:]}"
        self._write(text)

    def _write(self, text: str) -> None:
        with self.lock:
            pad = max(0, self.width - len(text))
            self.stream.write("\r" + text + " " * pad)
            self.stream.flush()
            self.width = len(text)

    def clear(self) -> None:
        if self.enabled and self.width:
            with self.lock:
                self.stream.write("\r" + " " * self.width + "\r")
                self.stream.flush()
                self.width = 0


# ---- running engine work with Ctrl+C support -------------------------------------------------


def run_cancellable(fn: Callable[..., Any], *args: Any, printer: ProgressPrinter | None = None,
                    **kw: Any) -> Any:
    """Run ``fn(*args, cancel=..., emit=..., **kw)`` on a worker thread.

    The first Ctrl+C asks the job to cancel cleanly; a second one exits immediately (130).
    """
    from .events import CancelToken

    cancel = CancelToken()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = fn(*args, cancel=cancel, emit=printer or (lambda e: None), **kw)
        except BaseException as e:   # re-raised on the main thread
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    interrupted = False
    while t.is_alive():
        try:
            t.join(0.2)
        except KeyboardInterrupt:
            if interrupted:
                if printer:
                    printer.clear()
                print("\nStopped.", file=sys.stderr)
                os._exit(EXIT_CANCELLED)
            interrupted = True
            cancel.cancel()
            if printer:
                printer.clear()
            print("\nCancelling... (press Ctrl+C again to stop immediately)", file=sys.stderr)
    if printer:
        printer.clear()
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ---- helpers --------------------------------------------------------------------------------


def _say(args: argparse.Namespace, *lines: str) -> None:
    if getattr(args, "quiet", False) or getattr(args, "json", False):
        return
    for line in lines:
        print(line)


def _parse_day_start(text: str) -> time:
    from .engine import parse_day_start

    try:
        return parse_day_start(text)
    except ValueError as e:
        raise UsageError(str(e)) from None


def _parse_date(text: str) -> date:
    from .naming import parse_user_date

    try:
        return parse_user_date(text)
    except ValueError as e:
        raise UsageError(str(e)) from None


def _sources(args: argparse.Namespace, printer: ProgressPrinter) -> list:
    from . import engine

    if args.sources:
        srcs = []
        for i, s in enumerate(args.sources):
            p = Path(s)
            if not p.is_dir():
                raise UsageError(f"not a folder: {s}")
            srcs.append(engine.make_source(p, order=i))
        return srcs
    cards = engine.detect_cards()
    if not cards:
        raise UsageError("No memory cards found. Insert a card (in a card reader) or pass a "
                         "folder, e.g.: raw-jpg-import import E:\\")
    _say(args, "Cards found:", *(f"  {v.path}  {v.label}" for v in cards))
    return [engine.make_source(v.path, order=i, volume=v) for i, v in enumerate(cards)]


def _scan_all(args: argparse.Namespace, cfg: Any, printer: ProgressPrinter):
    from . import engine

    scanned = []
    for src in _sources(args, printer):
        scanned.append(run_cancellable(engine.scan_source, src, cfg, printer=printer))
    analysis = engine.analyze(scanned)
    for path, err in analysis.errors[:20]:
        print(f"warning: could not read {path}: {err}", file=sys.stderr)
    return analysis


def _day_line(g: Any, folders: list[str]) -> str:
    from .naming import format_date

    counts = " ".join(f"{f} {g.counts.get(f, 0):>4}" for f in folders)
    return (f"{format_date(g.day)} {g.day:%a}  {g.first:%H:%M}-{g.last:%H:%M}  {counts}")


def _load_cfg() -> Any:
    from .config import load_config

    cfg = load_config()
    for w in cfg.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return cfg


# ---- commands -------------------------------------------------------------------------------


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui.app import main as gui_main

    return gui_main()


def cmd_cards(args: argparse.Namespace) -> int:
    from . import engine
    from .report import fmt_bytes

    cards = engine.detect_cards()
    if args.json:
        print(json.dumps([{"path": str(v.path), "label": v.label, "serial": v.serial,
                           "total": v.total, "free": v.free} for v in cards], indent=2))
        return EXIT_OK
    if not cards:
        print("No memory cards found (a card is a drive with a DCIM folder).")
        return EXIT_OK
    for v in cards:
        print(f"{v.path}  {v.label:20} {fmt_bytes(v.total - v.free)} used of "
              f"{fmt_bytes(v.total)}")
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    from . import engine
    from .grouping import DayChoice, day_key, suggestions_for
    from .naming import format_date
    from .planner import Action
    from .settings import load_settings

    settings = load_settings()
    cfg = _load_cfg()
    day_start = _parse_day_start(args.day_start or settings.day_start)
    printer = ProgressPrinter(enabled=None if not args.json else False)
    analysis = _scan_all(args, cfg, printer)
    groups = engine.group(analysis, day_start)
    folders = cfg.formats.folders()
    new_by_day: dict[date, int] = {}
    suggestions: dict[date, list[str]] = {}
    dest = Path(args.dest) if args.dest else None
    if dest is None and settings.dest_root and Path(settings.dest_root).is_dir():
        dest = Path(settings.dest_root)          # the last destination used, as for import
    if dest is not None and dest.is_dir():
        existing = engine.existing_events(dest, day_start=day_start)
        for g in groups:
            suggestions[g.day] = [e.folder for e in suggestions_for(g.day, existing)]
        choices = [DayChoice(day=g.day, text=(suggestions[g.day] or [""])[0]) for g in groups]
        if groups:
            plan = engine.plan(analysis, choices, dest_root=dest, day_start=day_start, cfg=cfg)
            for item in plan.items:
                if item.action is Action.COPY and item.capture is not None:
                    d = item.day or day_key(item.capture.local, day_start)
                    new_by_day[d] = new_by_day.get(d, 0) + 1
    if args.json:
        print(json.dumps({
            "day_start": day_start.strftime("%H:%M"),
            "days": [{"date": g.day.isoformat(), "first": g.first.isoformat(sep=" "),
                      "last": g.last.isoformat(sep=" "), "counts": dict(g.counts),
                      "bytes": g.bytes, "fallback_dates": g.fallback_dates,
                      "new": new_by_day.get(g.day, 0) if dest else None,
                      "suggestions": suggestions.get(g.day, [])} for g in groups],
            "unknown": analysis.unknown,
            "errors": [[str(p), e] for p, e in analysis.errors],
        }, indent=2))
        return EXIT_OK
    if not groups:
        print("No photos or videos found.")
    for g in groups:
        line = _day_line(g, folders)
        if dest is not None:
            line += f"  new {new_by_day.get(g.day, 0):>4}"
        if suggestions.get(g.day):
            line += "  -> " + " | ".join(suggestions[g.day])
        print(line)
    sug = engine.suggest_day_start(analysis.shots, day_start)
    if sug is not None:
        print(f"Note: shooting continued past midnight; --day-start {sug:%H:%M} would keep "
              "it on the previous day.")
    if analysis.unknown:
        kinds = ", ".join(f".{e or '(none)'} x{n}" for e, n in sorted(analysis.unknown.items()))
        print(f"Unknown file types (not imported): {kinds}")
    if groups:
        print(f"{len(groups)} days · {format_date(groups[0].day)} to "
              f"{format_date(groups[-1].day)}")
    return EXIT_OK


_DATE_LIKE = re.compile(r"\s*\d{4}[-.]")


def _parse_names(args: argparse.Namespace) -> tuple[dict[date, str], str | None, set[date]]:
    """``(DATE=NAME names, the NAME for every other day, --join dates)``.

    A ``--name`` that starts like a date before its ``=`` must be a valid date, so a typo
    such as ``2026-01-32=Party`` is an error instead of the folder name for every day.
    """
    per_day: dict[date, str] = {}
    generic: str | None = None
    for spec in args.name or []:
        head, eq, tail = spec.partition("=")
        if eq and _DATE_LIKE.match(head):
            try:
                day = _parse_date(head)
            except UsageError as e:
                raise UsageError(f"--name {spec!r}: {e}") from None
            if day in per_day and per_day[day] != tail:
                raise UsageError(f"two names for {day.isoformat()}: "
                                 f"{per_day[day]!r} and {tail!r}")
            per_day[day] = tail
            continue
        if generic is not None:
            raise UsageError("only one --name without a date is allowed")
        generic = spec
    joins = {_parse_date(d) for d in args.join or []}
    if both := sorted(joins & per_day.keys()):
        raise UsageError(f"--join {both[0].isoformat()} and --name {both[0].isoformat()}=... "
                         "contradict each other: a joined day takes the folder of the day "
                         "before it")
    return per_day, generic, joins


def _check_name_dates(args: argparse.Namespace, groups: list) -> None:
    """Dates given with ``--name DATE=NAME`` or ``--join`` must be days on the cards."""
    per_day, _, joins = _parse_names(args)
    days = {g.day for g in groups}
    for flag, wanted in (("--name DATE=NAME", per_day.keys()), ("--join", joins)):
        if unknown := sorted(set(wanted) - days):
            raise UsageError(f"{flag}: no photos on " +
                             ", ".join(d.isoformat() for d in unknown))


def _resolve_names(args: argparse.Namespace, groups: list, existing: list,
                   interactive: bool, *, kept: list | None = None,
                   held: dict[date, str] | None = None) -> list:
    """Per-day folder choices; the first match wins (README): ``--name DATE=NAME``, then
    ``--join`` / ``--merge``, then the one existing folder holding the day, then
    ``--name NAME``, then a prompt (terminal), a date-only folder (``--yes``) or an error.

    ``groups`` are all the selected days; only those in ``kept`` (default: all) are imported.
    Joins and ``--merge`` are worked out over all of them, so a day joined to a day that has
    nothing new goes into that day's folder: its explicit name, else ``held`` (the folder
    holding its files), else its one existing folder, else ``--name NAME``.
    """
    from .grouping import DayChoice, folder_name, suggestions_for
    from .naming import format_date

    per_day, generic, joins = _parse_names(args)
    kept_days = {g.day for g in (groups if kept is None else kept)}
    held = held or {}

    def head_folder(day: date) -> str | None:
        """The folder of a join chain whose first days are not imported in this run."""
        if day in per_day:
            return folder_name(day, per_day[day], existing)
        if day in held:
            return held[day]
        sugg = suggestions_for(day, existing)
        if len(sugg) == 1:
            return sugg[0].folder
        return folder_name(day, generic, existing) if generic is not None else None

    choices: list[DayChoice] = []
    missing: list[str] = []
    head = groups[0].day if groups else date.min
    started = False                 # a day of the current join chain is imported in this run
    for n, g in enumerate(groups):
        # A day with its own --name DATE=NAME starts a new folder, even with --merge.
        joined = n > 0 and g.day not in per_day and (bool(args.merge) or g.day in joins)
        if not joined:
            head, started = g.day, False
        if g.day not in kept_days:
            continue
        c = DayChoice(day=g.day, include=True)
        if joined and started:
            c.join_previous = True
            choices.append(c)
            continue
        started = True
        if joined and (folder := head_folder(head)) is not None:
            c.text = folder
            choices.append(c)
            continue
        sugg = suggestions_for(g.day, existing)
        if g.day in per_day:
            c.text = per_day[g.day]
        elif len(sugg) == 1:
            c.text = sugg[0].folder
        elif generic is not None:
            c.text = generic
        elif interactive:
            c.text, c.join_previous = _prompt_name(g, sugg, bool(choices))
        elif args.yes:
            c.text = sugg[0].folder if sugg else ""
        else:
            hint = f" (existing: {', '.join(e.folder for e in sugg)})" if sugg else ""
            missing.append(f"{format_date(g.day)}{hint}")
        choices.append(c)
    if missing:
        raise UsageError("No event name for: " + "; ".join(missing) +
                         ". Use --name \"Event\" or --name DATE=\"Event\", or --yes for "
                         "date-only folders.")
    return choices


def _prompt_name(g: Any, sugg: list, can_join: bool) -> tuple[str, bool]:
    from .naming import format_date

    counts = " · ".join(f"{f} {n}" for f, n in g.counts.items())
    print(f"\n{format_date(g.day)} ({g.day:%a}, {g.first:%H:%M}-{g.last:%H:%M}, {counts})")
    for i, e in enumerate(sugg, 1):
        print(f"  {i}) {e.folder}")
    hint = "number, " if sugg else ""
    hint += "^ = same folder as the day above, " if can_join else ""
    try:
        answer = input(f"Event name ({hint}empty = date only): ").strip()
    except EOFError:
        raise UsageError("no event name given (input ended); use --name") from None
    if can_join and answer == "^":
        return "", True
    if sugg and answer.isdigit() and 1 <= int(answer) <= len(sugg):
        return sugg[int(answer) - 1].folder, False
    return answer, False


def _folders_holding(plan: Any, groups: list) -> dict[date, str]:
    """Day -> the library folder holding most of that day's already-imported files."""
    from collections import Counter

    from .planner import Action

    day_of = {s.id: g.day for g in groups for s in g.shots}
    found: dict[date, Counter[str]] = {}
    for i in plan.items:
        day = day_of.get(i.shot_id) if i.shot_id is not None else None
        if day is not None and i.target is not None and \
                i.action in (Action.SKIP_IMPORTED, Action.SKIP_EXISTS):
            found.setdefault(day, Counter())[i.target.folder] += 1
    return {day: c.most_common(1)[0][0] for day, c in found.items()}


def _select_days(args: argparse.Namespace, groups: list) -> list:
    wanted = None
    if args.days:
        wanted = {_parse_date(d) for d in args.days.split(",") if d.strip()}
    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until) if args.until else None
    out = []
    for g in groups:
        if wanted is not None and g.day not in wanted:
            continue
        if since and g.day < since:
            continue
        if until and g.day > until:
            continue
        out.append(g)
    if wanted:
        unknown = wanted - {g.day for g in groups}
        if unknown:
            raise UsageError("no photos on " + ", ".join(sorted(d.isoformat() for d in unknown)))
    return out


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_import(args: argparse.Namespace) -> int:
    from . import engine
    from .grouping import DayChoice, day_key, suggestions_for
    from .planner import Action, PlanOptions
    from .report import (
        deletion_plan_lines,
        deletion_summary_lines,
        fmt_bytes,
        import_summary_lines,
        plan_summary_lines,
    )
    from .settings import load_settings, save_settings

    settings = load_settings()
    cfg = _load_cfg()
    dest_text = args.dest or settings.dest_root
    if not dest_text:
        raise UsageError("No destination. Use --dest FOLDER (it is remembered for next time).")
    dest = Path(os.path.abspath(dest_text))      # remembered absolute, measured absolute
    if not dest.is_dir():
        raise UsageError(f"Destination folder does not exist: {dest}")
    day_start = _parse_day_start(args.day_start or settings.day_start)
    interactive = sys.stdin.isatty() and not args.yes and not args.json
    printer = ProgressPrinter(enabled=None if not args.json else False)
    analysis = _scan_all(args, cfg, printer)
    all_groups = engine.group(analysis, day_start)
    groups = _select_days(args, all_groups)
    if not groups:
        _say(args, "No photos or videos to import.")
        if args.json:
            print(json.dumps({"plan": None}))
        return EXIT_OK
    _check_name_dates(args, all_groups)
    existing = engine.existing_events(dest, day_start=day_start)
    opts = PlanOptions(reimport_culled=args.reimport_culled,
                       copy_unknown=True if args.copy_unknown else None)

    # Leave out days with nothing new unless asked (or named explicitly).
    kept = groups
    held: dict[date, str] = {}
    carried: list[DayChoice] = []
    if not args.all_days and not args.days:
        probe = {}
        for g in groups:
            sugg = suggestions_for(g.day, existing)
            probe[g.day] = DayChoice(day=g.day, text=sugg[0].folder if sugg else "")
        pre = engine.plan(analysis, list(probe.values()), dest_root=dest, day_start=day_start,
                          cfg=cfg, opts=opts)
        new_days = {i.day or day_key(i.capture.local, day_start) for i in pre.items
                    if i.action is Action.COPY and i.capture is not None}
        skipped = [g for g in groups if g.day not in new_days]
        kept = [g for g in groups if g.day in new_days]
        held = _folders_holding(pre, skipped)
        if args.delete_originals:
            # Their files are in the library already. They stay in the plan, exactly as just
            # planned (nothing to copy), so they can be deleted from the cards as well.
            carried = [replace(probe[g.day], standalone=True) for g in skipped]
        if skipped:
            why = ("their files can still be deleted from the cards" if carried
                   else "use --all-days to include them")
            _say(args, f"{len(skipped)} days already imported ({why}).")
        if not kept and not carried:
            _say(args, "Nothing new to import.")
            if args.json:
                print(json.dumps({"plan": pre.to_json()}, indent=2))
            return EXIT_OK

    sug = engine.suggest_day_start(analysis.shots, day_start)
    if sug is not None and not args.day_start:
        _say(args, f"Note: shooting continued past midnight; use --day-start {sug:%H:%M} to "
                   "keep it on the previous day.")
    choices = _resolve_names(args, groups, existing, interactive, kept=kept, held=held)
    choices += carried
    # --merge is in the choices already (as joins), so a day named with DATE=NAME can split it.
    plan = engine.plan(analysis, choices, dest_root=dest, day_start=day_start, cfg=cfg,
                       opts=opts, probe_write=not args.dry_run)
    if args.json and args.dry_run:
        print(json.dumps({"plan": plan.to_json()}, indent=2))
        return EXIT_USAGE if plan.problems else EXIT_OK
    _say(args, "", *plan_summary_lines(plan))
    if plan.problems:
        if args.json:
            print(json.dumps({"plan": plan.to_json()}, indent=2))
        return EXIT_USAGE
    if args.dry_run:
        return EXIT_OK
    copies = len(plan.to_copy)
    if copies == 0:
        _say(args, "Nothing to copy.")
    elif interactive and not _confirm(f"Import {copies} files ({fmt_bytes(plan.bytes_to_copy)})? "
                                      "[y/N] "):
        _say(args, "Nothing copied.")
        return EXIT_OK

    report = run_cancellable(engine.execute, plan, printer=printer)
    settings.dest_root = str(dest)
    settings.day_start = day_start.strftime("%H:%M")
    for c in choices:
        if c.text and not c.join_previous and not c.standalone:
            settings.remember_name(c.text)
    try:
        save_settings(settings)
    except OSError:
        pass

    result: dict[str, Any] = {"status": report.status,
                              "counts": {o.name.lower(): n for o, n in report.counts().items()},
                              "folders": [str(f) for f in report.folders],
                              "log": str(report.log_path) if report.log_path else None}
    _say(args, "", *import_summary_lines(report))
    if report.log_path:
        _say(args, f"Log: {report.log_path}")

    delete_failed = False
    if args.delete_originals and report.status in ("ok", "partial"):
        dplan = engine.plan_deletion(report)
        if not dplan.candidates:
            _say(args, "Nothing can be deleted from the cards.")
        else:
            _say(args, "", *deletion_plan_lines(dplan),
                 "Tip: formatting the card in the camera is faster and safer.")
            ok = args.yes
            # --json keeps stdout machine-readable, so it never prompts.
            if not ok and sys.stdin.isatty() and not args.json:
                try:
                    ok = input("Type DELETE to permanently delete these files from the cards: ") \
                        .strip() == "DELETE"
                except EOFError:
                    ok = False
            if not ok:
                _say(args, "Nothing deleted.")
            else:
                dreport = run_cancellable(engine.delete_originals, dplan, confirmed=True,
                                          log_path=report.log_path, printer=printer)
                _say(args, "", *deletion_summary_lines(dreport))
                result["deleted"] = {o.name.lower(): n for o, n in dreport.counts().items()}
                if dreport.status == "cancelled":
                    report.status = "cancelled"
                delete_failed = dreport.status == "partial"
    if args.json:
        print(json.dumps({"result": result}, indent=2))
    if report.status == "cancelled":
        return EXIT_CANCELLED
    if report.status in ("partial", "aborted") or delete_failed:
        return EXIT_FAILED
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    from .config import load_config, write_template

    if args.config_cmd == "path":
        p = appdirs.config_file()
        print(f"{p}{'' if p.is_file() else '  (not created yet; run: raw-jpg-import config init)'}")
        return EXIT_OK
    if args.config_cmd == "init":
        try:
            p = write_template()
        except FileExistsError:
            print(f"Already exists: {appdirs.config_file()}", file=sys.stderr)
            return EXIT_USAGE
        print(f"Created {p}")
        return EXIT_OK
    cfg = load_config()
    t = cfg.formats
    if args.json:
        print(json.dumps({
            "config_file": str(cfg.user_path) if cfg.user_path else None,
            "categories": [{"key": c.key, "folder": c.folder, "role": c.role, "rank": c.rank,
                            "extensions": sorted(c.extensions)} for c in t.categories],
            "unknown": t.unknown_policy, "other_folder": t.other_folder,
            "warnings": cfg.warnings,
        }, indent=2))
        return EXIT_OK
    print(f"Config file: {cfg.user_path or 'none (built-in defaults)'}")
    for c in sorted(t.categories, key=lambda c: (c.role != "primary", c.rank, c.key)):
        where = c.folder or {"sidecar": "(follows its photo)", "ignore": "(ignored)"}[c.role]
        print(f"  {c.key:8} {where:20} {' '.join(sorted(c.extensions))}")
    print(f"Unknown types: {t.unknown_policy}"
          + (f" (into {t.other_folder}/)" if t.unknown_policy == "copy" else ""))
    for w in cfg.warnings:
        print(f"warning: {w}")
    return EXIT_OK


# ---- argument parsing -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="raw-jpg-import",
        description="Import photos and videos from camera cards into dated event folders "
                    "(RAW / JPG / MOV). Run without arguments to open the window.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="more log output")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only errors")
    # The same flags after the command (``import ... -q``); SUPPRESS keeps a value given
    # before the command when they are not repeated after it.
    common = argparse.ArgumentParser(add_help=False)
    after = common.add_mutually_exclusive_group()
    after.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="more log output")
    after.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                       help="only errors")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("gui", help="open the window", parents=[common])

    c = sub.add_parser("cards", help="list detected memory cards", parents=[common])
    c.add_argument("--json", action="store_true")

    s = sub.add_parser("scan", help="show the days on the cards (changes nothing)",
                       parents=[common])
    s.add_argument("sources", nargs="*", metavar="SRC", help="card or folder (default: all cards)")
    s.add_argument("--dest", metavar="DIR",
                   help="also show what is new for this destination (default: last used)")
    s.add_argument("--day-start", metavar="HH:MM", help="when a new day starts (00:00-06:00)")
    s.add_argument("--json", action="store_true")

    i = sub.add_parser("import", help="import photos and videos", parents=[common])
    i.add_argument("sources", nargs="*", metavar="SRC", help="card or folder (default: all cards)")
    i.add_argument("--dest", metavar="DIR", help="destination root (default: last used)")
    i.add_argument("-n", "--name", action="append", metavar="NAME|DATE=NAME",
                   help="event name for every day, or for one day (repeatable)")
    i.add_argument("--join", action="append", metavar="DATE",
                   help="put DATE into the previous day's folder (repeatable)")
    i.add_argument("--merge", action="store_true",
                   help="put all selected days into one folder (dated by the first day)")
    i.add_argument("--days", metavar="D1,D2", help="only these days")
    i.add_argument("--since", metavar="DATE", help="only days from DATE")
    i.add_argument("--until", metavar="DATE", help="only days up to DATE")
    i.add_argument("--all-days", action="store_true",
                   help="include days with nothing new")
    i.add_argument("--day-start", metavar="HH:MM", help="when a new day starts (00:00-06:00)")
    i.add_argument("--reimport-culled", action="store_true",
                   help="import again files you removed from the library")
    i.add_argument("--copy-unknown", action="store_true",
                   help="copy files of unknown type into OTHER/")
    i.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    i.add_argument("-y", "--yes", action="store_true",
                   help="no questions; accept suggestions; unnamed days get date-only folders")
    i.add_argument("--delete-originals", action="store_true",
                   help="after importing, delete verified files from the cards")
    i.add_argument("--json", action="store_true", help="machine-readable output on stdout")

    cfgp = sub.add_parser("config", help="show or create the config file", parents=[common])
    cfgsub = cfgp.add_subparsers(dest="config_cmd", metavar="<path|show|init>", required=True)
    cfgsub.add_parser("path", help="print the config file location")
    show = cfgsub.add_parser("show", help="print the effective file types")
    show.add_argument("--json", action="store_true")
    cfgsub.add_parser("init", help="create a commented config file")
    return p


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "gui": cmd_gui, "cards": cmd_cards, "scan": cmd_scan, "import": cmd_import,
    "config": cmd_config,
}


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command is None:
        return cmd_gui(args)
    from .report import configure_logging

    configure_logging(verbose=args.verbose, console=args.verbose)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return COMMANDS[args.command](args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except PlanBlocked as e:
        for problem in e.problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE
    except AnotherImportRunning as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_LOCKED
    except (Cancelled, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
