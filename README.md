# Trade Check

Tiny cross-platform desktop tool: capture a region of the screen where an enemy's name is
shown, OCR the name, show what you recorded about that player last time, and save the
current state (**trading / fighting / afk**). Records live in a local SQLite file.

## Run

Pick one:

- **Release build:** double-click `dist/TradeCheck.exe` (or the file from a release). Nothing to
  install. First start takes a few seconds; a `data/` folder appears next to the exe.
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

1. **Select region...** - drag a box over where the enemy name appears in-game. Saved for next time.
   Leave a little space above and beside the name and stop above the "Lvl." line; the preview under
   READ NAME shows exactly what is captured, so check the whole name is inside.
2. **Auto-read** is on by default: once a region exists the app captures it every second (interval
   adjustable, 0.5-5 s) and fills in the name by itself. It skips OCR while the frame is unchanged and
   only switches to a new name after two consistent reads, so a flickering frame cannot flip it.
   Untick the box to go manual; **READ NAME** / F5 always does a one-off read.
3. The panel shows the **previous record** for that name (state, times seen, first/last date), or "no previous record".
4. Fix the name in the text box if OCR got it slightly wrong (auto-read pauses while you type), then click
   **TRADING / FIGHTING / AFK**. Existing records are overwritten.
5. **Records** tab: search, filter by state, double-click to load a name into Home, change state or delete.
6. **Mini mode** (button in the top row): shrinks the app to a small always-on-top overlay with just the
   name, its previous state and the three buttons, so it can sit in front of the game. Drag the text to
   move it (position is remembered), `[ ]` returns to the full window, `X` quits. Auto-read keeps running.
   The app reopens in whichever mode you used last. Works with borderless/windowed games; exclusive
   fullscreen hides every overlay.

Tip: keep the app window (or a second monitor) clear of the capture region.

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
- **Start-up time.** The single file unpacks ~100 MB on every start (a few seconds). The exe keeps its
  database in `data/` next to itself, so it is fully portable.

## Layout

```
run.py              entry point
run.bat / run.sh    one-click start from source (creates .venv on first run)
build.py            PyInstaller single-file build; --release packages release/*.zip
assets/icon.ico     app icon (generated by build.py if missing)
.github/workflows/  tag-triggered multi-OS release build
app/db.py           SQLite: records (name, state, times_seen, dates) + settings
app/capture.py      mss screen grab + drag-to-select overlay
app/ocr.py          preprocessing, OCR engines (RapidOCR default, Tesseract optional), name picking
app/ui.py           tkinter UI (Home + Records tabs)
tools/ocr_tune.py   batch OCR over samples/ for tuning
samples/            drop screenshots here
data/records.sqlite created on first run
```
