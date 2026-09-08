"""Entry point: python run.py  (also the PyInstaller entry, see build.py).

    run.py                      start the app (re-launches itself inside .venv if needed)
    run.py --selftest [FILE]    load the OCR engine, read a synthetic image, write the result
                                to FILE (default: selftest.txt next to the executable/script)
                                and exit 0 on success. Useful to verify a release build.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    return Path(sys.executable).resolve().parent if FROZEN else ROOT


def _show_error(title: str, text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
    except Exception:  # noqa: BLE001
        sys.stderr.write(text + "\n")


def _ensure_dependencies() -> None:
    """If started with an interpreter that lacks the deps, hop into the project's .venv."""
    if FROZEN:
        return
    try:
        import mss  # noqa: F401
        import numpy  # noqa: F401
        import PIL  # noqa: F401
        return
    except ImportError:
        pass
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
        raise SystemExit(subprocess.call([str(venv_py), str(ROOT / "run.py"), *sys.argv[1:]]))
    _show_error(
        "Trade Check - setup needed",
        "The Python packages this app needs are not installed.\n\n"
        "Easiest: double-click run.bat (Windows) or run ./run.sh (macOS/Linux) - it creates the\n"
        ".venv folder and installs everything on first start.\n\n"
        "Manual: python -m venv .venv, then install requirements.txt into it and start the app with\n"
        "the python inside .venv.\n\nOr use the single-file release build: dist/TradeCheck.exe",
    )
    raise SystemExit(1)


def selftest(out: Path) -> int:
    lines = [f"python {sys.version.split()[0]}  frozen={FROZEN}  {sys.platform}"]
    try:
        import tkinter

        lines.append(f"tcl/tk {tkinter.Tcl().eval('info patchlevel')}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"tkinter unavailable: {exc}")
    code = 0
    try:
        from PIL import Image, ImageDraw

        from app import ocr
        from app.db import DB_PATH

        lines.append(f"database: {DB_PATH}")
        t0 = time.perf_counter()
        engine = ocr.make_engine("auto")
        lines.append(f"engine {engine.name} loaded in {time.perf_counter() - t0:.1f}s")
        img = Image.new("RGB", (220, 40), (20, 24, 30))
        ImageDraw.Draw(img).text((8, 12), "SelfTest 42", fill=(230, 230, 230))
        t0 = time.perf_counter()
        name, conf, rows, _ = ocr.read_name(img, engine, ocr.PreprocessConfig(scale=3))
        lines.append(f"read {name!r} conf={conf:.2f} in {time.perf_counter() - t0:.2f}s rows={[r.text for r in rows]}")
        lines.append("OK")
    except Exception:  # noqa: BLE001
        lines.append(traceback.format_exc())
        lines.append("FAILED")
        code = 2
    text = "\n".join(lines) + "\n"
    out.write_text(text, encoding="utf-8")
    if sys.stdout is not None:  # a windowed (no-console) build has no stdout
        sys.stdout.write(text)
    return code


def main() -> None:
    _ensure_dependencies()
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        out = Path(sys.argv[2]) if len(sys.argv) > 2 else _base_dir() / "selftest.txt"
        raise SystemExit(selftest(out))
    try:
        from app.ui import main as ui_main

        ui_main()
    except Exception:  # noqa: BLE001 - a windowed exe has no console, so show the error in a dialog
        _show_error("Trade Check - fatal error", traceback.format_exc())
        raise SystemExit(1)


if __name__ == "__main__":
    main()
