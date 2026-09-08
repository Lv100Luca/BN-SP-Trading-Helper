# Trade Check

Tiny cross-platform desktop tool: capture a region of the screen where an enemy's name is
shown, OCR the name, show what you recorded about that player last time, and save the
current state (**trading / fighting / climbing / dropping / stalling / traitor**). Records live in a local SQLite file shared by the source checkout and the packaged exe.

## Run

Pick one:

- **Release build:** double-click `dist/TradeCheck.exe` (or the file from a release). Nothing to
  install. First start takes a few seconds.
- **From source:** double-click `run.bat` (Windows) or run `./run.sh` (macOS/Linux). On first use
  it creates `.venv/` and installs `requirements.txt`, then starts the app. `run.py` also
  re-launches itself inside `.venv` if you start it with a Python that lacks the packages.

Manual setup, if you prefer:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt        # macOS / Linux
.venv\Scripts\python run.py
```

Python 3.10+ with tkinter (bundled on Windows/macOS; on Linux `sudo apt install python3-tk`).

## Flow

1. **Select region...** (on Home until one is set, afterwards in Settings -> Capture) - drag a box over
   where the enemy name appears in-game. Saved for next time. Leave a little space above and beside the
   name and stop above the "Lvl." line; **Test capture** in Settings shows exactly what is captured, so
   check the whole name is inside.
2. **Auto-read** is on by default (Settings -> Capture: toggle and interval, 0.5-5 s): once a region exists
   the app captures it every second and fills in the name by itself. It skips OCR while the frame is
   unchanged and only switches to a new name after two consistent reads, so a flickering frame cannot
   flip it. The grey line under READ NAME shows what auto-read last saw. **READ NAME** / F5 always does
   a one-off read.
3. The panel shows everything known about that name, from both sources at once: a line for **your own
   record** (state, times seen, first/last) and one for **everyone** (the shared table: state, encounters,
   last sighting), both **notes** when they differ, and both **histories** as strips of coloured blocks,
   one per sighting, oldest left, with the share of each state. Hover a block for its time. The headline
   and colour follow whichever source changed last. Or "no previous record".
4. Fix the name in the text box if OCR got it slightly wrong (auto-read pauses while you type), then click
   one of **TRADING / FIGHTING / CLIMBING / DROPPING / STALLING / TRAITOR**. The state becomes the current one
   and is added to the history.
   Misclicked? **Undo last save** takes the encounter back again (also on the mini HUD). Further clicks
   for the same name are ignored for 15 s (a countdown shows), so a double click cannot count twice.
5. **Records** tab (name, state, notes, source, seen, last updated): search, filter by state, double-click
   to load a name into Home, change the state
   (a correction, not an encounter), **Rename...** a record whose name OCR got wrong (merges into the
   right record if one exists), **Notes...** for a free-text note (also on Home; shown in the panel) or
   delete it. **Note presets** (Settings tab: a button name and a text each) appear as large buttons in
   the Notes dialog; one click fills the text in, and you can still type. **Export...** saves your own records as JSON (or CSV for
   Excel); with a search or filter active only the matching rows are exported.
6. **Mini mode** (button in the top row): shrinks the app to a small always-on-top overlay with just the
   name, its previous state, the last ten encounters and the four state buttons, so it can sit in front of
   the game. Drag the text to move it (position is remembered), `undo` takes the last save back, `[ ]`
   returns to the full window, `X` quits. Auto-read keeps running. The app reopens in whichever mode you
   used last. Works with borderless/windowed games; exclusive fullscreen hides every overlay.

Tip: keep the app window (or a second monitor) clear of the capture region.

**macOS:** start `TradeCheck.app` from the zip (right-click > Open the first time; the build is not
notarised). The first **Select region...** asks for *Screen Recording* permission; switch it on under
System Settings > Privacy & Security > Screen Recording, then quit and start the app again. Without it
every capture shows only the wallpaper. Run the game in a window: a full-screen game lives in its own
Space where no overlay can appear. On macOS the region selector is an ordinary window (borderless
windows there do not take key presses), so it sits below the menu bar; right-click or Esc cancels.

## Where the data lives

Both the source checkout and the packaged exe use one shared per-user database:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\TradeCheck
ecords.sqlite` |
| macOS | `~/Library/Application Support/TradeCheck/records.sqlite` |
| Linux | `$XDG_DATA_HOME/TradeCheck/records.sqlite` (default `~/.local/share/...`) |

Set the `TRADECHECK_DATA_DIR` environment variable to use another folder (portable setups). On the
first start of 0.3+, records and settings from the old location (`data/` in the project or next to
the exe) are merged in and the old file is renamed `records.sqlite.migrated`. Back up the shared
file to keep your records; `Export...` in the Records tab gives you a JSON copy.

## Global table (shared)

Besides your own records the app consults a shared lookup table hosted on a small server
([BN-SP-Trading-Helper-API](https://github.com/Lv100Luca/BN-SP-Trading-Helper-API): one Python
file, deployed to the VPS by its CI). The client downloads the whole table at start-up, on
**Refresh now** in the Settings tab and, with **Auto-download** ticked, every sync interval; it keeps the
copy in the local database and looks names up offline. Home and the mini HUD show your own record and
the table's entry side by side (states, notes, histories), so nothing one source knows is hidden by the
other. The Records tab lists both (**Source** filter) and shows both histories of the selected name;
setting a state on a global row copies it, note included, into your records, as does the first save of
a name you only knew from the table.

**Contributing.** With a contributor key from the table admin (`tools/manage_keys.py <server url>
create "Name"`, printed once) your saves go into the shared table: paste the key in Settings, **Save
key** (the app checks it with the server first and only keeps a working one), tick **Upload my saves**.
Every save, correction, undo and rename is queued and pushed on the sync interval, on **Upload now** and
when the app closes. On the server each save is one sighting in that name's history; Undo takes your own
sighting back again, Rename moves your sightings to the right name. Notes travel separately: only the
**Notes...** dialog changes the shared note (an empty note clears it), a state save never carries one.
You can only ever change what you uploaded yourself. A revoked key switches uploads off with a red note in Settings; **Discard queued**
throws away changes that have not been pushed yet.

The admin can also replace the whole table with an export: `tools/publish_global.py <server url>
records.json`. The feature is off until `GLOBAL_TABLE_URL` in `app/__init__.py` is set (or the
`TRADECHECK_GLOBAL_URL` environment variable, for testing).

## Tuning OCR

Drop screenshots into `samples/` (see `samples/README.md`) and run:

```
.venv\Scripts\python tools\ocr_tune.py --save-debug
```

Try `--scale`, `--threshold`, `--invert`, `--engine`; add `--save` to make the app use the
winning options.
For this game's top-right enemy plate use `--rel-region 0.054,0.005,0.135,0.025` (details in
`samples/README.md`). Current default: detector long side capped at 960 px (RapidOCR's default
stretches the short side to 736 px, which broke short names like "Bz"), 30 px padding, 3x upscale,
grayscale; rows that are just a number or "Lvl. 45" are never used as a name. 12/12 crops matched
exactly across both region geometries.

## Release build (single file)

```
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python build.py --release
```

This produces `dist/TradeCheck.exe` and a publish-ready `release/TradeCheck-v<version>-win-x64.zip`
(exe + quick-start README.txt) with a `.sha256` checksum next to it. Upload the zip wherever you
distribute it. Without `--release` you only get the exe. `build.py --console` keeps a console
window for debugging. To check a build on a fresh machine run `TradeCheck.exe --selftest`: it loads
the OCR engine and writes `selftest.txt` ending in `OK`.

Publishing checklist:

1. Bump `__version__` in `app/__init__.py` (it shows in the window title and the zip name).
2. Run `build.py --release` on each OS you ship to. PyInstaller does not cross-compile.
   Alternatively push the project to GitHub and tag it (`git tag v0.1.0 && git push --tags`):
   `.github/workflows/release.yml` builds Windows, macOS and Linux zips and attaches them to a
   GitHub Release automatically.
3. Test the zip on a machine without Python: unzip, run, `--selftest`.

Things users will hit:

- **SmartScreen.** The exe is not code-signed, so Windows shows "Windows protected your PC" on first
  run ("More info" -> "Run anyway"). Only an EV/OV code-signing certificate removes that.
- **Antivirus false positives.** Single-file PyInstaller executables are occasionally flagged. If it
  becomes a problem, sign the binary or ship a folder build (`--onedir`) instead.
- **Start-up time.** The single file unpacks ~100 MB on every start (a few seconds).

## Layout

```
run.py              entry point
run.bat / run.sh    one-click start from source (creates .venv on first run)
build.py            PyInstaller single-file build; --release packages release/*.zip
assets/icon.ico     app icon (generated by build.py if missing)
.github/workflows/  tag-triggered multi-OS release build
app/db.py           SQLite: records, sightings (history), settings, global table copy, upload queue; rename/merge, undo
app/export.py       JSON (default) / CSV export of records (read_records: the input side, used by publish_global)
app/sync.py         global table client: download (ETag), contributor key check, push of queued saves
app/capture.py      mss screen grab + drag-to-select overlay
app/ocr.py          preprocessing, OCR engines (RapidOCR default, Tesseract optional), name picking
app/ui.py           tkinter UI (Home, Records, Settings tabs; mini HUD)
tools/ocr_tune.py   batch OCR over samples/ for tuning
tools/publish_global.py  replace the global table with a records export (admin token)
tools/manage_keys.py     list / create / revoke contributor keys (admin token)
samples/            drop screenshots here
data/               legacy DB location (pre-0.3); migrated automatically
```
