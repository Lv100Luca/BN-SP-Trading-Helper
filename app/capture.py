"""Cross-platform screen capture (mss) and a drag-to-select region overlay (tkinter)."""
from __future__ import annotations

import tkinter as tk

import mss
from PIL import Image

Region = tuple[int, int, int, int]  # x, y, width, height in virtual-screen pixels


def virtual_screen() -> dict:
    """Bounding box of all monitors: {'left','top','width','height'}."""
    with mss.mss() as sct:
        return dict(sct.monitors[0])


def grab(region: Region) -> Image.Image:
    x, y, w, h = region
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def grab_full() -> Image.Image:
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class RegionSelector:
    """Full-screen translucent overlay. Drag a rectangle, release to confirm, Esc to cancel."""

    MIN_SIZE = 4

    def __init__(self, root: tk.Misc):
        self.root = root
        self.result: Region | None = None

    def select(self) -> Region | None:
        vs = virtual_screen()
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"{vs['width']}x{vs['height']}+{vs['left']}+{vs['top']}")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        top.configure(bg="black", cursor="crosshair")

        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            vs["width"] // 2, 40,
            text="Drag a box around the enemy name.  Release to confirm.  Esc to cancel.",
            fill="white", font=("", 14, "bold"),
        )

        start: list[int] = []
        rect = {"id": None}

        def on_press(e: tk.Event) -> None:
            start[:] = [e.x, e.y]
            if rect["id"] is not None:
                canvas.delete(rect["id"])
            rect["id"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00e5ff", width=2)

        def on_drag(e: tk.Event) -> None:
            if start and rect["id"] is not None:
                canvas.coords(rect["id"], start[0], start[1], e.x, e.y)

        def on_release(e: tk.Event) -> None:
            if not start:
                return
            x0, y0 = start
            x1, y1 = e.x, e.y
            left, right = sorted((x0, x1))
            topp, bottom = sorted((y0, y1))
            w, h = right - left, bottom - topp
            if w >= self.MIN_SIZE and h >= self.MIN_SIZE:
                self.result = (vs["left"] + left, vs["top"] + topp, w, h)
            top.destroy()

        def on_cancel(_e: tk.Event) -> None:
            self.result = None
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_cancel)

        top.update_idletasks()
        top.lift()
        top.focus_force()
        try:
            top.grab_set()
        except tk.TclError:
            pass
        self.root.wait_window(top)
        return self.result
