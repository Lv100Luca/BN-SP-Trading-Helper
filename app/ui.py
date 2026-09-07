"""Tkinter UI: Home tab (capture -> OCR -> previous record -> save state), Records tab (search,
rename, set state, export) and Settings (global table, contributing)."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageTk

from . import REPO_URL, __version__, capture, export, ocr, sync
from .db import STATES, Database, name_key

STATE_LABELS = {"trading": "TRADING", "fighting": "FIGHTING", "afk": "AFK", "fake": "FAKE"}
STATE_COLORS = {"trading": "#2e7d32", "fighting": "#c62828", "afk": "#616161", "fake": "#f9a825"}
STATE_FG = {"trading": "white", "fighting": "white", "afk": "white", "fake": "#212121"}  # button text
STATE_PALE = {"trading": "#e8f5e9", "fighting": "#ffebee", "afk": "#eeeeee", "fake": "#fff8e1"}
PREVIEW_MAX = (520, 140)
MIN_SIZE = (640, 520)   # smallest window; also the size on first start (the last size is restored later)
SAVE_COOLDOWN = 15   # seconds the double click guard ignores clicks for the same name (packaged app)


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


class HistoryBar(tk.Canvas):
    """State timeline as a strip of colored blocks, oldest left, newest right, with "oldest" /
    "newest" captions underneath. Hover a block for its time. Blocks are equal-width, so a run
    of the same state reads as one wide bar. With `slots` the strip always has room for that
    many blocks and fills from the right (used by the mini HUD for the last 10 encounters)."""
    BAR = 14
    HEIGHT = BAR + 13

    def __init__(self, master: tk.Misc, max_blocks: int = 60, slots: int | None = None,
                 caption_fg: str = "#888888", **kw) -> None:
        super().__init__(master, height=self.HEIGHT, highlightthickness=0, bd=0, **kw)
        self.max_blocks = slots or max_blocks
        self.slots = slots
        self.caption_fg = caption_fg
        self._rows: list = []
        self._tip: tk.Toplevel | None = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _e: self._hide_tip())

    def set(self, rows: list) -> None:
        self._rows = list(rows)[-self.max_blocks:]
        self._draw()

    def _layout(self) -> tuple[int, float, int]:
        """(number of slots, slot width, index of the first filled slot)."""
        n = len(self._rows)
        slots = self.slots or n
        w = self.winfo_width()
        return slots, (w / slots if slots else 0), slots - n

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if not self._rows or w < 10:
            return
        slots, step, first = self._layout()
        gap = 1 if step >= 4 else 0
        for i, r in enumerate(self._rows):
            x0 = round((first + i) * step)
            x1 = max(x0 + 1, round((first + i + 1) * step) - gap)
            self.create_rectangle(x0, 1, x1, self.BAR - 1, fill=STATE_COLORS[r["state"]], outline="")
        y = self.BAR + 6
        self.create_text(1, y, text="oldest", anchor="w", fill=self.caption_fg, font=("", 7))
        self.create_text(w - 1, y, text="newest >", anchor="e", fill=self.caption_fg, font=("", 7))

    def _hover(self, e: tk.Event) -> None:
        w = self.winfo_width()
        if not self._rows or w < 10 or e.y > self.BAR:
            self._hide_tip()
            return
        slots, step, first = self._layout()
        i = int(e.x / step) - first
        if not 0 <= i < len(self._rows):
            self._hide_tip()
            return
        r = self._rows[i]
        kind = {"edit": " (edited)", "import": " (imported)"}.get(r["kind"], "")
        self._show_tip(f"{STATE_LABELS[r['state']]}  {r['ts'][:16]}{kind}", e.x_root + 12, e.y_root + 12)

    def _show_tip(self, text: str, x: int, y: int) -> None:
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            self._tip_lbl = tk.Label(self._tip, bg="#ffffe0", relief="solid", bd=1, padx=4, pady=1, font=("", 8))
            self._tip_lbl.pack()
        self._tip_lbl.configure(text=text)
        self._tip.geometry(f"+{x}+{y}")

    def _hide_tip(self) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def history_summary(rows: list) -> str:
    """Share of each state in a timeline, most common first: 'trading 59%  |  fake 23%  |  afk 18%'."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    total = sum(counts.values()) or 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], STATES.index(kv[0])))
    return "  |  ".join(f"{st} {round(100 * n / total)}%" for st, n in ranked)


def newest(local, glob):
    """Of your own record and the shared table's entry, the one changed most recently (yours on a
    tie or when only one exists). Decides the headline state and colour of a panel."""
    if local is None or glob is None:
        return local if local is not None else glob
    return glob if glob["updated_at"] > local["updated_at"] else local


def notes_text(local, glob, you: str = "Your note", shared: str = "Shared note", both: str = "Note") -> str:
    """Both sources' notes, labelled, so neither is ever hidden; one line when they agree."""
    mine = local["notes"] if local is not None else ""
    theirs = glob["notes"] if glob is not None else ""
    if mine and theirs:
        return f"{both}: {mine}" if mine == theirs else f"{you}: {mine}\n{shared}: {theirs}"
    return f"{you}: {mine}" if mine else (f"{shared}: {theirs}" if theirs else "")


class HistoryPanel(tk.Frame):
    """Your own timeline and the shared table's, stacked and labelled ("You" / "Everyone"), so a
    block never needs a footnote to say whose sighting it is. A strip without rows is hidden."""

    def __init__(self, master: tk.Misc, slots: int | None = None, caption_fg: str = "#888888",
                 who: tuple[str, str] = ("You", "Everyone"), font=("", 8), fg: str = "#444444", **kw) -> None:
        super().__init__(master, **kw)
        self.who = who
        self.rows: list[tuple[tk.Label, HistoryBar]] = []
        for _ in who:
            lbl = tk.Label(self, text="", anchor="w", font=font)
            bar = HistoryBar(self, slots=slots, caption_fg=caption_fg)
            self.rows.append((lbl, bar))
        self.recolor(self.cget("bg"), fg)

    def set(self, local_rows: list, global_rows: list) -> None:
        for (lbl, bar), who, rows in zip(self.rows, self.who, (local_rows, global_rows)):
            lbl.pack_forget()
            bar.pack_forget()
            if not rows:
                continue
            bar.set(rows)
            n = len(rows)
            summary = f"  -  {history_summary(rows)}" if n > 1 else ""
            lbl.configure(text=f"{who}: {n} sighting{'s' if n != 1 else ''}{summary}")
            lbl.pack(fill="x", pady=(4, 0))
            bar.pack(fill="x")

    def recolor(self, bg: str, fg: str) -> None:
        self.configure(bg=bg)
        for lbl, bar in self.rows:
            lbl.configure(bg=bg, fg=fg)
            bar.configure(bg=bg)


def autowrap(label: tk.Misc, pad: int = 4) -> None:
    """Wrap a packed (fill=x) label at its own width, so long text never widens the window."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(60, e.width - pad)))


class NotesDialog(tk.Toplevel):
    """Modal editor for one record's note. One large button per preset (Settings -> Note presets)
    puts that preset's text into the box; typing stays possible. ask() returns the new text, or
    None when cancelled."""

    def __init__(self, parent: tk.Misc, name: str, initial: str, presets: list[dict], hint: str = "") -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Notes")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result: str | None = None
        self.var = tk.StringVar(value=initial)
        self.preset_btns: list[tk.Button] = []
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Notes for {name}:", font=("", 10, "bold")).pack(anchor="w")
        if hint:
            ttk.Label(body, text=hint, foreground="#666", justify="left", wraplength=440).pack(anchor="w", pady=(2, 0))
        if presets:
            grid = ttk.Frame(body)
            grid.pack(fill="x", pady=(8, 0))
            cols = min(3, len(presets))
            for i, p in enumerate(presets):
                btn = tk.Button(grid, text=p["name"], font=("", 11, "bold"), height=2, wraplength=140,
                                command=lambda t=p["text"]: self.var.set(t))
                btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
                self.preset_btns.append(btn)
            for c in range(cols):
                grid.columnconfigure(c, weight=1, uniform="preset")
        else:
            ttk.Label(body, text="Tip: Settings -> Note presets adds one-click buttons here.",
                      foreground="#666").pack(anchor="w", pady=(6, 0))
        entry = ttk.Entry(body, textvariable=self.var, width=60, font=("", 11))
        entry.pack(fill="x", pady=(10, 0))
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        entry.focus_set()
        entry.icursor("end")

    def _ok(self) -> None:
        self.result = self.var.get()
        self.destroy()

    def ask(self) -> str | None:
        self.grab_set()
        self.wait_window()
        return self.result


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
        self.minsize(*MIN_SIZE)
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
        if self.db.migration_report:
            r = self.db.migration_report
            self.status.set(
                f"Moved records to the shared database ({self.db.path}): {r['added']} added, "
                f"{r['updated']} updated from {r['source']}"
            )
        self._build_footer()   # packed first so a shrinking window squeezes the tabs, not the footer
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.home = HomeTab(self.nb, self)
        self.records = RecordsTab(self.nb, self)
        self.nb.add(self.home, text="   Home   ")
        self.nb.add(self.records, text="   Records   ")
        self.settings = SettingsTab(self.nb, self)
        self.nb.add(self.settings, text="   Settings   ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.bind("<F5>", lambda _e: self.home.read_name())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._restore_geometry()
        self.mini: MiniWindow | None = None
        if self.db.get_setting("mini_mode") == "1":
            self.after(200, self.enter_mini)
        self._sync_results: queue.Queue = queue.Queue()
        self._sync_busy = False
        self._push_busy = False
        self._inflight: set[int] = set()   # pending_uploads ids the push in progress carries
        self._last_sync = time.monotonic()
        self._refresh_global_label()
        if sync.table_url():
            self.after(1500, self.refresh_global)  # let the window come up first
            self.after(30_000, self._sync_tick)

    def _restore_geometry(self) -> None:
        """Last window size and position, or the minimum size (the content wraps to fit)."""
        saved = self.db.get_setting("win_geometry") or ""
        if re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)?", saved):
            self.geometry(saved)
            return
        self.geometry("%dx%d" % MIN_SIZE)

    def _build_footer(self) -> None:
        """Last action on the left, version on the right. Everything else lives in the Settings tab."""
        footer = ttk.Frame(self, relief="sunken", padding=(6, 3))
        footer.pack(fill="x", side="bottom")
        ttk.Label(footer, text=f"v{__version__}", foreground="#666").pack(side="right")
        ttk.Label(footer, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------ global table
    def _refresh_global_label(self) -> None:
        self.settings.refresh_global_label()

    def refresh_global(self) -> None:
        """Download the shared table in a worker thread; the result is applied on the UI thread."""
        url = sync.table_url()
        if not url or self._sync_busy:
            return
        self._sync_busy = True
        self.settings.set_refresh_enabled(False)
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
        if kind in ("push_ok", "push_error"):
            self._push_busy = False
            self._inflight = set()
            self.settings.set_upload_enabled(True)
            if kind == "push_error":
                msg, then_fetch, rejected = payload
                if rejected:
                    self.settings.key_rejected()
                self.set_status(f"Upload to the global table failed: {msg}")
            else:
                ids, report, then_fetch = payload
                self.db.clear_uploads(ids)
                extra = "".join(
                    f", {report[k]} {what}" for k, what in (("retracted", "taken back"), ("renamed", "renamed"),
                                                            ("noted", "note(s) changed"))
                    if report.get(k)
                )
                self.set_status(
                    f"Uploaded {len(ids)} change(s) to the global table: {report.get('added', 0)} new, "
                    f"{report.get('updated', 0)} changed, {report.get('unchanged', 0)} already known{extra}."
                )
            self.settings.refresh_pending_label()
            if then_fetch:
                self.refresh_global()
            elif not self._sync_results.empty():
                self.after(1, self._poll_sync)
            return
        self._sync_busy = False
        self.settings.set_refresh_enabled(True)
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

    # ------------------------------------------------------------ contributing
    def sync_interval(self) -> int:
        """Minutes between automatic uploads / downloads (1..15)."""
        try:
            return min(15, max(1, int(self.db.get_setting("sync_interval", "5") or 5)))
        except ValueError:
            return 5

    @staticmethod
    def save_cooldown() -> int:
        """Seconds the double click guard ignores clicks for the same name. Fixed in the packaged app;
        off when running from source (testing), unless TRADECHECK_SAVE_COOLDOWN says otherwise."""
        env = os.environ.get("TRADECHECK_SAVE_COOLDOWN")
        if env is not None:
            try:
                return max(0, int(env))
            except ValueError:
                pass
        return SAVE_COOLDOWN if getattr(sys, "frozen", False) else 0

    def sharing(self) -> bool:
        return self.db.get_setting("share_uploads") == "1" and bool(self.db.get_setting("contrib_key"))

    def record_saved(self, name: str, state: str, notes: str = "", kind: str = "seen",
                     new_name: str = "") -> tuple[int | None, str | None]:
        """Called after every change to a record; queues it for the global table when sharing is on.
        kind "seen" is an encounter (Home), "edit" a correction and "rename" a fix of the name
        (Records tab), "note" a changed note (the only kind that carries `notes`; the server ignores
        notes on the others). Returns the queue row's (id, ts), or (None, None) when nothing was queued."""
        if not self.sharing():
            return None, None
        upload_id, ts = self.db.queue_upload(name, state, notes, kind, new_name=new_name)
        self.settings.refresh_pending_label()
        return upload_id, ts

    def retract_upload(self, upload_id: int, name: str, state: str, ts: str) -> None:
        """Undo of a save that was queued for the global table: drop it from the queue while it is
        still there; once pushed (or in flight right now) ask the server to take it back instead."""
        if upload_id in self._inflight or not self.db.delete_upload(upload_id):
            self.db.queue_upload(name, state, "", "retract", ts=ts)
        self.settings.refresh_pending_label()

    def edit_notes(self, name: str, parent: tk.Misc) -> bool:
        """Dialog for the notes of your own record `name`. Contributors' notes go to the global table
        as a "note" row (an empty one clears the shared note). True when something changed."""
        rec = self.db.get(name)
        if rec is None:
            messagebox.showinfo("Notes", f"'{name}' has no record of yours yet. Save a state first.", parent=parent)
            return False
        glob = self.db.get_global(rec["name"])
        hint = (f"Shared note: {glob['notes']}" if glob is not None and glob["notes"]
                and glob["notes"] != rec["notes"] else "")
        new = NotesDialog(parent, rec["name"], rec["notes"], self.db.note_presets(), hint).ask()
        if new is None:
            return False
        new = " ".join(new.split())
        if new == rec["notes"]:
            return False
        self.db.set_notes(rec["name"], new)
        self.record_saved(rec["name"], rec["state"], new, kind="note")
        self.set_status(f"{rec['name']}: notes {'saved' if new else 'removed'}.")
        return True

    def run_bg(self, work, done) -> None:
        """Run `work()` in a thread and hand (result, exception) to `done` on the UI thread."""
        box: queue.Queue = queue.Queue()

        def runner() -> None:  # no tkinter or sqlite calls in here
            try:
                box.put((work(), None))
            except Exception as exc:  # noqa: BLE001
                box.put((None, exc))

        def poll() -> None:
            try:
                result, exc = box.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            done(result, exc)

        threading.Thread(target=runner, daemon=True).start()
        self.after(100, poll)

    def _sync_tick(self) -> None:
        self.after(30_000, self._sync_tick)
        if time.monotonic() - self._last_sync < self.sync_interval() * 60:
            return
        self._last_sync = time.monotonic()
        fetch = self.db.get_setting("auto_fetch") == "1"
        if self.sharing() and self.db.pending_upload_count():
            self.push_uploads(then_fetch=fetch)
        elif fetch:
            self.refresh_global()

    def push_uploads(self, then_fetch: bool = False) -> None:
        """Send the queued saves in a worker thread; the result is applied on the UI thread."""
        url, key = sync.table_url(), self.db.get_setting("contrib_key", "") or ""
        rows = self.db.pending_uploads()
        if not url or not key or self._push_busy:
            return
        if not rows:
            if then_fetch:
                self.refresh_global()
            return
        self._push_busy = True
        self.settings.set_upload_enabled(False)
        ids = [r["id"] for r in rows]
        self._inflight = set(ids)
        records = [_upload_payload(r) for r in rows]

        def work() -> None:  # no tkinter or sqlite calls in here
            try:
                self._sync_results.put(("push_ok", (ids, sync.push_records(url, key, records), then_fetch)))
            except sync.SyncError as exc:
                self._sync_results.put(("push_error", (str(exc), then_fetch, isinstance(exc, sync.AuthError))))
            except Exception as exc:  # noqa: BLE001
                self._sync_results.put(("push_error", (f"{type(exc).__name__}: {exc}", then_fetch, False)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_sync)

    def _flush_on_exit(self) -> None:
        """Best effort: push what is queued before the window goes away (short timeout)."""
        rows = self.db.pending_uploads()
        if not rows or not self.sharing() or self._push_busy:   # a push in flight would double-count
            return
        try:
            sync.push_records(sync.table_url(), self.db.get_setting("contrib_key", "") or "",
                              [_upload_payload(r) for r in rows], timeout=4)
            self.db.clear_uploads(r["id"] for r in rows)
        except Exception:  # noqa: BLE001  - stays queued for the next run
            pass

    # --------------------------------------------------------------- mini mode
    def enter_mini(self) -> None:
        if self.mini is not None:
            return
        self.withdraw()
        self.mini = MiniWindow(self)
        if self.home.name_var.get().strip():
            self.home.lookup(quiet=True)  # pushes record and history into the mini window
        else:
            self.mini.set_record("", None, None, [], [])
        self.db.set_setting("mini_mode", "1")

    def exit_mini(self) -> None:
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self.db.set_setting("mini_mode", "0")
        self.deiconify()
        self.lift()

    def update_mini(self, name: str, local, glob, local_rows: list, global_rows: list) -> None:
        if self.mini is not None:
            self.mini.set_record(name, local, glob, local_rows, global_rows)

    # ------------------------------------------------------------------ helpers
    def set_status(self, text: str) -> None:
        self.status.set(text)

    def _on_tab_changed(self, _e: tk.Event) -> None:
        if self.nb.nametowidget(self.nb.select()) is self.records:
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
        if self.winfo_viewable():
            self.db.set_setting("win_geometry", self.geometry())
        self._flush_on_exit()
        self.db.close()
        self.destroy()


def _upload_payload(r) -> dict:
    return {"name": r["name"], "state": r["state"], "notes": r["notes"], "ts": r["ts"], "kind": r["kind"],
            "new_name": r["new_name"]}


# ============================================================================ Home
class HomeTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._preview_img: ImageTk.PhotoImage | None = None
        self._busy = False
        self._results: queue.Queue = queue.Queue()  # worker thread -> UI thread hand-off
        self._typing_until = 0.0        # auto-read leaves the name box alone until this time
        self._auto_job: str | None = None
        self._auto_run = False          # is the OCR currently in flight an auto-read?
        self._candidate = ""            # name seen once, waiting for a confirming read
        self._last_fp: np.ndarray | None = None
        self._last_result: tuple | None = None
        self._last_save: dict | None = None  # what Undo takes back (see save_state)
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
        """Home is the play surface: read, check, save. Capture set-up lives in Settings
        (build_capture_controls); only a first-run "Select region..." shows here while none is set."""
        self.region_var = tk.StringVar()
        self.auto_status = tk.StringVar(value="auto-read off")
        self.ocr_info = tk.StringVar(value="")
        self.preview: ttk.Label | None = None

        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Button(row, text="Mini mode", command=self.app.enter_mini).pack(side="right")
        self.region_btn = ttk.Button(row, text="Select region...", command=self.select_region)

        self.read_btn = tk.Button(
            self, text="READ NAME  (F5)", font=("", 14, "bold"), height=2,
            bg="#1565c0", fg="white", activebackground="#0d47a1", activeforeground="white",
            command=self.read_name,
        )
        self.read_btn.pack(fill="x", pady=(10, 2))
        ttk.Label(self, textvariable=self.auto_status, foreground="#666", anchor="e").pack(fill="x", pady=(0, 8))

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(10, 4))
        ttk.Label(row, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.name_var, font=("", 12))
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.lookup())
        entry.bind("<Key>", lambda _e: self.note_typing())
        self.name_entry = entry
        ttk.Button(row, text="Lookup", command=self.lookup).pack(side="left")

        # Previous-record panel: headline, one line per source (you / everyone), both notes, both
        # timelines. Nothing one source knows is hidden behind the other.
        self.prev_frame = tk.Frame(self, bd=2, relief="ridge", padx=10, pady=10)
        self.prev_frame.pack(fill="x", pady=6)
        self.prev_title = tk.Label(self.prev_frame, text="No name read yet", font=("", 13, "bold"), anchor="w")
        self.prev_title.pack(fill="x")
        self.prev_detail = tk.Label(self.prev_frame, text="", justify="left", anchor="w")
        self.prev_detail.pack(fill="x")
        autowrap(self.prev_detail)
        self.prev_notes = tk.Label(self.prev_frame, text="", justify="left", anchor="w")
        autowrap(self.prev_notes)
        self.prev_history = HistoryPanel(self.prev_frame)
        self.prev_history.pack(fill="x", pady=(2, 0))

        ttk.Label(self, text="Record the current state (added to this player's history):").pack(anchor="w", pady=(8, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        for st in STATES:
            tk.Button(
                row, text=STATE_LABELS[st], font=("", 12, "bold"), height=2,
                bg=STATE_COLORS[st], fg=STATE_FG[st],
                activebackground=STATE_COLORS[st], activeforeground=STATE_FG[st],
                command=lambda s=st: self.save_state(s),
            ).pack(side="left", fill="x", expand=True, padx=3)
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(6, 0))
        self.cooldown_var = tk.StringVar()
        # fixed button width and a wrapping label, so the countdown never changes the window width
        self.undo_btn = ttk.Button(row, text="Undo last save", command=self.undo_save, state="disabled", width=30)
        self.undo_btn.pack(side="right")
        cd = ttk.Label(row, textvariable=self.cooldown_var, foreground="#666", justify="left")
        cd.pack(side="left", fill="x", expand=True)
        autowrap(cd)
        ttk.Button(row, text="Notes...", command=self.edit_notes).pack(side="right", padx=(0, 6))

    def note_typing(self) -> None:
        """Auto-read leaves the name box alone for a moment while a human types in it."""
        self._typing_until = time.time() + 3.0

    def edit_notes(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showinfo("Notes", "Read or type a name first.")
            return
        if self.app.edit_notes(name, self):
            self.lookup(quiet=True)

    def build_capture_controls(self, parent: tk.Misc) -> None:
        """Region, test capture, auto-read and the capture preview, placed in the Settings tab."""
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.region_var).pack(side="left")
        ttk.Button(row, text="Test capture", command=self.test_capture).pack(side="right")
        ttk.Button(row, text="Select region...", command=self.select_region).pack(side="right", padx=(0, 6))
        hint = ttk.Label(parent, text="Drag a box over where the enemy name appears; leave a little space around it "
                                      "and stop above the level line. The preview shows exactly what is captured.",
                         foreground="#666", justify="left")
        hint.pack(fill="x", pady=(2, 8))
        autowrap(hint)

        auto = ttk.Frame(parent)
        auto.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(auto, text="Auto-read every", variable=self._auto_var, command=self._toggle_auto).pack(side="left")
        ttk.Spinbox(auto, from_=0.5, to=5.0, increment=0.5, width=4, textvariable=self._interval_var,
                    command=self._toggle_auto).pack(side="left", padx=(4, 2))
        ttk.Label(auto, text="s   (skips unchanged frames; a new name needs two consistent reads)",
                  foreground="#666").pack(side="left")

        self.preview = ttk.Label(parent, anchor="center", relief="groove", text="(capture preview)")
        self.preview.pack(fill="x", ipady=6)
        info = ttk.Label(parent, textvariable=self.ocr_info, foreground="#666", justify="left")
        info.pack(fill="x", pady=(2, 0))
        autowrap(info)

    # ------------------------------------------------------------------ region
    def _refresh_region_label(self) -> None:
        r = self.app.region
        if r:
            self.region_var.set(f"Region: x={r[0]} y={r[1]}  {r[2]} x {r[3]} px")
            self.region_btn.pack_forget()
        else:
            self.region_var.set("Region: not set  ->  click 'Select region...'")
            self.region_btn.pack(side="right", padx=(0, 6))

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
        if self.preview is None:
            return
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

    def _on_ocr_done(self, engine_name: str, name: str, conf: float, lines: list[ocr.OcrLine], _img) -> None:
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
        self.name_var.set(name)
        self.lookup()
        self.ocr_info.set(f"[{engine_name}] " + self._alts(lines))
        self.auto_status.set(f"{stamp}  {name} ({conf:.0%})")
        self.app.set_status(f"Auto-read '{name}' ({conf:.0%}).")

    # ----------------------------------------------------------------- records
    def set_name(self, name: str) -> None:
        """Put a name in the box that did not come from the last capture (Records tab)."""
        self.name_var.set(name)
        self.lookup()

    def lookup(self, quiet: bool = False) -> None:
        """Show your own record and the shared table's entry for the name in the box, side by side.
        `quiet` re-runs the lookup after a global table refresh and does nothing when the box is empty."""
        name = self.name_var.get().strip()
        if quiet and not name:
            return
        if not name:
            self._show_prev("", None, None)
            return
        local, glob = self.app.db.find_both(name)
        self._show_prev(name, local, glob)

    def _show_prev(self, name: str, local, glob) -> None:
        """Paint the panel from both sources: `local` is your own record, `glob` the shared table's
        entry (either may be None). Headline and colour follow whichever changed last."""
        lrows = self.app.db.history(local["name"]) if local is not None else []
        grows = self.app.db.global_history(glob["name"]) if glob is not None else []
        self.app.update_mini(name, local, glob, lrows, grows)
        self.prev_history.set(lrows, grows)
        if not name:
            self._paint_prev("No name read yet", "", bg=None, fg="black")
            return
        if local is None and glob is None:
            self._paint_prev(f"{name}: no previous record", "First time seeing this player.", bg="#fff8e1", fg="#795548")
            return
        head = newest(local, glob)
        st = head["state"]
        title = f"{head['name']}: {STATE_LABELS[st]}"
        if local is not None and glob is not None and local["state"] != glob["state"]:
            title += "  (yours)" if head is local else "  (shared, newer)"
        if local is not None:
            lines = [f"You: {STATE_LABELS[local['state']]}  -  seen {local['times_seen']}x  -  "
                     f"first {local['created_at'][:16]}  -  last {local['updated_at'][:16]}"]
        else:
            lines = ["You: no record of your own yet"]
        if sync.table_url():
            if glob is not None:
                last = grows[-1]["ts"] if grows else glob["updated_at"]
                lines.append(f"Everyone: {STATE_LABELS[glob['state']]}  -  {glob['times_seen']} encounter(s)"
                             f"  -  last {last[:16]}")
            else:
                lines.append("Everyone: not in the global table")
        self._paint_prev(title, "\n".join(lines), bg=STATE_PALE[st], fg=STATE_COLORS[st], notes=notes_text(local, glob))

    def _paint_prev(self, title: str, detail: str, bg: str | None, fg: str, notes: str = "") -> None:
        bg = bg or self.app.cget("bg")
        for w in (self.prev_frame, self.prev_title, self.prev_detail, self.prev_notes):
            w.configure(bg=bg)
        self.prev_history.recolor(bg, fg)
        self.prev_title.configure(text=title, fg=fg)
        self.prev_detail.configure(text=detail, fg=fg)
        self.prev_notes.configure(text=notes, fg=fg)
        if notes:
            self.prev_notes.pack(fill="x", after=self.prev_detail)
        else:
            self.prev_notes.pack_forget()

    def save_state(self, state: str) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showinfo("No name", "Read or type a name first.")
            return
        ls = self._last_save
        if ls is not None and name_key(ls["name"]) == name_key(name) and self._cooldown_left() > 0:
            self.app.set_status(f"{ls['name']} was saved as {STATE_LABELS[ls['state']]} "
                                f"{self.app.save_cooldown() - self._cooldown_left():.0f} s ago; a new encounter "
                                f"counts in {self._cooldown_left():.0f} s. Misclick? Undo it.")
            return
        prev = self.app.db.get(name)
        glob = self.app.db.get_global(name)
        # a first record of your own starts with the shared note, so saving never hides it
        rec = self.app.db.upsert(name, state, glob["notes"] if prev is None and glob is not None else None)
        upload_id, ts = self.app.record_saved(rec["name"], state)
        self._last_save = {
            "name": rec["name"], "state": state, "prev": dict(prev) if prev is not None else None,
            "sighting_id": self.app.db.last_sighting_id(rec["id"]), "upload_id": upload_id, "ts": ts,
            "at": time.monotonic(),
        }
        self._refresh_undo()
        self._tick_cooldown()
        self._show_prev(name, rec, glob)
        if prev is not None and prev["state"] != state:
            self.app.set_status(
                f"{rec['name']}: {STATE_LABELS[prev['state']]} -> {STATE_LABELS[state]} (overwritten)"
            )
        else:
            self.app.set_status(f"{rec['name']}: saved as {STATE_LABELS[state]}")

    def _cooldown_left(self) -> float:
        ls = self._last_save
        return max(0.0, self.app.save_cooldown() - (time.monotonic() - ls["at"])) if ls else 0.0

    def _tick_cooldown(self) -> None:
        left = self._cooldown_left()
        if left <= 0:
            self.cooldown_var.set("")
            if self.app.mini is not None:
                self.app.mini.set_cooldown(0)
            return
        self.cooldown_var.set(f"Double click guard: same name ignored for {left:.0f} s. Misclick? Undo.")
        if self.app.mini is not None:
            self.app.mini.set_cooldown(left)
        self.after(250, self._tick_cooldown)

    def undo_save(self) -> None:
        """Take back the last state saved here or on the mini HUD: the sighting and encounter count
        locally and, for contributors, the queued or already pushed upload."""
        ls = self._last_save
        if ls is None:
            return
        self._last_save = None
        self.app.db.undo_save(ls["name"], ls["sighting_id"], ls["prev"])
        if ls["upload_id"] is not None:
            self.app.retract_upload(ls["upload_id"], ls["name"], ls["state"], ls["ts"])
        self._refresh_undo()
        self._tick_cooldown()
        self.lookup(quiet=True)
        back = f"back to {STATE_LABELS[ls['prev']['state']]}" if ls["prev"] else "record removed"
        self.app.set_status(f"Undone: {ls['name']} {STATE_LABELS[ls['state']]} ({back}).")

    def forget_undo(self, name: str) -> None:
        """The record was renamed or deleted elsewhere; the last save can no longer be undone."""
        if self._last_save is not None and name_key(self._last_save["name"]) == name_key(name):
            self._last_save = None
            self._refresh_undo()

    def _refresh_undo(self) -> None:
        ls = self._last_save
        text = f"Undo: {ls['name']} {STATE_LABELS[ls['state']]}" if ls else "Undo last save"
        self.undo_btn.configure(text=text, state="normal" if ls else "disabled")
        if self.app.mini is not None:
            self.app.mini.set_undo(ls is not None)


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
        cols = ("name", "state", "notes", "source", "seen", "updated")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in (
            ("name", "Name", 220, "w"),
            ("state", "State", 90, "center"),
            ("notes", "Notes", 200, "w"),
            ("source", "Source", 60, "center"),
            ("seen", "Seen", 60, "center"),
            ("updated", "Last updated", 150, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("name", "notes"))
        for st in STATES:
            self.tree.tag_configure(st, foreground=STATE_COLORS[st])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.load_selected)
        self.tree.bind("<Delete>", self.delete_selected)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_history())

        hist = ttk.Frame(self)
        hist.pack(fill="x", pady=(6, 0))
        self.history_lbl = ttk.Label(hist, text="History: select a record", foreground="#666")
        self.history_lbl.pack(anchor="w")
        self.history_panel = HistoryPanel(hist, bg=self.app.cget("bg"))
        self.history_panel.pack(fill="x")

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Load into Home", command=self.load_selected).pack(side="left")
        ttk.Button(btns, text="Rename...", command=self.rename_selected).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Notes...", command=self.notes_selected).pack(side="left", padx=(6, 0))
        ttk.Label(btns, text="Set:").pack(side="left", padx=(10, 0))
        for st in STATES:
            tk.Button(
                btns, text=STATE_LABELS[st], bg=STATE_COLORS[st], fg=STATE_FG[st],
                activebackground=STATE_COLORS[st], activeforeground=STATE_FG[st],
                command=lambda s=st: self.set_selected_state(s),
            ).pack(side="left", padx=(4, 0))
        tools = ttk.Frame(self)
        tools.pack(fill="x", pady=(6, 0))
        ttk.Button(tools, text="Export...", command=self.export_rows).pack(side="left")
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
            seen = r["times_seen"] or "-"
            self.tree.insert(
                "", "end", iid=self._iid(r["source"], r["name"]),
                values=(r["name"], STATE_LABELS[r["state"]], r["notes"], r["source"], seen, r["updated_at"]),
                tags=(r["state"],),
            )
        keep = [i for i in (self._iid(r["source"], r["name"]) for r in rows) if i in selected]
        if keep:
            self.tree.selection_set(keep)
        self._show_history()
        source = self.source_var.get()
        parts = []
        if source in ("both", "local"):
            c = self.app.db.counts("local")
            parts.append(f"{sum(c.values())} own (trading {c.get('trading', 0)}, fighting {c.get('fighting', 0)}, "
                         f"afk {c.get('afk', 0)}, fake {c.get('fake', 0)})")
        if source in ("both", "global"):
            parts.append(f"{sum(self.app.db.counts('global').values())} global")
        self.count_var.set(f"{len(rows)} shown / " + ", ".join(parts))

    def _show_history(self) -> None:
        """Both timelines of the selected name, yours and the shared table's, whichever row was picked."""
        picked = self._selected()
        if len(picked) != 1:
            self.history_lbl.configure(text="History: select one record" if picked else "History: select a record")
            self.history_panel.set([], [])
            return
        name = picked[0][0]
        local, glob = self.app.db.find_both(name)
        lrows = self.app.db.history(local["name"]) if local is not None else []
        grows = self.app.db.global_history(glob["name"]) if glob is not None else []
        self.history_panel.set(lrows, grows)
        self.history_lbl.configure(text=f"History of {name}" + ("" if lrows or grows else ": none"))

    def _current_rows(self):
        q = self.search_var.get().strip()
        f = self.filter_var.get()
        rows = self.app.db.all(q, None if f == "all" else f, self.source_var.get())
        return rows, (q == "" and f == "all")

    def export_rows(self) -> None:
        """Save your own records (the current search/filter applied, never the global table's copy)
        as JSON (or CSV)."""
        q, f = self.search_var.get().strip(), self.filter_var.get()
        rows = self.app.db.all(q, None if f == "all" else f, "local")
        is_everything = q == "" and f == "all"
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
        scope = "all your records" if is_everything else "your records currently matching"
        self.app.set_status(f"Exported {n} ({scope}) to {path}")

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

    def notes_selected(self) -> None:
        picked = self._selected()
        if len(picked) != 1:
            messagebox.showinfo("Notes", "Select exactly one record.", parent=self)
            return
        if self.app.edit_notes(picked[0][0], self):
            self.refresh()
            self.app.home.lookup(quiet=True)

    def rename_selected(self) -> None:
        """Fix a name OCR got wrong. If the new name already has a record the two are merged after
        asking. Contributors' sightings follow the name in the global table too."""
        picked = self._selected()
        if len(picked) != 1 or picked[0][1] != "local":
            messagebox.showinfo("Rename", "Select exactly one of your own records.", parent=self)
            return
        src = self.app.db.get(picked[0][0], loose=False)
        if src is None:
            return
        new = simpledialog.askstring("Rename record", f"Correct name for '{src['name']}':",
                                     initialvalue=src["name"], parent=self)
        new = " ".join((new or "").split())
        if not new or new == src["name"]:
            return
        dst = self.app.db.get(new)
        if dst is not None and dst["id"] != src["id"] and not messagebox.askyesno(
            "Rename record",
            f"'{dst['name']}' already has a record ({STATE_LABELS[dst['state']]}, seen {dst['times_seen']}x).\n\n"
            f"Merge '{src['name']}' ({STATE_LABELS[src['state']]}, seen {src['times_seen']}x) into it?",
            parent=self,
        ):
            return
        try:
            result = self.app.db.rename_record(src["name"], new)
        except (ValueError, LookupError) as exc:
            messagebox.showerror("Rename record", str(exc), parent=self)
            return
        if result["action"] != "unchanged":
            self.app.record_saved(src["name"], src["state"], kind="rename", new_name=new)
        self.app.home.forget_undo(src["name"])
        self.refresh()
        if name_key(self.app.home.name_var.get()) == name_key(src["name"]):
            self.app.home.set_name(new)
        self.app.set_status(f"{src['name']} {result['action']} -> {new}")

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
            self.app.record_saved(name, state, kind="edit")
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
            self.app.home.forget_undo(n)
        self.refresh()
        note = f" ({skipped} global table entries skipped)" if skipped else ""
        self.app.set_status(f"Deleted {len(names)} record(s){note}.")


# ======================================================================== Settings
class SettingsTab(ttk.Frame):
    """Capture set-up, global table, contributing, note presets, About. Content sits in a scrollable
    canvas so a small window still reaches everything."""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self.global_var: tk.StringVar | None = None
        self.global_btn: ttk.Button | None = None
        canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=app.cget("bg"))
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(canvas, padding=10)
        win = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def wheel(e: tk.Event) -> None:
            if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self._build()

    def _build(self) -> None:
        cap = ttk.LabelFrame(self.body, text="Capture", padding=10)
        cap.pack(fill="x")
        self.app.home.build_capture_controls(cap)

        if sync.table_url():
            box = ttk.LabelFrame(self.body, text="Global table", padding=10)
            box.pack(fill="x", pady=(10, 0))
            row = ttk.Frame(box)
            row.pack(fill="x")
            self.global_var = tk.StringVar()
            ttk.Label(row, textvariable=self.global_var).pack(side="left")
            self.global_btn = ttk.Button(row, text="Refresh now", command=self.app.refresh_global)
            self.global_btn.pack(side="right")
            ttk.Label(box, text=f"Server: {sync.table_url()}   |   downloaded on start-up and on Refresh",
                      foreground="#666").pack(anchor="w", pady=(2, 0))
            self.refresh_global_label()
            self._build_sync(box)
            self._build_contribute()
        self._build_presets()

        about = ttk.LabelFrame(self.body, text="About", padding=10)
        about.pack(fill="x", pady=(10, 0))
        ttk.Label(about, text=f"Trade Check v{__version__}").pack(anchor="w")
        link = ttk.Label(about, text=REPO_URL, foreground="#1565c0", cursor="hand2", font=("", 9, "underline"))
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(REPO_URL))
        ttk.Label(about, text=f"Database: {self.app.db.path}", foreground="#666").pack(anchor="w", pady=(6, 0))

    def _build_sync(self, box: ttk.LabelFrame) -> None:
        db = self.app.db
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(10, 0))
        self.auto_fetch_var = tk.BooleanVar(value=db.get_setting("auto_fetch", "0") == "1")
        ttk.Checkbutton(row, text="Auto-download the table", variable=self.auto_fetch_var,
                        command=lambda: db.set_setting("auto_fetch", "1" if self.auto_fetch_var.get() else "0")
                        ).pack(side="left")
        ttk.Label(row, text="Sync interval:").pack(side="left", padx=(16, 4))
        self.interval_var = tk.IntVar(value=self.app.sync_interval())
        spin = ttk.Spinbox(row, from_=1, to=15, increment=1, width=4, textvariable=self.interval_var,
                           command=self._set_interval)
        spin.pack(side="left")
        spin.bind("<FocusOut>", lambda _e: self._set_interval())
        spin.bind("<Return>", lambda _e: self._set_interval())
        ttk.Label(row, text="min  (downloads and uploads)").pack(side="left", padx=(4, 0))

    def _build_contribute(self) -> None:
        db = self.app.db
        box = ttk.LabelFrame(self.body, text="Contribute to the global table", padding=10)
        box.pack(fill="x", pady=(10, 0))
        hint = ttk.Label(box, text="With a contributor key from the table admin your saves are uploaded and "
                                   "merged into the shared table on the sync interval (and when the app closes).",
                         foreground="#666", justify="left")
        hint.pack(fill="x")
        autowrap(hint)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Contributor key:").pack(side="left")
        self.key_var = tk.StringVar(value=db.get_setting("contrib_key", "") or "")
        self.key_entry = ttk.Entry(row, textvariable=self.key_var, show="*", width=52)
        self.key_entry.pack(side="left", padx=(6, 0))
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="show", variable=self.show_key_var,
                        command=lambda: self.key_entry.configure(show="" if self.show_key_var.get() else "*")
                        ).pack(side="left", padx=(6, 0))
        self.key_btn = ttk.Button(row, text="Save key", command=self._save_key)
        self.key_btn.pack(side="left", padx=(6, 0))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.share_var = tk.BooleanVar(value=db.get_setting("share_uploads", "0") == "1")
        self.share_chk = ttk.Checkbutton(row, text="Upload my saves", variable=self.share_var,
                                         command=self._toggle_share)
        self.share_chk.pack(side="left")
        self.pending_var = tk.StringVar()
        self.pending_lbl = ttk.Label(row, textvariable=self.pending_var, foreground="#666")
        self.pending_lbl.pack(side="left", padx=(16, 0))
        self.upload_btn = ttk.Button(row, text="Upload now", command=self.app.push_uploads)
        self.upload_btn.pack(side="right")
        self.discard_btn = ttk.Button(row, text="Discard queued", command=self._discard_queue)
        self.discard_btn.pack(side="right", padx=(0, 6))
        self._rejected = False   # the server answered 401 to an upload this session
        self.refresh_pending_label()

    def _build_presets(self) -> None:
        box = ttk.LabelFrame(self.body, text="Note presets", padding=10)
        box.pack(fill="x", pady=(10, 0))
        hint = ttk.Label(box, text="Each preset is a large button in the Notes dialog; one click puts its text into "
                                   "the note (you can still type). Select a row to edit it.",
                         foreground="#666", justify="left")
        hint.pack(fill="x")
        autowrap(hint)
        self.preset_tree = ttk.Treeview(box, columns=("name", "text"), show="headings", height=4, selectmode="browse")
        self.preset_tree.heading("name", text="Button")
        self.preset_tree.heading("text", text="Note text")
        self.preset_tree.column("name", width=140, stretch=False)
        self.preset_tree.column("text", width=320, stretch=True)
        self.preset_tree.pack(fill="x", pady=(6, 0))
        self.preset_tree.bind("<<TreeviewSelect>>", lambda _e: self._preset_pick())
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        self.preset_name = tk.StringVar()
        self.preset_text = tk.StringVar()
        ttk.Label(row, text="Button:").pack(side="left")
        ttk.Entry(row, textvariable=self.preset_name, width=16).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Text:").pack(side="left")
        ttk.Entry(row, textvariable=self.preset_text).pack(side="left", fill="x", expand=True, padx=(4, 10))
        ttk.Button(row, text="Remove", command=self._preset_remove).pack(side="right")
        ttk.Button(row, text="Add / Update", command=self._preset_save).pack(side="right", padx=(0, 6))
        self._refresh_presets()

    def _refresh_presets(self) -> None:
        self.preset_tree.delete(*self.preset_tree.get_children())
        for i, p in enumerate(self.app.db.note_presets()):
            self.preset_tree.insert("", "end", iid=str(i), values=(p["name"], p["text"]))

    def _preset_pick(self) -> None:
        sel = self.preset_tree.selection()
        if sel:
            name, text = self.preset_tree.item(sel[0], "values")
            self.preset_name.set(name)
            self.preset_text.set(text)

    def _preset_save(self) -> None:
        """Add / Update: the selected row is replaced (so a button can be renamed); with nothing
        selected, a preset of the same name is updated, otherwise a new one is appended."""
        name = " ".join(self.preset_name.get().split())
        text = " ".join(self.preset_text.get().split())
        if not name or not text:
            messagebox.showinfo("Note presets", "A preset needs a button name and a note text.", parent=self)
            return
        presets = self.app.db.note_presets()
        sel = self.preset_tree.selection()
        idx = int(sel[0]) if sel else next((i for i, p in enumerate(presets) if p["name"].lower() == name.lower()), None)
        if idx is None:
            presets.append({"name": name, "text": text})
        else:
            presets[idx] = {"name": name, "text": text}
        self.app.db.set_note_presets(presets)
        self._refresh_presets()
        self.preset_name.set("")
        self.preset_text.set("")
        self.app.set_status(f"Note preset '{name}' saved.")

    def _preset_remove(self) -> None:
        sel = self.preset_tree.selection()
        if not sel:
            return
        presets = self.app.db.note_presets()
        del presets[int(sel[0])]
        self.app.db.set_note_presets(presets)
        self._refresh_presets()
        self.preset_name.set("")
        self.preset_text.set("")

    def _set_interval(self) -> None:
        try:
            minutes = min(15, max(1, int(self.interval_var.get())))
        except (tk.TclError, ValueError):
            minutes = 5
        self.interval_var.set(minutes)
        self.app.db.set_setting("sync_interval", str(minutes))

    def _save_key(self) -> None:
        """The key is checked with the server first (GET /v1/keys/me) and only saved when it is known
        there, so 'Upload my saves' can never be on with a key that does not work."""
        key = "".join(self.key_var.get().split())
        if not key:
            self.app.db.set_setting("contrib_key", "")
            self.app.db.set_setting("contrib_label", "")
            self.app.db.set_setting("share_uploads", "0")
            self.share_var.set(False)
            self.app.set_status("Contributor key removed.")
            self.refresh_pending_label()
            return
        if not re.fullmatch(r"tck_[0-9a-f]{48}", key):
            messagebox.showerror("Contributor key", "That is not a contributor key. It looks like "
                                 "tck_ followed by 48 hex characters and is printed once by "
                                 "tools/manage_keys.py create.", parent=self)
            return
        self.key_var.set(key)
        url = sync.table_url()
        self.key_btn.configure(state="disabled", text="Checking...")
        self.app.set_status("Checking the contributor key with the server...")

        def done(info, exc) -> None:
            self.key_btn.configure(state="normal", text="Save key")
            if isinstance(exc, sync.AuthError):
                messagebox.showerror("Contributor key", "The server does not know this key. Check it for "
                                     "typos or ask the table admin for a new one.", parent=self)
                self.app.set_status("Contributor key rejected by the server; not saved.")
                return
            if exc is not None:
                messagebox.showerror("Contributor key", f"Could not check the key: {exc}", parent=self)
                self.app.set_status("Contributor key not checked; not saved.")
                return
            label = str(info.get("label") or "") if isinstance(info, dict) else ""
            self.app.db.set_setting("contrib_key", key)
            self.app.db.set_setting("contrib_label", label)
            self._rejected = False
            who = f" for {label}" if label else ""
            self.app.set_status(f"Contributor key{who} accepted. Tick 'Upload my saves' to start sharing.")
            self.refresh_pending_label()

        self.app.run_bg(lambda: sync.check_key(url, key), done)

    def _discard_queue(self) -> None:
        n = self.app.db.pending_upload_count()
        if not n:
            return
        if not messagebox.askyesno("Discard queued uploads",
                                   f"Throw away {n} queued change(s)? They will never reach the global table "
                                   "(undo and rename fixes included). Your own records are not affected.",
                                   parent=self):
            return
        self.app.db.clear_all_uploads()
        self.refresh_pending_label()
        self.app.set_status(f"Discarded {n} queued upload(s).")

    def key_rejected(self) -> None:
        """An upload came back 401: the key was revoked. Stop sharing until a working key is saved."""
        self.app.db.set_setting("share_uploads", "0")
        self.share_var.set(False)
        self._rejected = True
        self.refresh_pending_label()

    def _toggle_share(self) -> None:
        on = self.share_var.get()
        if on and not (self.app.db.get_setting("contrib_key") or ""):
            self.share_var.set(False)
            on = False
            self.app.set_status("Enter and save a contributor key first.")
        self.app.db.set_setting("share_uploads", "1" if on else "0")
        self.refresh_pending_label()

    def refresh_pending_label(self) -> None:
        if not hasattr(self, "pending_var"):
            return
        n = self.app.db.pending_upload_count()
        key = self.app.db.get_setting("contrib_key") or ""
        label = self.app.db.get_setting("contrib_label") or ""
        who = f"Key of {label}.  " if label else ""
        queued = f"{n} change(s) queued" if n else "nothing queued"
        color = "#666"
        if not key:
            text = "No key saved."
        elif self._rejected:
            text, color = f"The server rejected your key; uploads are off ({queued}). Save a working key.", "#c62828"
        elif not self.share_var.get():
            text = f"{who}Uploads off; {queued}."
        else:
            text = f"{who}{queued.capitalize()}" + (" for the next upload." if n else ".")
        self.pending_var.set(text)
        self.pending_lbl.configure(foreground=color)
        self.share_chk.configure(state="normal" if key else "disabled")
        self.upload_btn.configure(state="normal" if (n and self.app.sharing()) else "disabled")
        self.discard_btn.configure(state="normal" if n else "disabled")

    def set_upload_enabled(self, on: bool) -> None:
        if hasattr(self, "upload_btn"):
            if on:
                self.refresh_pending_label()
            else:
                self.upload_btn.configure(state="disabled")

    def refresh_global_label(self) -> None:
        if self.global_var is None:
            return
        info = self.app.db.global_info()
        if not info["synced_at"]:
            self.global_var.set("Not downloaded yet.")
        else:
            self.global_var.set(f"{info['count']} names, version {info['version']}, synced {info['synced_at'][:16]}")

    def set_refresh_enabled(self, on: bool) -> None:
        if self.global_btn is not None:
            self.global_btn.configure(state="normal" if on else "disabled")


# ============================================================================ Mini
MINI_BG = "#202124"
MINI_FG = "#f5f5f5"
MINI_DIM = "#9e9e9e"
MINI_STATE_FG = {"trading": "#66bb6a", "fighting": "#ef5350", "afk": "#bdbdbd", "fake": "#ffd54f"}
MINI_NEW_FG = "#80d8ff"
MINI_HISTORY = 10   # encounters shown on the mini HUD strip


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
        self.undo_btn = tk.Button(bar, text=" undo ", command=app.home.undo_save, width=8,
                                  disabledforeground="#4a4a4a", **small)
        self.undo_btn.pack(side="right")

        self.state_lbl = tk.Label(self, text="", fg=MINI_DIM, bg=MINI_BG, font=("", 10, "bold"), anchor="w")
        self.state_lbl.pack(fill="x", padx=6)
        self.src_lbl = tk.Label(self, text="", fg=MINI_DIM, bg=MINI_BG, font=("", 8), anchor="w")
        self.note_lbl = tk.Label(self, text="", fg=MINI_FG, bg=MINI_BG, font=("", 9), anchor="w",
                                 justify="left", wraplength=330)
        self.history = HistoryPanel(self, slots=MINI_HISTORY, caption_fg="#777777", who=("you", "everyone"),
                                    font=("", 7), fg=MINI_DIM, bg=MINI_BG)
        self.history.pack(fill="x", padx=6, pady=(3, 0))

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

        drag = [bar, self.name_lbl, self.state_lbl, self.src_lbl, self.note_lbl, self.history, btns, foot]
        drag += [w for pair in self.history.rows for w in pair]
        for w in drag:  # everything but the buttons
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.minsize(330, 1)
        self.geometry(app.db.get_setting("mini_pos") or "+60+60")
        self.update_idletasks()
        self.lift()
        self.set_undo(app.home._last_save is not None)
        self.set_cooldown(app.home._cooldown_left())

    def set_undo(self, on: bool) -> None:
        self.undo_btn.configure(state="normal" if on else "disabled")

    def set_cooldown(self, left: float) -> None:
        """Seconds in which clicks for the same name are ignored."""
        self.undo_btn.configure(text=f" undo {left:.0f}s " if left > 0 else " undo ")

    def set_record(self, name: str, local, glob, local_rows: list, global_rows: list) -> None:
        """The Home panel compressed: headline from whichever source changed last, one line with
        both states and counts, both notes, both timelines."""
        if not name:
            local = glob = None
            local_rows, global_rows = [], []
        self.history.set(local_rows, global_rows)
        note = notes_text(local, glob, you="you", shared="all", both="note")
        if note:
            self.note_lbl.configure(text=note)
            self.note_lbl.pack(fill="x", padx=6, pady=(3, 0), after=self.history)
        else:
            self.note_lbl.pack_forget()
        head = newest(local, glob)
        if not name:
            self.state_lbl.configure(text="waiting for a name...", fg=MINI_DIM)
        elif head is None:
            self.state_lbl.configure(text="NEW  -  no record yet", fg=MINI_NEW_FG)
        else:
            st = head["state"]
            last = global_rows[-1]["ts"] if head is glob and global_rows else head["updated_at"]
            self.state_lbl.configure(text=f"{STATE_LABELS[st]}  -  last {last[:16]}", fg=MINI_STATE_FG[st])
        parts = []
        if head is not None:
            parts.append(f"you: {STATE_LABELS[local['state']]} {local['times_seen']}x" if local is not None else "you: -")
            if sync.table_url():
                parts.append(f"everyone: {STATE_LABELS[glob['state']]} {glob['times_seen']}x" if glob is not None
                             else "everyone: -")
        if parts:
            self.src_lbl.configure(text="   |   ".join(parts))
            self.src_lbl.pack(fill="x", padx=6, after=self.state_lbl)
        else:
            self.src_lbl.pack_forget()

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
