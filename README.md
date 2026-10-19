# raw-jpg-import

Copy photos and videos from your camera's memory cards into one tidy folder per event, with
the raw files, the JPGs and the videos kept apart:

```
Photos/
  2026.01.23 - Sunset Beachwalk/
    RAW/    DSC_0001.NEF  DSC_0002.NEF  ...
    JPG/    DSC_0001.JPG  DSC_0002.JPG  ...
    MOV/    DSC_0003.MOV
  2026.01.24 - Old Town/
    RAW/
    JPG/
```

It was written for the Nikon Zf with two cards, where the NEFs go to one card and the JPGs to
the other, but it works with any camera that puts a `DCIM` folder on its card, and with plain
folders too.

- **One window or one command.** Put the cards in a card reader, type an event name per day,
  press Import. There is a command-line tool for the same job.
- **Both cards at once.** A NEF on the SD card and its JPG on the microSD card end up in the
  same event folder with matching names.
- **Your cards are only read.** Every copy is checked byte for byte (SHA-256) before it gets
  its real name. Nothing is ever overwritten. Running it twice copies nothing twice.
- **Check the result in the app.** A built-in viewer shows the imported photos, NEF and JPG
  side by side, and finds any shot that is missing its partner.
- **Deleting from the cards is opt-in**, offered after the import, and only for files whose
  copy has just been read back and found identical.
- Windows, macOS and Linux. The importer needs nothing beyond Python's standard library (the
  viewer's previews use Pillow), and the standalone apps need no Python at all.

Contents: [Camera setup](#setting-up-the-nikon-zf) · [Install](#install) ·
[The app](#using-the-app) · [Command line](#using-the-command-line) ·
[Dates and names](#how-dates-and-names-work) · [Safety](#safety-guarantees) ·
[File types and config](#file-types-and-configtoml) · [Where things live](#where-settings-history-and-logs-live) ·
[Troubleshooting](#troubleshooting) · [Development](#development)

## Setting up the Nikon Zf

The Zf has an SD slot and a microSD slot. In the **Photo shooting menu**, set **Image quality**
to a RAW + JPEG option and **Secondary slot function** to **RAW primary - JPEG secondary**. The
NEFs then go to the primary card and the JPGs to the secondary card. Put both cards in your
reader (or a dual reader) and import them together; each JPG is matched to its NEF by name and
capture time.

The other slot modes work too:

| Secondary slot function | What you get |
|---|---|
| RAW primary - JPEG secondary | NEF from one card, JPG from the other, paired |
| Backup | The same files on both cards. Each is copied once; the second card is a spare |
| Overflow | Whatever is on each card; it does not matter which card holds which shot |
| JPEG primary - JPEG secondary | Two JPGs of different sizes per shot. Both are kept; the smaller one is named `DSC_0001-v2.JPG` |

**HEIF.** With **Tone mode** set to **HLG**, the Zf records HEIF (`.HIF`) instead of JPEG. HIF
files go into `JPG/` next to the JPGs (you can give them their own folder, see
[config](#file-types-and-configtoml)).

**Video.** Clips (`.MOV` or `.MP4`) go to the card chosen under **Video recording menu >
Destination**, whichever slot that is. They land in `MOV/`, dated by the camera's own recording
time.

Set the camera clock (and time zone) correctly: folder dates come from the time the camera
wrote into each file.

## Install

### Standalone app (no Python needed)

Download the file for your system from the [Releases](../../releases) page:

| System | File |
|---|---|
| Windows 10/11 (64-bit) | `RawJpgImport-<version>-windows-x64.zip` |
| macOS on Apple silicon (M1 and later) | `RawJpgImport-<version>-macos-arm64.zip` |
| macOS on Intel | `RawJpgImport-<version>-macos-x64.zip` |
| Linux (x86-64, glibc 2.35 or newer, e.g. Ubuntu 22.04+) | `RawJpgImport-<version>-linux-x64.tar.gz` |

`SHA256SUMS.txt` on the same page lets you check the download.

The apps are not code-signed, so the first launch needs one extra step:

- **Windows.** Unzip, then double-click `RawJpgImport.exe`. If SmartScreen says "Windows
  protected your PC", click **More info**, then **Run anyway**.
- **macOS.** Unzip and drag `RawJpgImport.app` into Applications. Open it once; macOS refuses.
  Then go to **System Settings > Privacy & Security**, scroll down and click **Open Anyway**.
  Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/RawJpgImport.app`.
  The first time it reads a card, macOS asks whether RawJpgImport may access files on a
  removable volume. Click **Allow**, or the cards stay invisible to it (you can change this
  later under **Privacy & Security > Files and Folders**).
- **Linux.** Unpack, then put the binary on your PATH and the menu entry in place:

  ```sh
  tar xzf RawJpgImport-*-linux-x64.tar.gz
  cd RawJpgImport-*-linux-x64
  install -Dm755 RawJpgImport ~/.local/bin/RawJpgImport
  install -Dm644 raw-jpg-import.desktop ~/.local/share/applications/raw-jpg-import.desktop
  ```

  The app brings its own Tk; nothing else needs installing.

The standalone apps are the window only. For the command-line tool, install from source.

### From source, with Python 3.11 or newer

From a clone of this repository (or an unpacked source download):

```sh
pipx install .          # recommended: its own environment, commands on your PATH
# or
pip install .
```

The wheel on the Releases page works the same way:
`pipx install raw_jpg_import-<version>-py3-none-any.whl`.

This gives two commands: `raw-jpg-import-gui` opens the window and `raw-jpg-import` is the
command-line tool. `python -m raw_jpg_import` also opens the window.

The window needs Tk:

- **Windows** and the **python.org macOS installer** include it.
- **Homebrew Python:** `brew install python-tk`.
- **Linux:** `sudo apt install python3-tk` (Debian, Ubuntu), `sudo dnf install python3-tkinter`
  (Fedora), `sudo pacman -S tk` (Arch).

The command-line tool works without Tk.

The photo viewer uses [Pillow](https://pypi.org/project/pillow/) to decode JPEGs. The
standalone apps include it. For a source install, add it with `pipx install ".[viewer]"` (or
`pip install pillow`). Without Pillow everything else works, and the viewer lists the files
without previews.

## Using the app

1. **Sources.** Cards in a card reader appear by themselves within a few seconds, ticked and
   scanned (a card is any drive with a `DCIM` folder). **Add folder...** adds any folder, for
   example an old card copied to disk. Untick a source to leave it out.
2. **Destination.** **Import into:** your photo library folder (**Browse...**). It is remembered.
   The line below shows the free space and how many event folders are already there.
3. **Days.** One row per day of shooting, with the time range, how many RAW / JPG / MOV files
   it holds and how many of them are new. Type an **Event name** for each day you want; untick
   the days you do not.
   - The name box suggests existing folders for that day and names you used recently. Picking
     an existing folder imports into it (handy for a trip folder started on an earlier day).
   - **Same folder as above** puts a day into the folder of the day above it, for example a
     weekend trip in one folder. **One folder for all checked days** does that for every row.
     A shared folder is dated by its first day.
   - An empty name gives a date-only folder such as `2026.01.23`.
   - **Hide days already imported** keeps finished days out of the way.
   - **Day starts at** (00:00 to 06:00). When you shot past midnight, a banner offers to count
     those photos as the evening before, so a party that ended at 00:31 stays in one folder.
4. **Destination folders** shows exactly which folders will be created (NEW) or added to
   (EXISTING) and what goes in each, with names cleaned up as they will be on disk.
   Notices underneath mention unknown file types, files found on both cards, and files
   without a capture date.
5. **Preview...** lists every file and what will happen to it: copy, skip (and why), or rename.
6. **Import** asks for confirmation (you can turn that off in **Settings...**), then copies with
   a progress bar. **Cancel** stops cleanly: finished files stay, the cards are untouched, and
   the next run picks up where it stopped.
7. **Summary.** What was copied, skipped, renamed or failed, with details, the folders with
   **View** and **Open** buttons, and **Open log**. **Run again** re-checks the same cards;
   **New import** starts over.
8. **View photos** opens a viewer on the imported folders, so you can check them without a
   file manager:
   - One thumbnail per shot, with badges showing which files it has (**RAW JPG**, **VIDEO**).
     The line above the grid counts them, for example "300 shots · 288 RAW + JPG · 12 RAW
     without JPG".
   - **Show** filters the grid: **RAW without JPG** and **JPG without RAW** find any shot that
     is missing its partner.
   - Click a shot to see it large. **Previous** / **Next** or the arrow keys move through the
     shots, and **Esc** goes back to the grid. **Files** switches between the JPG and the NEF.
     A NEF shows the full-size preview the camera stored inside it, so it opens instantly.
     **Open file** and **Show in folder** hand the file to your usual apps.
   - Files that are only in the cloud (OneDrive "online-only") are not downloaded just to
     show a thumbnail.
   - The viewer only reads. It never changes, moves or deletes a file.
9. **Delete imported files from cards...** (optional). Shows how many files per card would go
   and which files are kept and why. Tick "I understand these files will be permanently
   removed..." to enable the button. Each file is read again from both the card and the
   library before it is deleted. Formatting the card in the camera is faster, and just as
   good once the import is done.

**Settings...** has the defaults (destination, day start, hide imported days, ask before
importing), the effective file-type table with **Open config file** and **Reload**, and
**About** with **Open log folder**.

## Using the command line

```
raw-jpg-import cards                     list the memory cards it can see
raw-jpg-import scan [SRC...]             show the days on the cards (changes nothing)
raw-jpg-import import [SRC...] [options] import
raw-jpg-import config path|show|init     where the config file is / effective file types / create it
raw-jpg-import gui                       open the window (also: no command at all)
```

`SRC` defaults to every detected card; pass card roots or folders to choose (`E:\ F:\`,
`/Volumes/NIKON\ Z\ F`). `--dest` defaults to the last destination used (for `scan` too).

```sh
# What is on the cards, and what is new for my library?
raw-jpg-import scan --dest "D:\Photos"

# One event for everything new
raw-jpg-import import --dest "D:\Photos" --name "Sunset Beachwalk"

# A name per day (DATE is 2026-01-23 or 2026.01.23)
raw-jpg-import import --name "2026-01-23=Sunset Beachwalk" --name "2026-01-24=Old Town"

# A three-day trip in one folder, dated by its first day
raw-jpg-import import E:\ F:\ --dest "D:\Photos" --merge --name "Bali Trip"

# Put the 24th into the 23rd's folder, keep other days separate
raw-jpg-import import --name "2026-01-23=Sunset Beachwalk" --join 2026-01-24

# Count photos taken before 01:00 as the previous day
raw-jpg-import import --day-start 01:00 --name "New Year's Eve"

# Show the plan only; nothing is copied or created
raw-jpg-import import --name "Sunset Beachwalk" --dry-run

# No questions (scripts): suggestions accepted, unnamed days get date-only folders
raw-jpg-import import --yes

# Import, then delete the verified originals from the cards (asks you to type DELETE)
raw-jpg-import import --name "Sunset Beachwalk" --delete-originals
```

How a day gets its folder, first match wins: `--name DATE=NAME`; `--join` / `--merge`; the one
existing folder for that day; `--name NAME`; a prompt (in a terminal, `^` means "same folder as
the day above"); a date-only folder with `--yes`. Without a terminal and without names, the
command stops with exit code 2 and says which days need one.

- A day with its own `--name DATE=NAME` starts a new folder even with `--merge`; the days
  after it then join that folder. `--join DATE` together with `--name DATE=...` is an error,
  and so is a date in `--name DATE=NAME` or `--join` that is not a valid date or has no photos.
- Days already imported are left out, but `--join` and `--merge` still reach back to them:
  run the trip command above again on the third evening and the new day goes into the
  trip's folder.
- With `--delete-originals`, the files of days imported in an earlier run are offered for
  deleting too.

More options: `--days 2026-01-23,2026-01-24`, `--since DATE`, `--until DATE`, `--all-days`
(include days with nothing new), `--reimport-culled`, `--copy-unknown` (files from a day
without photos go into the folder of the nearest day imported), `--json` (machine-readable
output), `-q` / `-v` (before or after the command).

Progress goes to stderr (only in a terminal); the plan and the summary go to stdout. Ctrl+C
cancels cleanly; a second Ctrl+C stops at once.

| Exit code | Meaning |
|---|---|
| 0 | Done (or nothing to do) |
| 1 | Finished, but some files failed (see the summary and the log) |
| 2 | Bad arguments, bad config, missing event names, or the import cannot start (no space, destination not writable, ...) |
| 3 | Another import is already running |
| 130 | Cancelled |

## How dates and names work

**Dates.** Each file is dated by the moment the camera took it:

- Photos: the EXIF capture time (`DateTimeOriginal`) in NEF, JPG, HIF and the other raw formats
  (CR3, CR2, ARW, RAF, ORF, RW2, DNG, PEF, ...).
- Nikon videos: the camera's own recording time stored in the clip (Nikon `NCTG` tags).
  Other videos: Apple's creation date, or the QuickTime header time with the time zone worked
  out from the card's file time.
- Anything else, or a file whose date cannot be read: the file's modification time on the
  card. The app says how many files were dated this way.
- The NEF and JPG of one shot always share a date, so a pair never splits across two days.

**Folder names** are `YYYY.MM.DD - Event Name`. Characters Windows does not allow in names
(`< > : " / \ | ? *`) become `-`, so "Sunset: Beach/Walk?" becomes `Sunset- Beach-Walk`. Names
are cut at 80 characters, and the same rules apply on every system so the library syncs
cleanly between them.

**File names** stay as the camera wrote them, with these exceptions:

- **Clashes get `-2`.** If the event folder already holds a different `DSC_0001` (the camera's
  counter went past 9999 and started again, say), the new shot becomes `DSC_0001-2.NEF` and
  `DSC_0001-2.JPG`. The whole shot is renamed together, so Lightroom and friends still see a
  pair. A second card imported later gets the same `-2` names.
- **Variants get `-v2`.** Two different versions of the same shot with the same name (JPEG +
  JPEG slot mode) are both kept; the smaller one gets `-v2`. When the two cards are imported
  at different times, the one imported second gets `-v2`.
- **Already imported files are skipped.** A file counts as already there when the name and
  size match and either the file time or the capture time matches (a file time exactly one
  hour off, as cards show after a daylight-saving change, counts when sampled bytes of both
  files match). Files you imported before and then deleted from your library are not
  brought back (the app keeps a history), unless you tick **Re-import files removed from the
  library** (`--reimport-culled`).
- **Files on both cards** (backup mode) are copied once.

## Safety guarantees

- **Cards are only read.** Nothing is written to a card during an import: no temporary files,
  no logs, no history.
- **Every copy is verified.** Each file is written to a hidden temporary file, flushed to disk,
  read back and compared (SHA-256) with what was read from the card. Only then is it renamed
  to its real name and given the card's timestamps. A half-copied file never sits under a
  real name.
- **Nothing is overwritten.** A different file with the same name gets a `-2` name instead.
- **Re-running is safe.** Files already in the library are recognised and skipped, and an
  interrupted import simply continues.
- **Every file is accounted for.** The summary and the per-import log list what happened to
  each file. Unknown file types are listed, not silently ignored.
- **Nothing starts that cannot finish.** Too little free space, a destination that cannot be
  written, a destination inside a card (or the other way round, or anywhere on a card that a
  source folder is on), or paths too long for Windows stop the import before anything is
  copied. Only one import runs at a time.
- **Deleting from the cards** happens only when you ask for it, after the import. Just before
  each file is deleted, both the card file and the library copy are read again from scratch
  and must still match. Files are kept when they are protected in the camera (read-only), have
  changed since the import, or when the library copy is an online-only cloud placeholder
  (OneDrive, iCloud). Folders and camera system files are never touched.

## File types and config.toml

Extensions match in any case (`.NEF` = `.nef`):

| Type | Goes to | Extensions |
|---|---|---|
| Raw | `RAW/` | nef nrw cr3 cr2 crw arw srf sr2 raf orf ori rw2 rwl raw pef ptx dng srw 3fr fff iiq erf mef mos mrw x3f kdc dcr k25 gpr |
| HEIF | `JPG/` | hif heif heic |
| JPEG | `JPG/` | jpg jpeg jpe |
| Video | `MOV/` | mov mp4 m4v mts m2ts m2t avi mxf mpg nev braw crm r3d |
| Sidecar | next to its photo or video | xmp wav srt xml |
| Ignored (camera files) | not copied, only counted | thm lrv lrf ctg dat bin ndf dsc mrk cpi mpl bdm ini db fir log 422 |

Only the subfolders that receive files are created. Files of any other type are listed in the
summary and left on the card. System folders (`$RECYCLE.BIN`, `.Trashes`, ...), hidden files,
`NC_FLLST.DAT`, the Nikon settings folder and Sony/AVCHD thumbnail folders are skipped.

To change this, create your own config file and edit it:

```sh
raw-jpg-import config init     # writes a commented config.toml (never overwrites)
raw-jpg-import config path     # where it is
raw-jpg-import config show     # the table that is in effect
```

Everything in the new file is commented out; uncomment what you want to change. Examples:

```toml
schema = 1

[categories.heif]
folder = "HEIF"              # .HIF files in their own folder instead of JPG/

[categories.raw]
add = ["cri"]                # add an extension
remove = ["dng"]             # stop treating .dng as raw

[categories.proxy]           # a new type: keep GoPro/DJI .LRV proxies (normally ignored)
folder = "PROXY"
role = "primary"
rank = 4
extensions = ["lrv"]

[import]
unknown = "copy"             # copy unknown file types into OTHER/ instead of leaving them
```

Unknown files are filed by their file time; one from a day without photos goes into the
folder of the nearest day imported.

`extensions` replaces a type's list; `add` and `remove` adjust it. A mistake in the file is
reported with its line and column, and the app will not import until it is fixed; it never
quietly falls back to the defaults. In the app, use **Settings... > File types > Reload** after
editing.

## Where settings, history and logs live

| | Settings (`config.toml`, `settings.json`) | History (`history.sqlite3`) | Logs |
|---|---|---|---|
| Windows | `%APPDATA%\raw-jpg-import\` | `%LOCALAPPDATA%\raw-jpg-import\` | `%LOCALAPPDATA%\raw-jpg-import\logs\` |
| macOS | `~/Library/Application Support/raw-jpg-import/` | same | `~/Library/Logs/raw-jpg-import/` |
| Linux | `~/.config/raw-jpg-import/` | `~/.local/share/raw-jpg-import/` | `~/.local/state/raw-jpg-import/logs/` |

With the **Microsoft Store** Python (source install), Windows redirects these folders into
`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.*\LocalCache\` (`Roaming\` and
`Local\`). The app works the same, but look there for the files. The standalone app and the
python.org installer are not affected.

On Linux the `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` variables are honoured. If
`RAW_JPG_IMPORT_HOME` is set, all of it goes into that one folder instead (useful for trying
things out without touching your real history).

- `config.toml` is yours to edit; the app never rewrites it. `settings.json` is the app's
  memory (last destination, day start, recent names).
- `history.sqlite3` remembers which card file went where. Deleting it is harmless: files
  already in the library are still recognised on disk, but files you removed from the
  library would be imported again.
- Each import writes `import-<id>.log` listing every file's outcome; the newest 50 are kept.
  `raw-jpg-import.log` is the app's general log.

## Troubleshooting

**My camera is plugged in with a USB cable and nothing shows up.** Over USB the camera talks
MTP/PTP and does not appear as a drive, so the app cannot read it. Use a card reader (most
laptops have an SD slot; a USB reader costs little).

**The card is in the reader but not listed.** A card is recognised by a `DCIM` folder at its
top level. Click **Rescan**, or use **Add folder...** (CLI: pass the folder as `SRC`).
On macOS, check **System Settings > Privacy & Security > Files and Folders > RawJpgImport >
Removable Volumes**. On Linux the card must be mounted (opening it in the file manager does
that).

**No window opens (source install).** Tk is missing; see [From source](#from-source-with-python-311-or-newer).
The command line works without it.

**Some photos landed on the wrong day.** Check the camera clock. Files without a readable
capture date use the file time; the app shows how many there were. For shooting past
midnight, use **Day starts at** / `--day-start`.

**"Another import is running" (exit code 3).** Another window or command is importing. If one
crashed, the lock is released by the operating system; just try again.

**"Copies keep failing verification; check the card reader and cable".** The read-back of a
copy differed from the card. Each file gets a second try; after three mismatches the import
stops, because the reader, cable or card is probably failing. Try another reader or port, then
run the import again.

**"The copy does not match the card; not deleted".** When deleting from the cards, a library
copy no longer matched its card file, so the card file was kept. Delete that copy from the
library and import again.

**"File is too large for the destination drive (FAT32 has a 4 GB file limit)".** Long videos
do not fit on FAT32 drives. Use a drive formatted as exFAT, NTFS or APFS.

**"Path too long for Windows".** Windows limits paths to 260 characters. Use a shorter event
name or a destination closer to the drive root.

**Card files kept because "the copy is online-only".** Your library is in OneDrive (or
iCloud) and the copy is a cloud placeholder. The app will not delete a card file based on a
copy it cannot read locally. Set the folder to "Always keep on this device" and try again.

**Something else.** **Open log** on the summary page (or the log folder above) has the details
of every file. Please include it when you report a problem.

## Development

Python 3.11+, standard library only at run time, except Pillow for the optional photo viewer.
Keep the virtual environment (and build
output) **outside** OneDrive, Dropbox or iCloud folders: their file locks break pip and
PyInstaller on Windows.

```sh
# Windows: py -3.11 -m venv %LOCALAPPDATA%\venvs\rji  &&  %LOCALAPPDATA%\venvs\rji\Scripts\activate
python3 -m venv ~/.venvs/rji && source ~/.venvs/rji/bin/activate
pip install -e ".[dev]"

pytest                 # unit, integration and GUI model tests; no display or camera needed
pytest -m gui          # Tk smoke tests (need a display; on Linux CI: xvfb-run -a pytest -m gui)
pytest -m real         # optional: your own camera files in tests/real/data or $RJI_SAMPLES_DIR
pytest --cov
ruff check .
```

The tests never touch real cards or your settings: they build fake cards in temporary folders
and set `RAW_JPG_IMPORT_HOME`. For trying the app by hand, `scripts/make_fake_cards.py` builds
Zf-like cards with real EXIF dates (and `--verify` checks they were not changed):

```sh
python scripts/make_fake_cards.py ~/rji-test --mode raw_jpg
RAW_JPG_IMPORT_HOME=~/rji-test/home raw-jpg-import import ~/rji-test/SD ~/rji-test/MICROSD \
    --dest ~/rji-test/Photos --name "Sunset Beachwalk" --dry-run
python scripts/make_fake_cards.py --verify ~/rji-test
```

[docs/DESIGN.md](docs/DESIGN.md) explains how the engine works and which rules it must never
break.

### Building the standalone app

```sh
pip install -e ".[build]"
python packaging/build.py --smoke --out ~/rji-build     # Windows: --out %LOCALAPPDATA%\rji-build
```

This runs PyInstaller with `packaging/raw_jpg_import.spec`, runs the built app with
`--selftest`, and writes `RawJpgImport-<version>-<platform>.zip` (or `.tar.gz` on Linux) plus a
`.sha256` file to `<out>/dist/release/`. `--out` can also come from `RJI_BUILD_DIR`; without
either, it builds into `dist/` and `build/` in the repository.

### Releasing

1. Set `__version__` in `src/raw_jpg_import/__init__.py` (for a release candidate, e.g.
   `0.2.0rc1`).
2. Commit, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`. The tag must be `v`
   plus the exact version.
3. The Release workflow tests and builds the Windows, macOS (Apple silicon and Intel) and
   Linux apps, the sdist and the wheel, and creates a **draft** GitHub release with
   `SHA256SUMS.txt`. Check it, then publish it.

## License

MIT. See [LICENSE](LICENSE).
