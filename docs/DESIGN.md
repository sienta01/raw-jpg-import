# raw-jpg-import: design notes

For contributors. The README covers what the tool does for its users; this page covers how
the engine is put together and the rules it must never break. Python 3.11+, standard library
only at run time (`tomllib`, `sqlite3`, `hashlib.file_digest`, `tkinter` for the window). The
one exception is the optional photo viewer, which decodes previews with Pillow (bundled in the
standalone apps; `pip install ".[viewer]"` from source).

## Architecture

```
gui/  (Tk; the only code that imports tkinter)      cli.py (argparse)
        \                                            /
         +------------------ engine.py -------------+      facade, import lock, session log
                                |
  volumes  scanner  metadata/  shots  grouping  destindex  planner  copier  deleter  report
  formats  config   settings   naming  ledger   fsops      events   errors  models   appdirs
```

Dependencies point downwards only. `gui` and `cli` talk to the engine through `engine.py`;
nothing outside `gui/` imports tkinter, and `tests/integration/test_no_tk_import.py` checks
that `raw_jpg_import.cli` loads without it. `gui/viewmodel.py` holds the window's rules as
plain Python so they are tested without a display.

| Module | Job |
|---|---|
| `appdirs`, `settings`, `config`, `formats` | per-OS folders (`RAW_JPG_IMPORT_HOME` overrides all); app-owned `settings.json`; user `config.toml` merged over `data/defaults.toml` into a `FormatTable` |
| `volumes` | `detect_cards()`: drives with a `DCIM` folder at the root (Win32 API, `/Volumes`, `/proc/self/mounts`) |
| `scanner` | walk one source, apply skip rules, classify files -> `SourceScan` |
| `metadata/` | `read_capture()` per file, never raises; TIFF IFD, JPEG APP1, ISO-BMFF (HEIF, CR3, MOV/MP4), RAF; bounded reads |
| `shots` | pair files across cards into `Shot`s; duplicates, variants, sidecars |
| `grouping` | `day_key`, `DayGroup`s, midnight suggestion, existing event folders, `resolve_targets` |
| `ledger` | SQLite import history |
| `destindex` | cached destination listings, `same_file()`, `stem_owners()` |
| `planner` | `build_plan()` -> `ImportPlan` of `PlanItem`s with an `Action` each |
| `fsops` | `FileOps` protocol + `RealFileOps`: no-clobber rename, lock retries, online-only test, file lock, keep-awake |
| `copier`, `deleter` | execute a plan / delete verified originals |
| `report`, `events`, `errors` | summaries and logs; `Progress`/`FileDone`/`Notice` events, `CancelToken`, `Throttle`; exception types |
| `selftest` | `RawJpgImport --selftest` for frozen builds |

## Pipeline

```
scan -> metadata -> shots -> days -> targets -> plan -> copy -> (opt-in) delete
```

1. **Scan** (`scanner.scan_source`). Iterative `os.scandir` with `follow_symlinks=False` and a
   visited `(st_dev, st_ino)` set. Cards are walked from the volume root (Sony keeps video
   under `PRIVATE/`). System folders, dotfiles, `._*`, `*.part` and the configured skip rules
   are left out. Each file is classified by extension into a category (`primary`, `sidecar`,
   `ignore`) or "unknown". The camera's protect flag becomes `readonly`. Read errors are
   collected and the walk goes on.
2. **Metadata** (`metadata.read_many`, called by `engine.scan_source`). A small thread pool
   hides card latency. `Capture.local` is the camera's naive wall-clock time; `exact` is true
   only for real capture tags (EXIF, HEIF Exif, CR3, RAF, Nikon `NCTG`, Apple keys). Videos
   without those use `mvhd`, with the UTC offset inferred from the card mtime minus the clip
   duration (snapped to 15 minutes), and finally the mtime. Parsers are fuzz-tested never to
   raise; any failure falls through to the next source.
3. **Shots** (`shots.build_shots`). Primaries are bucketed by `key(stem)` and linked with
   `same_shot` (exact times: same second and sub-second; otherwise within 120 s), so a NEF on
   card A and its JPG on card B form one shot while a counter rollover `DSC_0001` stays a
   separate shot. A fallback-dated member inherits the best member's time (source
   `"partner"`), so a pair never splits across days. Same name and size inside a shot is a
   **duplicate** (backup mode; the lowest source order is main); same name, different size is
   a **variant** (`-v2`, `-v3`; the largest keeps the plain name). Sidecars attach to a
   primary in the same directory by stem or full name. Order is deterministic.
4. **Days** (`grouping.group_days`). `day_key(t, day_start) = (t - day_start).date()`;
   `day_start` is 00:00 to 06:00. `suggest_day_start` finds clusters (gaps over 60 min) that
   cross midnight and proposes the next full hour after their last shot.
5. **Targets** (`grouping.resolve_targets`). Each included `DayChoice` gets an `EventTarget`.
   A joined day (or every day with `merge_all`) takes the previous included day's target; a
   `standalone` choice (a day planned only so its already-imported files can be deleted from
   the cards) is never joined or merged. Text equal (by `key`) to an existing folder name, or
   to `YYYY.MM.DD - text`, reuses that folder; otherwise the folder is
   `event_folder_name(day, sanitize(text))`, reused if it exists or if an existing folder's
   name cleans up to the same thing (`Rock--Paper`, `Trip  2`). A merged new folder is dated
   by its earliest day. Existing folders are the top-level `YYYY.MM.DD[ - Name]` directories of
   the destination, plus the extra days the ledger says each one holds (trip folders). Those
   days are the `day_key` of the recorded captures with the current `day_start`, so a party
   folder that runs past midnight does not also claim the next afternoon.
6. **Plan** (`planner.build_plan`). Stats and small destination metadata reads only; no
   hashing, no writes except the optional write probe. The same function serves the GUI's
   live preview, `--dry-run` and the real run, and the GUI rebuilds the plan on Import.
   Actions: `COPY`, `SKIP_IMPORTED`, `SKIP_EXISTS`, `SKIP_CULLED`, `SKIP_DUPLICATE`,
   `SKIP_DESELECTED`, `UNKNOWN`, `ORPHAN`. Blocking `problems` (nothing is copied): destination
   missing or not writable, a source and the destination nested either way, the destination
   on the card a folder source lies on (`F:\DCIM` as source, `F:\Backup` as destination),
   free space minus the copy below 512 MiB, a `RAW`/`JPG`/... path that is a file, a path
   over 259 characters on Windows (measured absolute: `engine.plan` and `build_plan` make the
   destination absolute first), no day included. No write probe is made on a card. With
   unknown files copied, a loose file from a day without photos goes with the nearest
   included day (and a notice says so); every item records the day it is planned under.
7. **Copy** (`copier.execute`, under the import lock). See the invariants below. Items are
   copied in `(source order, rel)` order for sequential card reads; folders are created
   lazily and the empty ones removed at the end. If a main member fails, its duplicates on
   the other card are promoted and copied instead. `SKIP_EXISTS` items get an `adopted` ledger
   row. The report has one `ItemResult` per item and a status `ok | partial | cancelled |
   aborted`; `report.write_session_log` writes `import-<session>.log`.
8. **Delete** (`deleter`, opt-in). `plan_deletion(report)` lists candidates: `COPIED`,
   `ALREADY_THERE`, and `DUPLICATE`s whose main member is one of those. Everything else is
   excluded with a reason. `delete_originals(..., confirmed=True)` re-stats the card file,
   keeps read-only files, requires a local (not online-only) copy of the same size, then
   hashes the copy and the card file afresh; both must match each other and the hash recorded
   at import. Only then `os.remove`, then `ledger.mark_deleted`.

## Safety invariants

Code review and the tests enforce these. A change that weakens one needs a very good reason.

1. **Sources are read-only.** Card files are only opened `"rb"`. Nothing (temp files, logs,
   ledger) is written to a source. Only `deleter.py` removes files, only with
   `confirmed=True`.
2. **No partial file under a final name.** Copy to `.<name>.rji-<session>.part` in the target
   folder, fsync, re-read it with a fresh open and compare SHA-256 with the bytes read from the
   card (one retry; the 3rd mismatch in a session aborts with "check the card reader"),
   `os.utime` (timestamps only, never `copystat`, so the protect bit is not copied), then a
   no-clobber rename, and only then the ledger row. Stale `.part` files older than an hour
   are cleaned up.
3. **Never replace an existing file.** Windows: `os.rename` (fails if the target exists),
   retried on sharing violations (winerror 5/32, antivirus or OneDrive). POSIX: `os.link`
   + `unlink`; where links are unsupported (exFAT, FAT, SMB) an `lexists` check + `rename`,
   guarded by the import lock.
4. **Every scanned file ends in exactly one reported outcome**, including unknown and ignored
   files (ignored ones are counted by extension).
5. **Re-running copies 0 bytes.** Already-imported files are recognised by the ledger and by
   the disk checks.
6. **Delete only after a fresh two-sided read.** The copy-time read-back may come from the OS
   page cache; the delete-time hashes are the strong end-to-end check.
7. **Planning is deterministic.** The same inputs and destination state give the same plan.

The source is also re-stat'ed before and after each copy; a changed size or mtime fails the
item ("changed since scan"). Card removal fails only that source; a full destination aborts.

## Naming and collision rules

- `naming.key(s)` (NFC + casefold) is used for every name comparison, so `.nef` and `.NEF`
  collide on every OS, as do NFD and NFC spellings.
- `sanitize_component`: NFC; `<>:"/\|?*` and control characters become `-`; repeated `-` and
  spaces collapse; trailing dots, spaces and `-` go; Windows reserved names (`CON`, `COM1`,
  ...) get `_`; at most 80 characters. Empty, `.` and `..` give a date-only folder. The same
  rules apply on every OS so libraries sync cleanly.
- **Stem allocation** is per event folder over the whole folder (all subfolders), per shot:
  try the stem an already-imported member of the shot has (ledger), then any `stem-N` that
  library files of this same capture already use (so a card imported later keeps its
  partner's `-2` even after the plain stem became free), then `stem`, then `stem-2`,
  `stem-3`, ... A candidate is rejected when another shot in this
  batch claimed it, when any member's destination name exists and is not `same_file` (except
  when it holds another version of the same exact capture, as with JPEG + JPEG cards imported
  one at a time: the member then becomes the next free `stem-vN`), or when
  a file with that stem (any `-vN` removed) in the folder belongs to a different capture
  (`stem_owners`). So a clash in `JPG/` alone renames the NEF too, and a second card imported
  later gets the same `-2` (the ledger remembers the stem; without it, the destination files'
  capture times decide). Only photos, videos and files the ledger recorded count as stem
  owners: an editor's `.xmp` or a voice memo that the ledger does not know only has a file
  time, which says nothing about its shot, so it never forces a rename.
- `same_file(dest, src)`: equal size and either mtime within 2 s, or (destination not
  online-only) the same exact capture second read from the destination. Without an exact
  capture, mtimes exactly one hour (+-2 s) apart also match when 64 KiB samples at the start,
  middle and end of both files are equal: FAT/exFAT store local time, so after a
  daylight-saving change the OS reports card times an hour off. Anything else is a
  different file, so the planner renames rather than skips: when in doubt, copy.
- The original extension case is kept. Variants are `stem-vN.ext`; sidecars follow their
  primary's final stem (`DSC_0001-2.NEF.xmp`).

## Ledger semantics

`history.sqlite3` in the data dir, WAL, `synchronous=NORMAL`, `meta.schema = 1`. One row per
library file written (`copied`) or found identical (`adopted`); `deleted_from_card` marks a
card file removed later. Rows carry the destination root (`root_key`: normcase + abspath +
NFC, and casefolded on macOS, whose APFS/HFS+ ignore case by default; a history written
before that is re-keyed once when opened), event folder, `dest_rel` (POSIX), original name
key, size, mtime, capture time, SHA-256 and the card's label and serial.

- **Lookup** by `(orig_key, size)` with the capture within 2 s, for the current destination
  root only.
- Event folder and file both present, and the file is still the one recorded (same size and
  mtime within 2 s; otherwise the exact capture read from the file decides, so a copy edited
  in place still counts; without exact captures the size decides): `SKIP_IMPORTED` (pointing
  at the real location, with a warning when that is not the chosen folder).
- Event folder present, file gone (or another file took its name after a cull): the user
  culled it, so `SKIP_CULLED` unless `reimport_culled`.
- Event folder gone (renamed or moved): the row is ignored and the disk checks decide.
- The ledger only adds knowledge. A missing or corrupt database (renamed `*.bad` and
  recreated) loses culling memory but never causes an overwrite or a duplicate. A row is
  written only after the final name exists, so a crash in between leads to `SKIP_EXISTS` and an
  `adopted` row on the next run.
- One connection per thread or job (sqlite3 connections stay on their thread); `engine`
  opens one per call.

## Threading model

- The engine is synchronous. Long calls take `cancel: CancelToken` and `emit: Callable`;
  progress goes out as `Progress` / `FileDone` / `Notice` events, rate-limited by `Throttle`.
  `cancel.check()` runs between chunks and between files (deletes: between files only).
- `metadata.read_many` uses a `ThreadPoolExecutor` (4 workers) with a cache keyed by
  `(path, size, mtime_ns)`.
- **GUI** (`gui/jobs.py`): only the Tk thread touches widgets or tk Variables. One long job at
  a time (scan queue, import or delete) on a worker thread, plus "light" jobs (card polling
  every 3 s, the plan refresh 300 ms after any change) tagged with a generation counter so
  stale results are dropped. Workers post to a `queue.Queue` that `root.after(50)` drains, at
  most 500 events per tick. Workers receive plain values, never tk Variables.
- **Photo viewer** (`gui/viewer.py`, `previews.py`): read-only. `previews.list_library`
  groups an event folder's files into shots by stem (minus `-vN`). Two `PreviewLoader`
  workers decode previews newest-request-first, so the rows on screen win. JPEGs are decoded
  with Pillow's `draft()` (DCT scaling). Raws show the largest JPEG embedded in them, found by
  walking IFD0 and the SubIFDs (Nikon `JpgFromRaw`) or the RAF header, and rotated by the
  raw's EXIF orientation. Results go through a queue to the Tk thread, which alone creates
  `PhotoImage`s. Cloud placeholders (online-only) are never read, because a read would
  download them.
- **CLI**: the engine runs on a worker thread; the main thread waits so the first Ctrl+C can
  set the cancel token (exit 130 after a partial summary) and a second one exits at once.
- **Processes**: `engine.import_lock()` holds an OS lock (`msvcrt.locking` / `fcntl.flock`) on
  `<data>/import.lock` during imports and deletes, released by the OS if the process dies;
  a second import gets `AnotherImportRunning` (CLI exit 3). Windows imports also hold
  `SetThreadExecutionState` so the PC does not sleep.

## Configuration

`data/defaults.toml` is the built-in table (raw, heif -> `JPG/`, jpg, video -> `MOV/`, sidecar,
ignore) plus scan skip rules and the unknown-file policy (`report` or `copy` into `OTHER/`).
The user's `config.toml` adjusts it: `extensions` replaces a list, `add`/`remove` adjust it, a
new category can claim extensions from defaults, two user categories claiming one extension is
an error. Unknown keys warn; TOML errors report line and column; a bad config raises
`ConfigError` and the UIs refuse to import (no silent fallback). Precedence: defaults <
`config.toml` < CLI flags / GUI options. `settings.json` is app-owned and written atomically;
a corrupt one is set aside as `settings.json.bad`. The delete choice is never persisted.

## Testing strategy

- **No real camera files.** `tests/fakes/` builds byte-exact TIFF, JPEG, HEIF, MOV/MP4, RAF and
  CR3 files with chosen dates (stdlib `struct`), and `fakes.card.make_zf_cards(mode=...)` builds
  two Zf-like cards (`raw_jpg`, `backup`, `overflow`, `jpg_jpg`, `raw_heif`) with junk files, a
  protected file and an unknown type, plus snapshots to prove a card was not changed.
  `exifread` is used only as an oracle that the fake writers follow the spec.
- **Isolation.** An autouse fixture sets `RAW_JPG_IMPORT_HOME` to a temp folder; card detection
  is mocked. No test may see a real card.
- **Unit** (`tests/unit/`): every parser round-trips (LE/BE, iloc v0-v2, NCTG local time,
  `mvhd` offset inference), a fuzz test of truncations and bit flips never raises, plus naming,
  config, scanner, shots, grouping, ledger, destindex, planner (renames, culling, blocking,
  determinism) and fsops.
- **Faults** (`test_copier_faults.py`): an injected `FileOps` produces read errors (with the
  backup twin promoted), flipped bytes (retry, failure, abort on the 3rd), ENOSPC, files too
  large for FAT32, a card pulled mid-copy, a source changed while copying, no hard links, busy
  renames, a name taken after planning, and a "crash" between the rename and the ledger write.
- **Integration** (`tests/integration/`): full two-card imports (tree, bytes, mtimes, no
  `.part`, card snapshot unchanged, re-run copies 0, cancel and resume, lock), deletion
  eligibility, CLI behaviour and exit codes, and the no-tkinter import check.
- **GUI**: `tests/gui/test_viewmodel.py` runs everywhere; `test_gui_smoke.py` (marker `gui`)
  drives a real Tk window on fake cards. CI runs it under `xvfb-run` on Linux.
- **Real samples** (marker `real`, opt-in): the user's own files in the git-ignored
  `tests/real/data/` or `RJI_SAMPLES_DIR`.

Markers `gui` and `real` are deselected by default (`pyproject.toml`); `pytest -m gui` runs
the smoke tests. CI: ruff + pytest on Windows, macOS and Linux with Python 3.11 and 3.14.

## Packaging

`packaging/raw_jpg_import.spec` builds a onefile windowed executable on Windows and Linux and a
onedir `RawJpgImport.app` on macOS (with `NSRemovableVolumesUsageDescription`), UPX off.
`packaging/launch_gui.py` is the frozen entry point and handles `--selftest`.
`packaging/build.py` runs PyInstaller, smoke-tests the result, and writes the release archive
and its `.sha256`. The tag-triggered release workflow builds windows-x64, macos-arm64,
macos-x64 and linux-x64 (on Ubuntu 22.04 for an old glibc), the sdist and the wheel, and opens
a draft GitHub release. The CLI ships through pip/pipx only.

## Deliberately left out

exiftool integration, per-folder manifests, hashing at plan time, `\\?\` long paths, folder
templates and year subfolders, eject, an in-app formats editor, portable mode. Each would add
code without being the last line of defence for any photo.
