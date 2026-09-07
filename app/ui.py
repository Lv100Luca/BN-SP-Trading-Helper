"""Tkinter UI: Home tab (capture -> OCR -> previous record -> save state), Records tab and, when the
reading log is on (running from source), a Readings tab to review captures and fix misreads."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

from . import REPO_URL, __version__, capture, export, ocr, readings, sync
from .db import STATES, Database, name_key

STATE_LABELS = {"trading": "TRADING", "fighting": "FIGHTING", "afk": "AFK", "fake": "FAKE"}
STATE_COLORS = {"trading": "#2e7d32", "fighting": "#c62828", "afk": "#616161", "fake": "#f9a825"}
STATE_FG = {"trading": "white", "fighting": "white", "afk": "white", "fake": "#212121"}  # button text
STATE_PALE = {"trading": "#e8f5e9", "fighting": "#ffebee", "afk": "#eeeeee", "fake": "#fff8e1"}
GLOBAL_PALE = "#e3f2fd"   # previous-record panel when the state comes from the shared table
PREVIEW_MAX = (520, 140)
READING_PREVIEW_MAX = (620, 160)


def _fingerprint(img: Image.Image) -> np.ndarray:
    """Full-resolution grayscale copy used to tell whether the captured frame changed.

    Screen captures are noise-free, so comparing every pixel is both cheap and exact; a
    downscaled thumbnail would wash out a small name inside a generously drawn region.
    """
    return np.asarray(img.convert("L"), dtype=np.int16)


def _changed(a: np.ndarray, b: np.ndarray, pixel_delta: int = 40, min_pixels: int = 30) -> bool:
    """True if at least `min_pixels` (or 0.1% of the region) moved by more than `pixel_delta`."""
    if a.shape != b.shape:
        return True
    moved = int((np.abs(a - b) > pixel_delta).sum())
    return moved > max(min_pixels, a.size // 1000)


def _overlaps(win: tk.Misc, region: capture.Region, min_fraction: float = 0.10) -> bool:
    """True if the window covers more than `min_fraction` of the capture region's area."""
    x, y, w, h = region
    mx, my, mw, mh = win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height()
    ix = max(0, min(x + w, mx + mw) - max(x, mx))
    iy = max(0, min(y + h, my + mh) - max(y, my))
    return ix * iy > min_fraction * w * h


def fix_dpi() -> None:
    """Make tkinter coordinates match physical pixels on Windows (needed for mss)."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Trade Check v{__version__}")
        self.minsize(660, 600)
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass

        self.db = Database()
        # Running from source: keep every capture a name was read from, so misreads can be
        # inspected and repaired later (Readings tab). Off in the packaged exe unless forced on.
        self.log: readings.ReadingLog | None = readings.ReadingLog(self.db) if readings.enabled() else None
        self.engine: ocr.OcrEngine | None = None
        self._engine_lock = threading.Lock()
        self.region: capture.Region | None = self._load_region()
        self.pre_cfg = ocr.PreprocessConfig.from_dict(
            json.loads(self.db.get_setting("preprocess", "{}") or "{}")
        )

        self.status = tk.StringVar(value="Ready.")
        if self.db.migration_report:
            r = self.db.migration_report
            self.status.set(
                f"Moved records to the shared database ({self.db.path}): {r['added']} added, "
                f"{r['updated']} updated from {r['source']}"
            )
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.home = HomeTab(self.nb, self)
        self.records = RecordsTab(self.nb, self)
        self.nb.add(self.home, text="   Home   ")
        self.nb.add(self.records, text="   Records   ")
        self.readings_tab: ReadingsTab | None = None
        if self.log is not None:
            self.readings_tab = ReadingsTab(self.nb, self)
            self.nb.add(self.readings_tab, text="   Readings   ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_footer()
        self.bind("<F5>", lambda _e: self.home.read_name())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.mini: MiniWindow | None = None
        if self.db.get_setting("mini_mode") == "1":
            self.after(200, self.enter_mini)
        self._sync_results: queue.Queue = queue.Queue()
        self._sync_busy = False
        self._refresh_global_label()
        if sync.table_url():
            self.after(1500, self.refresh_global)  # let the window come up first

    def _build_footer(self) -> None:
        """Status text on the left; a small About section (version + GitHub link) on the right."""
        footer = ttk.Frame(self, relief="sunken", padding=(6, 3))
        footer.pack(fill="x", side="bottom")
        about = ttk.Frame(footer)
        about.pack(side="right", padx=(12, 0))
        ttk.Label(about, text=f"Trade Check v{__version__}  ·", foreground="#666").pack(side="left")
        link = ttk.Label(about, text="GitHub", foreground="#1565c0", cursor="hand2", font=("", 9, "underline"))
        link.pack(side="left", padx=(4, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(REPO_URL))
        self.global_var: tk.StringVar | None = None
        self.global_btn: ttk.Button | None = None
        if sync.table_url():
            ttk.Label(about, text="  ·", foreground="#666").pack(side="left")
            self.global_var = tk.StringVar()
            ttk.Label(about, textvariable=self.global_var, foreground="#666").pack(side="left", padx=(4, 0))
            self.global_btn = ttk.Button(about, text="Refresh", width=8, command=self.refresh_global)
            self.global_btn.pack(side="left", padx=(6, 0))
            ttk.Label(about, text="Prefer:", foreground="#666").pack(side="left", padx=(10, 0))
            self.prefer_var = tk.StringVar(value=self.db.lookup_preference())
            prefer = ttk.Combobox(about, textvariable=self.prefer_var, values=("local", "global"),
                                  state="readonly", width=7)
            prefer.pack(side="left", padx=(4, 0))
            prefer.bind("<<ComboboxSelected>>", lambda _e: self._set_preference())
        ttk.Label(footer, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------ global table
    def _set_preference(self) -> None:
        pref = self.prefer_var.get()
        self.db.set_setting("lookup_prefer", pref)
        self.home.lookup(quiet=True)
        first, second = ("your own records", "the global table") if pref == "local" else ("the global table", "your own records")
        self.set_status(f"Lookups now prefer {first}, falling back to {second}.")

    def _refresh_global_label(self) -> None:
        if self.global_var is None:
            return
        info = self.db.global_info()
        if not info["synced_at"]:
            self.global_var.set("Global table: not downloaded yet")
        else:
            self.global_var.set(f"Global table: {info['count']} names (synced {info['synced_at'][11:16]})")

    def refresh_global(self) -> None:
        """Download the shared table in a worker thread; the result is applied on the UI thread."""
        url = sync.table_url()
        if not url or self._sync_busy:
            return
        self._sync_busy = True
        if self.global_btn is not None:
            self.global_btn.configure(state="disabled")
        etag = self.db.global_info()["etag"]

        def work() -> None:  # no tkinter or sqlite calls in here
            try:
                self._sync_results.put(("ok", sync.fetch_table(url, etag)))
            except sync.SyncError as exc:
                self._sync_results.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._sync_results.put(("error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_sync)

    def _poll_sync(self) -> None:
        try:
            kind, payload = self._sync_results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_sync)
            return
        self._sync_busy = False
        if self.global_btn is not None:
            self.global_btn.configure(state="normal")
        if kind == "error":
            self.set_status(f"Global table: {payload}")
        elif payload.status == "unchanged":
            self.db.touch_global()
            self.set_status("Global table is up to date.")
        else:
            n = self.db.replace_global(payload.records, payload.version, payload.updated_at, payload.etag)
            self.set_status(f"Global table updated: {n} names (v{payload.version}).")
        self._refresh_global_label()
        self.home.lookup(quiet=True)  # the name on screen may now have a global hit

    # --------------------------------------------------------------- mini mode
    def enter_mini(self) -> None:
        if self.mini is not None:
            return
        self.withdraw()
        self.mini = MiniWindow(self)
        self.mini.set_record(self.home.current_record, self.home.name_var.get().strip(), self.home.current_source)
        self.db.set_setting("mini_mode", "1")

    def exit_mini(self) -> None:
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self.db.set_setting("mini_mode", "0")
        self.deiconify()
        self.lift()

    def update_mini(self, rec, name: str, source: str = "local") -> None:
        if self.mini is not None:
            self.mini.set_record(rec, name, source)

    # ------------------------------------------------------------------ helpers
    def set_status(self, text: str) -> None:
        self.status.set(text)

    def _on_tab_changed(self, _e: tk.Event) -> None:
        tab = self.nb.nametowidget(self.nb.select())
        if tab is self.records:
            self.records.refresh()
        elif tab is self.readings_tab:
            self.readings_tab.refresh()

    def _load_region(self) -> capture.Region | None:
        raw = self.db.get_setting("region")
        if not raw:
            return None
        try:
            x, y, w, h = (int(v) for v in json.loads(raw))
            return (x, y, w, h)
        except Exception:
            return None

    def save_region(self, region: capture.Region) -> None:
        self.region = region
        self.db.set_setting("region", json.dumps(list(region)))

    def engine_name(self) -> str:
        """Read on the UI thread only: the SQLite connection must not be touched from workers."""
        return self.db.get_setting("ocr_engine", "auto") or "auto"

    def get_engine(self, name: str = "auto") -> ocr.OcrEngine:
        """Safe to call from a worker thread; loads the engine once."""
        with self._engine_lock:
            if self.engine is None:
                self.engine = ocr.make_engine(name)
            return self.engine

    def _on_close(self) -> None:
        self.home.stop_auto()
        self.db.close()
        self.destroy()


# ============================================================================ Home
class HomeTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._preview_img: ImageTk.PhotoImage | None = None
        self._busy = False
        self._results: queue.Queue = queue.Queue()  # worker thread -> UI thread hand-off
        self.current_record = None      # sqlite3.Row shown in the previous-record panel (or None)
        self.current_source = ""        # "local" (your records), "global" (shared table) or ""
        self._typing_until = 0.0        # auto-read leaves the name box alone until this time
        self._auto_job: str | None = None
        self._auto_run = False          # is the OCR currently in flight an auto-read?
        self._candidate = ""            # name seen once, waiting for a confirming read
        self._last_fp: np.ndarray | None = None
        self._last_result: tuple | None = None
        self.reading_id: int | None = None  # log row of the capture the current name came from
        self._auto_var = tk.BooleanVar(value=app.db.get_setting("auto_read", "1") == "1")
        try:
            interval = float(app.db.get_setting("auto_interval", "1.0") or 1.0)
        except ValueError:
            interval = 1.0
        self._interval_var = tk.DoubleVar(value=interval)
        self._build()
        self._refresh_region_label()
        if self._auto_var.get() and self.app.region:
            self.after(500, self._toggle_auto)  # start reading right away
        elif self._auto_var.get():
            self.auto_status.set("auto-read starts once a region is selected")

    def _build(self) -> None:
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.region_var = tk.StringVar()
        ttk.Label(row, textvariable=self.region_var).pack(side="left")
        ttk.Button(row, text="Test capture", command=self.test_capture).pack(side="right")
        ttk.Button(row, text="Select region...", command=self.select_region).pack(side="right", padx=(0, 6))
        ttk.Button(row, text="Mini mode", command=self.app.enter_mini).pack(side="right", padx=(0, 6))

        self.read_btn = tk.Button(
            self, text="READ NAME  (F5)", font=("", 14, "bold"), height=2,
            bg="#1565c0", fg="white", activebackground="#0d47a1", activeforeground="white",
            command=self.read_name,
        )
        self.read_btn.pack(fill="x", pady=(10, 4))

        auto = ttk.Frame(self)
        auto.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(auto, text="Auto-read every", variable=self._auto_var, command=self._toggle_auto).pack(side="left")
        ttk.Spinbox(auto, from_=0.5, to=5.0, increment=0.5, width=4, textvariable=self._interval_var,
                    command=self._toggle_auto).pack(side="left", padx=(4, 2))
        ttk.Label(auto, text="s").pack(side="left")
        self.auto_status = tk.StringVar(value="auto-read off")
        ttk.Label(auto, textvariable=self.auto_status, foreground="#666").pack(side="right")

        self.preview = ttk.Label(self, anchor="center", relief="groove", text="(capture preview)")
        self.preview.pack(fill="x", ipady=6)
        self.ocr_info = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.ocr_info, foreground="#666", wraplength=540, justify="left").pack(
            anchor="w", pady=(2, 0)
        )

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(10, 4))
        ttk.Label(row, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.name_var, font=("", 12))
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.lookup())
        entry.bind("<Key>", lambda _e: setattr(self, "_typing_until", time.time() + 3.0))
        self.name_entry = entry
        ttk.Button(row, text="Lookup", command=self.lookup).pack(side="left")

        self.prev_frame = tk.Frame(self, bd=2, relief="ridge", padx=10, pady=10)
        self.prev_frame.pack(fill="x", pady=6)
        self.prev_title = tk.Label(self.prev_frame, text="No name read yet", font=("", 13, "bold"), anchor="w")
        self.prev_title.pack(fill="x")
        self.prev_detail = tk.Label(self.prev_frame, text="", justify="left", anchor="w")
        self.prev_detail.pack(fill="x")

        ttk.Label(self, text="Record current state (overwrites the previous one):").pack(anchor="w", pady=(8, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        for st in STATES:
            tk.Button(
                row, text=STATE_LABELS[st], font=("", 12, "bold"), height=2,
                bg=STATE_COLORS[st], fg=STATE_FG[st],
                activebackground=STATE_COLORS[st], activeforeground=STATE_FG[st],
                command=lambda s=st: self.save_state(s),
            ).pack(side="left", fill="x", expand=True, padx=3)

    # ------------------------------------------------------------------ region
    def _refresh_region_label(self) -> None:
        r = self.app.region
        if r:
            self.region_var.set(f"Region: x={r[0]} y={r[1]}  {r[2]} x {r[3]} px")
        else:
            self.region_var.set("Region: not set  ->  click 'Select region...'")

    def select_region(self) -> None:
        self.app.withdraw()
        self.app.update()
        try:
            region = capture.RegionSelector(self.app).select()
        finally:
            self.app.deiconify()
            self.app.lift()
        if region:
            self.app.save_region(region)
            self._refresh_region_label()
            self.app.set_status(f"Region saved: {region}")
            self.test_capture()
            if self._auto_var.get():
                self._toggle_auto()  # (re)start auto-read on the new region
        else:
            self.app.set_status("Region selection cancelled.")

    def _grab(self):
        if not self.app.region:
            messagebox.showinfo("No region", "First select the screen region where the enemy name appears.")
            return None
        try:
            return capture.grab(self.app.region)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Capture failed", str(exc))
            return None

    def _show_preview(self, img) -> None:
        im = img.copy()
        im.thumbnail(PREVIEW_MAX)
        self._preview_img = ImageTk.PhotoImage(im)
        self.preview.configure(image=self._preview_img, text="")

    def test_capture(self) -> None:
        img = self._grab()
        if img is not None:
            self._show_preview(img)
            self.app.set_status(f"Captured {img.width} x {img.height} px.")

    # --------------------------------------------------------------------- OCR
    def read_name(self) -> None:
        """Manual read (button / F5)."""
        if self._busy:
            return
        img = self._grab()
        if img is None:
            return
        self._show_preview(img)
        self.read_btn.configure(state="disabled", text="READING...")
        self.app.set_status("Running OCR... (first run loads the model, may take a few seconds)")
        self._start_ocr(img, auto=False)

    def _start_ocr(self, img, auto: bool) -> None:
        self._busy = True
        self._auto_run = auto
        cfg = self.app.pre_cfg
        engine_name = self.app.engine_name()  # DB access stays on the UI thread

        def work() -> None:  # no tkinter or sqlite calls in here
            try:
                engine = self.app.get_engine(engine_name)
                name, conf, lines, _ = ocr.read_name(img, engine, cfg)
                self._results.put(("ok", engine.name, name, conf, lines, img))
            except Exception as exc:  # noqa: BLE001
                self._results.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_ocr)

    def _poll_ocr(self) -> None:
        try:
            item = self._results.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_ocr)
            return
        self._busy = False
        self.read_btn.configure(state="normal", text="READ NAME  (F5)")
        if item[0] != "ok":
            self._on_ocr_error(item[1])
        elif self._auto_run:
            self._apply_auto_result(*item[1:])
        else:
            self._on_ocr_done(*item[1:])

    def _on_ocr_done(self, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine], img) -> None:
        self._log_reading(img, engine_name, name, conf, lines, "manual")  # manual reads are always kept
        if not name:
            self.ocr_info.set(f"[{engine_name}] no text found")
            self.app.set_status("OCR found no text. Check the region with 'Test capture'.")
            return
        self.ocr_info.set(f"[{engine_name}] " + self._alts(lines))
        self.name_var.set(name)
        self.lookup()
        self.app.set_status(f"Read '{name}' ({conf:.0%} confidence). Fix the name if needed, then pick a state.")

    def _on_ocr_error(self, exc: Exception) -> None:
        self.app.set_status("OCR failed.")
        if self._auto_var.get():
            self._set_auto(False)
        messagebox.showerror("OCR error", str(exc))

    @staticmethod
    def _alts(lines: list[ocr.OcrLine]) -> str:
        return "  |  ".join(f"{l.text} ({l.confidence:.2f})" for l in lines[:4])

    def _log_reading(self, img, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine], source: str) -> None:
        """Remember this capture as the origin of the name now in the box (no-op when the log is off)."""
        if self.app.log is None or img is None:
            return
        try:
            self.reading_id = self.app.log.add(
                img, engine=engine_name, name=name, conf=conf, lines=lines,
                region=self.app.region, cfg=self.app.pre_cfg, source=source,
            )
        except Exception as exc:  # noqa: BLE001 - a debugging aid must never break a read
            print(f"reading log failed: {exc}", file=sys.stderr)

    # --------------------------------------------------------------- auto-read
    def _set_auto(self, on: bool) -> None:
        self._auto_var.set(on)
        self._toggle_auto()

    def _toggle_auto(self) -> None:
        on = bool(self._auto_var.get())
        self.stop_auto()
        if on and not self.app.region:
            self._auto_var.set(False)
            on = False
            messagebox.showinfo("No region", "Select the screen region first, then enable auto-read.")
        self.app.db.set_setting("auto_read", "1" if on else "0")
        self.app.db.set_setting("auto_interval", f"{self._interval()}")
        if on:
            self._last_fp = None
            self._candidate = ""
            self.auto_status.set("auto-read on")
            self._auto_job = self.after(100, self._auto_tick)
        else:
            self.auto_status.set("auto-read off")

    def stop_auto(self) -> None:
        if self._auto_job is not None:
            self.after_cancel(self._auto_job)
            self._auto_job = None

    def _interval(self) -> float:
        try:
            return min(10.0, max(0.2, float(self._interval_var.get())))
        except (tk.TclError, ValueError):
            return 1.0

    def _auto_tick(self) -> None:
        self._auto_job = None
        if not self._auto_var.get():
            return
        try:
            self._auto_step()
        finally:
            if self._auto_var.get():
                self._auto_job = self.after(int(self._interval() * 1000), self._auto_tick)

    def _auto_step(self) -> None:
        if self._busy or not self.app.region:
            return
        if self.app.mini is not None and _overlaps(self.app.mini, self.app.region):
            self.auto_status.set("overlay covers the capture region - drag it away")
            return
        try:
            img = capture.grab(self.app.region)
        except Exception as exc:  # noqa: BLE001
            self.auto_status.set(f"capture failed: {exc}")
            self._set_auto(False)
            return
        fp = _fingerprint(img)
        if self._last_fp is not None and not _changed(fp, self._last_fp):
            if self._candidate and self._last_result:
                self._apply_auto_result(*self._last_result)  # same frame => same text: confirms
            return
        self._last_fp = fp
        self._show_preview(img)
        self._start_ocr(img, auto=True)

    def _apply_auto_result(self, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine], img) -> None:
        self._last_result = (engine_name, name, conf, lines, img)
        stamp = time.strftime("%H:%M:%S")
        if not name:
            self.auto_status.set(f"{stamp}  nothing readable")
            return
        if name_key(name) == name_key(self.name_var.get()):
            self._candidate = ""
            self.auto_status.set(f"{stamp}  {name} ({conf:.0%})")
            return
        if name_key(name) != name_key(self._candidate):
            self._candidate = name  # new name: wait for a second consistent read before switching
            self.auto_status.set(f"{stamp}  confirming '{name}'...")
            return
        if time.time() < self._typing_until:
            self.auto_status.set(f"{stamp}  saw '{name}' (paused while you type)")
            return
        self._candidate = ""
        self._log_reading(img, engine_name, name, conf, lines, "auto")  # only the read that switched the name
        self.name_var.set(name)
        self.lookup()
        self.ocr_info.set(f"[{engine_name}] " + self._alts(lines))
        self.auto_status.set(f"{stamp}  {name} ({conf:.0%})")
        self.app.set_status(f"Auto-read '{name}' ({conf:.0%}).")

    # ----------------------------------------------------------------- records
    def set_name(self, name: str) -> None:
        """Put a name in the box that did not come from the last capture (Records / Readings tab)."""
        self.reading_id = None
        self.name_var.set(name)
        self.lookup()

    def lookup(self, quiet: bool = False) -> None:
        """Show the record for the name in the box from the preferred source (footer setting), the
        other source as fallback. `quiet` re-runs the lookup after a global table refresh or a
        preference change and does nothing when the box is empty."""
        name = self.name_var.get().strip()
        if quiet and not name:
            return
        if not name:
            self._show_prev(None, name, "")
            return
        local, glob = self.app.db.find_both(name)
        rec, source = self.app.db.find(name)
        self._show_prev(rec, name, source, other=(glob if source == "local" else local))

    def _show_prev(self, rec, name: str, source: str = "local", other=None) -> None:
        """`other` is the record from the source that did not win (shown as a hint), if any."""
        self.current_record = rec
        self.current_source = source if rec is not None else ""
        self.app.update_mini(rec, name, self.current_source)
        if not name:
            self._paint_prev("No name read yet", "", bg=None, fg="black")
        elif rec is None:
            self._paint_prev(f"{name}: no previous record", "First time seeing this player.", bg="#fff8e1", fg="#795548")
        elif source == "global":
            st = rec["state"]
            notes = f"   |   {rec['notes']}" if rec["notes"] else ""
            own = (f"Your own record: {STATE_LABELS[other['state']]} (seen {other['times_seen']}x)"
                   if other is not None else "Not in your own records")
            self._paint_prev(
                f"{rec['name']}: {STATE_LABELS[st]} in the global table",
                f"{own}   |   table entry from {rec['updated_at'][:10]}{notes}",
                bg=GLOBAL_PALE, fg=STATE_COLORS[st],
            )
        else:
            st = rec["state"]
            hint = f"   |   global table: {STATE_LABELS[other['state']]}" if other is not None else ""
            self._paint_prev(
                f"{rec['name']}: previously {STATE_LABELS[st]}",
                f"Seen {rec['times_seen']}x   |   first {rec['created_at']}   |   last {rec['updated_at']}{hint}",
                bg=STATE_PALE[st], fg=STATE_COLORS[st],
            )

    def _paint_prev(self, title: str, detail: str, bg: str | None, fg: str) -> None:
        bg = bg or self.app.cget("bg")
        for w in (self.prev_frame, self.prev_title, self.prev_detail):
            w.configure(bg=bg)
        self.prev_title.configure(text=title, fg=fg)
        self.prev_detail.configure(text=detail, fg=fg)

    def save_state(self, state: str) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showinfo("No name", "Read or type a name first.")
            return
        prev = self.app.db.get(name)
        rec = self.app.db.upsert(name, state)
        if self.app.log is not None and self.reading_id is not None:
            self.app.db.mark_reading_saved(self.reading_id, rec["name"], state)
        self._show_prev(rec, name, "local", other=self.app.db.get_global(name))
        if prev is not None and prev["state"] != state:
            self.app.set_status(
                f"{rec['name']}: {STATE_LABELS[prev['state']]} -> {STATE_LABELS[state]} (overwritten)"
            )
        else:
            self.app.set_status(f"{rec['name']}: saved as {STATE_LABELS[state]}")


# ========================================================================= Records
class RecordsTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._build()
        self.refresh()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(top, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=6)
        self.filter_var = tk.StringVar(value="all")
        combo = ttk.Combobox(top, textvariable=self.filter_var, values=("all", *STATES), state="readonly", width=9)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        # Which table(s) to list. Only your own records when the global table feature is off.
        self.source_var = tk.StringVar(value="local")
        if sync.table_url():
            saved = self.app.db.get_setting("records_source", "both") or "both"
            self.source_var.set(saved if saved in Database.SOURCES else "both")
            ttk.Label(top, text="Source:").pack(side="left", padx=(10, 0))
            src = ttk.Combobox(top, textvariable=self.source_var, values=Database.SOURCES, state="readonly", width=7)
            src.pack(side="left", padx=(4, 0))
            src.bind("<<ComboboxSelected>>", lambda _e: self._source_changed())

        self.count_var = tk.StringVar()
        ttk.Label(self, textvariable=self.count_var, foreground="#666").pack(anchor="w", pady=(6, 2))

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        cols = ("name", "state", "source", "seen", "updated")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in (
            ("name", "Name", 220, "w"),
            ("state", "State", 90, "center"),
            ("source", "Source", 60, "center"),
            ("seen", "Seen", 60, "center"),
            ("updated", "Last updated", 150, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        for st in STATES:
            self.tree.tag_configure(st, foreground=STATE_COLORS[st])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.load_selected)
        self.tree.bind("<Delete>", self.delete_selected)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Load into Home", command=self.load_selected).pack(side="left")
        ttk.Label(btns, text="Set:").pack(side="left", padx=(10, 0))
        for st in STATES:
            tk.Button(
                btns, text=STATE_LABELS[st], bg=STATE_COLORS[st], fg=STATE_FG[st],
                activebackground=STATE_COLORS[st], activeforeground=STATE_FG[st],
                command=lambda s=st: self.set_selected_state(s),
            ).pack(side="left", padx=(4, 0))
        tools = ttk.Frame(self)
        tools.pack(fill="x", pady=(6, 0))
        ttk.Button(tools, text="Import...", command=self.import_rows).pack(side="left")
        ttk.Button(tools, text="Export...", command=self.export_rows).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Delete", command=self.delete_selected).pack(side="right")
        ttk.Button(tools, text="Refresh", command=self.refresh).pack(side="right", padx=(0, 6))

    def _source_changed(self) -> None:
        self.app.db.set_setting("records_source", self.source_var.get())
        self.refresh()

    @staticmethod
    def _iid(source: str, name: str) -> str:
        return f"{source}:{name}"

    def refresh(self) -> None:
        rows, _ = self._current_rows()
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            seen = r["times_seen"] if r["source"] == "local" else "-"
            self.tree.insert(
                "", "end", iid=self._iid(r["source"], r["name"]),
                values=(r["name"], STATE_LABELS[r["state"]], r["source"], seen, r["updated_at"]),
                tags=(r["state"],),
            )
        keep = [i for i in (self._iid(r["source"], r["name"]) for r in rows) if i in selected]
        if keep:
            self.tree.selection_set(keep)
        source = self.source_var.get()
        parts = []
        if source in ("both", "local"):
            c = self.app.db.counts("local")
            parts.append(f"{sum(c.values())} own (trading {c.get('trading', 0)}, fighting {c.get('fighting', 0)}, "
                         f"afk {c.get('afk', 0)}, fake {c.get('fake', 0)})")
        if source in ("both", "global"):
            parts.append(f"{sum(self.app.db.counts('global').values())} global")
        self.count_var.set(f"{len(rows)} shown / " + ", ".join(parts))

    def _current_rows(self):
        q = self.search_var.get().strip()
        f = self.filter_var.get()
        rows = self.app.db.all(q, None if f == "all" else f, self.source_var.get())
        return rows, (q == "" and f == "all")

    def export_rows(self) -> None:
        """Save the rows currently listed (all records unless a search/filter is active) as JSON (or CSV)."""
        rows, is_everything = self._current_rows()
        if not rows:
            messagebox.showinfo("Export", "No records to export.")
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export records", defaultextension=".json",
            initialfile=f"trade-check-records-{time.strftime('%Y-%m-%d')}.json",
            filetypes=[("JSON", "*.json"), ("CSV (Excel)", "*.csv")],
        )
        if not path:
            return
        try:
            n = export.export_records(rows, path)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        scope = "all records" if is_everything else "the records currently shown"
        self.app.set_status(f"Exported {n} ({scope}) to {path}")

    def import_rows(self) -> None:
        """Merge records from a JSON (or CSV) export or another records.sqlite into this database."""
        path = filedialog.askopenfilename(
            parent=self, title="Import records",
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv"), ("SQLite database", "*.sqlite"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            report = self.app.db.merge_records(export.read_records(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import failed", str(exc))
            return
        self.refresh()
        self.app.set_status(
            f"Imported from {Path(path).name}: {report['added']} added, {report['updated']} updated, "
            f"{report['unchanged']} unchanged, {report['skipped']} skipped"
        )

    def _selected_names(self) -> list[str]:
        return [self.tree.item(i, "values")[0] for i in self.tree.selection()]

    def _selected(self) -> list[tuple[str, str]]:
        """(name, source) per selected row."""
        return [(v[0], v[2]) for v in (self.tree.item(i, "values") for i in self.tree.selection())]

    def load_selected(self, _e=None) -> None:
        names = self._selected_names()
        if not names:
            return
        self.app.home.set_name(names[0])
        self.app.nb.select(0)

    def set_selected_state(self, state: str) -> None:
        """Own records change state in place. A global row cannot be edited, so setting a state on
        it creates (or updates) your own record for that name instead."""
        picked = self._selected()
        adopted = 0
        for name, source in picked:
            if source == "local":
                self.app.db.set_state(name, state)
            else:
                glob = self.app.db.get_global(name)
                self.app.db.upsert(name, state, glob["notes"] if glob is not None else None)
                adopted += 1
        self.refresh()
        if picked:
            extra = f" ({adopted} copied from the global table into your records)" if adopted else ""
            self.app.set_status(f"Set {len(picked)} record(s) to {STATE_LABELS[state]}{extra}.")

    def delete_selected(self, _e=None) -> None:
        picked = self._selected()
        names = [n for n, src in picked if src == "local"]
        skipped = len(picked) - len(names)
        if not picked:
            return
        if not names:
            messagebox.showinfo("Delete", "Global table entries cannot be deleted; only your own records can.")
            return
        if not messagebox.askyesno("Delete", f"Delete {len(names)} record(s)?"):
            return
        for n in names:
            self.app.db.delete(n)
        self.refresh()
        note = f" ({skipped} global table entries skipped)" if skipped else ""
        self.app.set_status(f"Deleted {len(names)} record(s){note}.")


# ======================================================================== Readings
class ReadingsTab(ttk.Frame):
    """Every capture the app took a name from (running from source only). Pick one, look at the
    image, and either confirm the read or type the right name: the fix is stored on the reading
    and, if a state was saved from it, the record is renamed (or merged into the correct one)."""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._img: ImageTk.PhotoImage | None = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        ttk.Label(
            self, foreground="#666", wraplength=620, justify="left",
            text="Captures behind every name the app read (kept because you run from source). Select a row to "
                 "see the image; type the real name and click Fix to correct it - a record saved under the "
                 "misread is renamed too.",
        ).pack(anchor="w", pady=(0, 6))
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(top, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=6)
        self.filter_var = tk.StringVar(value="all")
        combo = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=10,
                             values=("all", "saved", "unchecked", "fixed"))
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        self.count_var = tk.StringVar()
        ttk.Label(self, textvariable=self.count_var, foreground="#666").pack(anchor="w", pady=(6, 2))

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        cols = ("time", "read", "conf", "src", "saved", "state", "check")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended", height=9)
        for col, text, width, anchor in (
            ("time", "Time", 125, "w"),
            ("read", "Read as", 170, "w"),
            ("conf", "Conf", 50, "center"),
            ("src", "Via", 55, "center"),
            ("saved", "Saved as", 150, "w"),
            ("state", "State", 75, "center"),
            ("check", "Check", 150, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("read", "saved", "check"))
        self.tree.tag_configure("fixed", foreground="#c62828")
        self.tree.tag_configure("ok", foreground="#2e7d32")
        self.tree.tag_configure("empty", foreground="#9e9e9e")
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", self.delete_selected)

        self.preview = ttk.Label(self, anchor="center", relief="groove", text="(select a reading)")
        self.preview.pack(fill="x", pady=(8, 2), ipady=6)
        self.detail_var = tk.StringVar()
        ttk.Label(self, textvariable=self.detail_var, foreground="#666", wraplength=620, justify="left").pack(anchor="w")

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Correct name:").pack(side="left")
        self.fix_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.fix_var, font=("", 11))
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.fix_selected())
        tk.Button(row, text="Fix", font=("", 10, "bold"), bg="#1565c0", fg="white",
                  activebackground="#0d47a1", activeforeground="white", padx=12,
                  command=self.fix_selected).pack(side="left")
        ttk.Button(row, text="Read was right", command=self.confirm_selected).pack(side="left", padx=(6, 0))

        tools = ttk.Frame(self)
        tools.pack(fill="x", pady=(6, 0))
        ttk.Button(tools, text="Load into Home", command=self.load_selected).pack(side="left")
        ttk.Button(tools, text="Open folder", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Delete", command=self.delete_selected).pack(side="right")
        ttk.Button(tools, text="Refresh", command=self.refresh).pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------- table
    @staticmethod
    def _is_fixed(r) -> bool:
        """True when a human changed the name (as opposed to confirming or not checking it)."""
        return bool(r["fixed_name"]) and r["fixed_name"].lower() != (r["read_name"] or "").lower()

    def refresh(self) -> None:
        rows = self.app.db.readings(self.search_var.get().strip())
        f = self.filter_var.get()
        if f == "saved":
            rows = [r for r in rows if r["saved_name"]]
        elif f == "unchecked":
            rows = [r for r in rows if not r["fixed_name"]]
        elif f == "fixed":
            rows = [r for r in rows if self._is_fixed(r)]
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            if not r["fixed_name"]:
                check, tags = "", (("empty",) if not r["read_name"] else ())
            elif self._is_fixed(r):
                check, tags = f"-> {r['fixed_name']}", ("fixed",)
            else:
                check, tags = "ok", ("ok",)
            self.tree.insert(
                "", "end", iid=str(r["id"]),
                values=(r["ts"], r["read_name"] or "(nothing readable)", f"{r['confidence']:.2f}", r["source"],
                        r["saved_name"], STATE_LABELS.get(r["saved_state"], ""), check),
                tags=tags,
            )
        keep = [str(r["id"]) for r in rows if str(r["id"]) in selected]
        if keep:
            self.tree.selection_set(keep)
        else:
            self._show(None)
        n_saved = sum(1 for r in rows if r["saved_name"])
        n_fixed = sum(1 for r in rows if self._is_fixed(r))
        self.count_var.set(f"{len(rows)} readings shown   ({n_saved} led to a saved state, {n_fixed} fixed)   "
                           f"folder: {self.app.log.dir}")

    def _selected_ids(self) -> list[int]:
        return [int(i) for i in self.tree.selection()]

    def _on_select(self, _e=None) -> None:
        ids = self._selected_ids()
        self._show(self.app.db.reading(ids[0]) if ids else None)

    def _show(self, r) -> None:
        if r is None:
            self.preview.configure(image="", text="(select a reading)")
            self._img = None
            self.detail_var.set("")
            self.fix_var.set("")
            return
        img = self.app.log.image(r)
        if img is None:
            self.preview.configure(image="", text="(image missing)")
            self._img = None
        else:
            im = img.copy()
            if im.width * 2 <= READING_PREVIEW_MAX[0] and im.height * 2 <= READING_PREVIEW_MAX[1]:
                im = im.resize((im.width * 2, im.height * 2), Image.NEAREST)  # small crops: show 2x
            im.thumbnail(READING_PREVIEW_MAX)
            self._img = ImageTk.PhotoImage(im)
            self.preview.configure(image=self._img, text="")
        try:
            alts = json.loads(r["alternatives"] or "[]")
        except ValueError:
            alts = []
        alt_text = "  |  ".join(f"{t} ({c:.2f})" for t, c in alts) or "-"
        fixed = f"   fixed {r['fixed_at']}" if r["fixed_name"] else ""
        self.detail_var.set(f"[{r['engine']}] rows: {alt_text}\nregion {r['region'] or '-'}   {r['image']}{fixed}")
        self.fix_var.set(r["fixed_name"] or r["read_name"])

    # ----------------------------------------------------------------- actions
    def confirm_selected(self) -> None:
        ids = self._selected_ids()
        for rid in ids:
            r = self.app.db.reading(rid)
            if r is not None and r["read_name"]:
                self.app.db.fix_reading(rid, r["read_name"])
        self.refresh()
        if ids:
            self.app.set_status(f"Marked {len(ids)} reading(s) as read correctly.")

    def fix_selected(self) -> None:
        ids = self._selected_ids()
        new = " ".join(self.fix_var.get().split())
        if not ids:
            messagebox.showinfo("Fix reading", "Select the reading(s) to fix first.")
            return
        if not new:
            messagebox.showinfo("Fix reading", "Type the correct name first.")
            return
        # Records saved under the misread: repair each distinct one once, after asking.
        wrong_names: list[str] = []
        for rid in ids:
            r = self.app.db.reading(rid)
            if r is not None and r["saved_name"] and r["saved_name"].lower() != new.lower():
                if r["saved_name"].lower() not in {w.lower() for w in wrong_names}:
                    wrong_names.append(r["saved_name"])
        results = []
        for old in wrong_names:
            src = self.app.db.get(old, loose=False)
            if src is None:
                continue  # already renamed or deleted; nothing left to repair
            dst = self.app.db.get(new)
            if dst is not None and dst["id"] != src["id"]:
                what = (f"'{new}' already has a record ({STATE_LABELS[dst['state']]}, seen {dst['times_seen']}x).\n\n"
                        f"Merge '{src['name']}' ({STATE_LABELS[src['state']]}, seen {src['times_seen']}x) into it?")
            else:
                what = f"Rename the record '{src['name']}' ({STATE_LABELS[src['state']]}, seen {src['times_seen']}x) to '{new}'?"
            if not messagebox.askyesno("Fix record", what, parent=self):
                continue
            try:
                results.append(self.app.db.rename_record(src["name"], new))
            except (ValueError, LookupError) as exc:
                messagebox.showerror("Fix record", str(exc), parent=self)
        for rid in ids:
            self.app.db.fix_reading(rid, new)
        self.refresh()
        self.app.records.refresh()
        if results and self.app.home.name_var.get().strip().lower() in {r["old"].lower() for r in results}:
            self.app.home.set_name(new)
        done = "; ".join(f"{r['old']} {r['action']} -> {r['name']}" for r in results)
        self.app.set_status(f"Fixed {len(ids)} reading(s) to '{new}'" + (f".  Records: {done}" if done else "."))

    def load_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        r = self.app.db.reading(ids[0])
        name = (r["fixed_name"] or r["saved_name"] or r["read_name"]) if r is not None else ""
        if name:
            self.app.home.set_name(name)
            self.app.nb.select(0)

    def delete_selected(self, _e=None) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if not messagebox.askyesno("Delete", f"Delete {len(ids)} reading(s) and their images? Records are not touched."):
            return
        self.app.log.delete(ids)
        if self.app.home.reading_id in ids:
            self.app.home.reading_id = None
        self.refresh()
        self.app.set_status(f"Deleted {len(ids)} reading(s).")

    def open_folder(self) -> None:
        folder = self.app.log.dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror("Open folder", str(exc))


# ============================================================================ Mini
MINI_BG = "#202124"
MINI_FG = "#f5f5f5"
MINI_DIM = "#9e9e9e"
MINI_STATE_FG = {"trading": "#66bb6a", "fighting": "#ef5350", "afk": "#bdbdbd", "fake": "#ffd54f"}
MINI_NEW_FG = "#80d8ff"


class MiniWindow(tk.Toplevel):
    """Compact always-on-top overlay: current name, previous state, the three buttons.

    Frameless; drag the text area to move it. Position is remembered. Auto-read keeps running
    in the (hidden) main window underneath.
    """

    def __init__(self, app: App) -> None:
        super().__init__(app, bg=MINI_BG)
        self.app = app
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.93)
        except tk.TclError:
            pass
        self._dx = self._dy = 0
        self._pos: str | None = None

        bar = tk.Frame(self, bg=MINI_BG)
        bar.pack(fill="x", padx=6, pady=(4, 0))
        self.name_lbl = tk.Label(bar, textvariable=app.home.name_var, fg=MINI_FG, bg=MINI_BG,
                                 font=("", 12, "bold"), anchor="w")
        self.name_lbl.pack(side="left", fill="x", expand=True)
        small = dict(bg=MINI_BG, fg=MINI_DIM, bd=0, activebackground="#3a3a3a", activeforeground="white")
        tk.Button(bar, text=" X ", command=app._on_close, **small).pack(side="right")
        tk.Button(bar, text=" [ ] ", command=app.exit_mini, **small).pack(side="right")

        self.state_lbl = tk.Label(self, text="", fg=MINI_DIM, bg=MINI_BG, font=("", 10, "bold"), anchor="w")
        self.state_lbl.pack(fill="x", padx=6)

        btns = tk.Frame(self, bg=MINI_BG)
        btns.pack(fill="x", padx=4, pady=4)
        for st in STATES:
            tk.Button(
                btns, text=STATE_LABELS[st], bg=STATE_COLORS[st], fg=STATE_FG[st],
                activebackground=STATE_COLORS[st], activeforeground=STATE_FG[st],
                font=("", 9, "bold"), bd=0, padx=6, pady=3,
                command=lambda s=st: app.home.save_state(s),
            ).pack(side="left", fill="x", expand=True, padx=2)

        foot = tk.Label(self, textvariable=app.home.auto_status, fg="#777777", bg=MINI_BG, font=("", 7), anchor="w")
        foot.pack(fill="x", padx=6, pady=(0, 3))

        for w in (bar, self.name_lbl, self.state_lbl, btns, foot):  # not the buttons themselves
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.minsize(330, 1)
        self.geometry(app.db.get_setting("mini_pos") or "+60+60")
        self.update_idletasks()
        self.lift()

    def set_record(self, rec, name: str, source: str = "local") -> None:
        if not name:
            self.state_lbl.configure(text="waiting for a name...", fg=MINI_DIM)
        elif rec is None:
            self.state_lbl.configure(text="NEW  -  no record yet", fg=MINI_NEW_FG)
        elif source == "global":
            st = rec["state"]
            self.state_lbl.configure(
                text=f"{STATE_LABELS[st]}  -  global table  -  {rec['updated_at'][:10]}",
                fg=MINI_STATE_FG[st],
            )
        else:
            st = rec["state"]
            self.state_lbl.configure(
                text=f"{STATE_LABELS[st]}  -  seen {rec['times_seen']}x  -  last {rec['updated_at'][:16]}",
                fg=MINI_STATE_FG[st],
            )

    def _drag_start(self, e: tk.Event) -> None:
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag_move(self, e: tk.Event) -> None:
        self._pos = f"+{e.x_root - self._dx}+{e.y_root - self._dy}"
        self.geometry(self._pos)

    def _drag_end(self, _e: tk.Event) -> None:
        if self._pos:
            self.app.db.set_setting("mini_pos", self._pos)


def main() -> None:
    fix_dpi()
    App().mainloop()
