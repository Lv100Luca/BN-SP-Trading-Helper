"""Tkinter UI: Home tab (capture -> OCR -> previous record -> save state) and Records tab."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

from . import __version__, capture, ocr
from .db import STATES, Database, name_key

STATE_LABELS = {"trading": "TRADING", "fighting": "FIGHTING", "afk": "AFK"}
STATE_COLORS = {"trading": "#2e7d32", "fighting": "#c62828", "afk": "#616161"}
STATE_PALE = {"trading": "#e8f5e9", "fighting": "#ffebee", "afk": "#eeeeee"}
PREVIEW_MAX = (520, 140)


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
        self.minsize(580, 600)
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass

        self.db = Database()
        self.engine: ocr.OcrEngine | None = None
        self._engine_lock = threading.Lock()
        self.region: capture.Region | None = self._load_region()
        self.pre_cfg = ocr.PreprocessConfig.from_dict(
            json.loads(self.db.get_setting("preprocess", "{}") or "{}")
        )

        self.status = tk.StringVar(value="Ready.")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.home = HomeTab(self.nb, self)
        self.records = RecordsTab(self.nb, self)
        self.nb.add(self.home, text="   Home   ")
        self.nb.add(self.records, text="   Records   ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken", padding=(6, 3)).pack(
            fill="x", side="bottom"
        )
        self.bind("<F5>", lambda _e: self.home.read_name())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.mini: MiniWindow | None = None
        if self.db.get_setting("mini_mode") == "1":
            self.after(200, self.enter_mini)

    # --------------------------------------------------------------- mini mode
    def enter_mini(self) -> None:
        if self.mini is not None:
            return
        self.withdraw()
        self.mini = MiniWindow(self)
        self.mini.set_record(self.home.current_record, self.home.name_var.get().strip())
        self.db.set_setting("mini_mode", "1")

    def exit_mini(self) -> None:
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self.db.set_setting("mini_mode", "0")
        self.deiconify()
        self.lift()

    def update_mini(self, rec, name: str) -> None:
        if self.mini is not None:
            self.mini.set_record(rec, name)

    # ------------------------------------------------------------------ helpers
    def set_status(self, text: str) -> None:
        self.status.set(text)

    def _on_tab_changed(self, _e: tk.Event) -> None:
        if self.nb.index("current") == 1:
            self.records.refresh()

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
        self._typing_until = 0.0        # auto-read leaves the name box alone until this time
        self._auto_job: str | None = None
        self._auto_run = False          # is the OCR currently in flight an auto-read?
        self._candidate = ""            # name seen once, waiting for a confirming read
        self._last_fp: np.ndarray | None = None
        self._last_result: tuple | None = None
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
                bg=STATE_COLORS[st], fg="white",
                activebackground=STATE_COLORS[st], activeforeground="white",
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
                self._results.put(("ok", engine.name, name, conf, lines))
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

    def _on_ocr_done(self, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine]) -> None:
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

    def _apply_auto_result(self, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine]) -> None:
        self._last_result = (engine_name, name, conf, lines)
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
        self.name_var.set(name)
        self.lookup()
        self.ocr_info.set(f"[{engine_name}] " + self._alts(lines))
        self.auto_status.set(f"{stamp}  {name} ({conf:.0%})")
        self.app.set_status(f"Auto-read '{name}' ({conf:.0%}).")

    # ----------------------------------------------------------------- records
    def lookup(self) -> None:
        name = self.name_var.get().strip()
        rec = self.app.db.get(name) if name else None
        self._show_prev(rec, name)

    def _show_prev(self, rec, name: str) -> None:
        self.current_record = rec
        self.app.update_mini(rec, name)
        if not name:
            self._paint_prev("No name read yet", "", bg=None, fg="black")
        elif rec is None:
            self._paint_prev(f"{name}: no previous record", "First time seeing this player.", bg="#fff8e1", fg="#795548")
        else:
            st = rec["state"]
            self._paint_prev(
                f"{rec['name']}: previously {STATE_LABELS[st]}",
                f"Seen {rec['times_seen']}x   |   first {rec['created_at']}   |   last {rec['updated_at']}",
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
        self._show_prev(rec, name)
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

        self.count_var = tk.StringVar()
        ttk.Label(self, textvariable=self.count_var, foreground="#666").pack(anchor="w", pady=(6, 2))

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        cols = ("name", "state", "seen", "updated")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in (
            ("name", "Name", 220, "w"),
            ("state", "State", 90, "center"),
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
        for st in STATES:
            tk.Button(
                btns, text=f"Set {STATE_LABELS[st]}", bg=STATE_COLORS[st], fg="white",
                activebackground=STATE_COLORS[st], activeforeground="white",
                command=lambda s=st: self.set_selected_state(s),
            ).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Delete", command=self.delete_selected).pack(side="right")
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="right", padx=(0, 6))

    def refresh(self) -> None:
        q = self.search_var.get().strip()
        f = self.filter_var.get()
        rows = self.app.db.all(q, None if f == "all" else f)
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert(
                "", "end", iid=r["name"],
                values=(r["name"], STATE_LABELS[r["state"]], r["times_seen"], r["updated_at"]),
                tags=(r["state"],),
            )
        keep = [r["name"] for r in rows if r["name"] in selected]
        if keep:
            self.tree.selection_set(keep)
        c = self.app.db.counts()
        total = sum(c.values())
        self.count_var.set(
            f"{len(rows)} shown / {total} total   "
            f"(trading {c.get('trading', 0)}, fighting {c.get('fighting', 0)}, afk {c.get('afk', 0)})"
        )

    def _selected_names(self) -> list[str]:
        return [self.tree.item(i, "values")[0] for i in self.tree.selection()]

    def load_selected(self, _e=None) -> None:
        names = self._selected_names()
        if not names:
            return
        self.app.home.name_var.set(names[0])
        self.app.home.lookup()
        self.app.nb.select(0)

    def set_selected_state(self, state: str) -> None:
        names = self._selected_names()
        for n in names:
            self.app.db.set_state(n, state)
        self.refresh()
        if names:
            self.app.set_status(f"Set {len(names)} record(s) to {STATE_LABELS[state]}.")

    def delete_selected(self, _e=None) -> None:
        names = self._selected_names()
        if not names:
            return
        if not messagebox.askyesno("Delete", f"Delete {len(names)} record(s)?"):
            return
        for n in names:
            self.app.db.delete(n)
        self.refresh()
        self.app.set_status(f"Deleted {len(names)} record(s).")


# ============================================================================ Mini
MINI_BG = "#202124"
MINI_FG = "#f5f5f5"
MINI_DIM = "#9e9e9e"
MINI_STATE_FG = {"trading": "#66bb6a", "fighting": "#ef5350", "afk": "#bdbdbd"}


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
                btns, text=STATE_LABELS[st], bg=STATE_COLORS[st], fg="white",
                activebackground=STATE_COLORS[st], activeforeground="white",
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

    def set_record(self, rec, name: str) -> None:
        if not name:
            self.state_lbl.configure(text="waiting for a name...", fg=MINI_DIM)
        elif rec is None:
            self.state_lbl.configure(text="NEW  -  no record yet", fg="#ffd54f")
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
