r"""Build the single-file executable with PyInstaller; optionally package it as a release zip.

    .venv\Scripts\python -m pip install pyinstaller
    .venv\Scripts\python build.py             # dist/TradeCheck.exe (windowed)
    .venv\Scripts\python build.py --release   # + release/TradeCheck-v<version>-<platform>.zip (+ .sha256)
    .venv\Scripts\python build.py --console   # keep a console window (debugging)

PyInstaller does not cross-compile: run this on each OS you ship to, or push a tag and let
.github/workflows/release.yml build all three. The database lives in the per-user app-data
folder shared with the source checkout (see app/db.py). Bump the version in app/__init__.py.
"""
from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402

NAME = "TradeCheck"
ICON = ROOT / "assets" / "icon.ico"


def platform_tag() -> str:
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    system = {"win32": "win", "darwin": "macos"}.get(sys.platform, "linux")
    return f"{system}-{arch}"


def ensure_icon() -> Path | None:
    """Generate a simple app icon (dark plate, gold 'TC') if none has been provided."""
    if ICON.exists():
        return ICON
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    ICON.parent.mkdir(exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, size - 8, size - 8), radius=48, fill=(32, 33, 36), outline=(97, 95, 80), width=8)
    try:
        font = ImageFont.truetype("arialbd.ttf", 124)
    except OSError:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2 + 4), "TC", fill=(255, 200, 60), font=font, anchor="mm")
    img.save(ICON, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return ICON


def prefetch_models() -> None:
    """Download the OCR models into the rapidocr package so --collect-all bundles them.

    rapidocr fetches its .onnx files on first use into <site-packages>/rapidocr/models. A frozen
    exe extracts to a fresh temp dir every run, so anything not bundled would be re-downloaded on
    every start (and fail with no network). Instantiating the engine here pulls them in first.
    """
    sys.path.insert(0, str(ROOT))
    from app import ocr

    print("Fetching OCR models...")
    engine = ocr.make_engine("rapidocr")
    for grab in (getattr(engine, "_cyrillic", None),):  # second pass is lazy; force it too
        if grab is not None:
            grab()
    print("OCR models ready.")


def build(console: bool) -> int:
    for d in ("build", "dist"):
        try:
            shutil.rmtree(ROOT / d)
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            print(f"Cannot clean {d}/: {exc}")
            print(f"Is a previous {NAME} still running? Close it and retry.")
            return 1
    for spec in ROOT.glob("*.spec"):
        spec.unlink()

    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", NAME,
        "--collect-all", "rapidocr",
        "--collect-all", "onnxruntime",
        "--hidden-import", "PIL._tkinter_finder",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
    ]
    if not console:
        args.append("--windowed")
    icon = ensure_icon()
    if icon:
        args += ["--icon", str(icon)]
    args.append(str(ROOT / "run.py"))
    print(" ".join(args))
    return subprocess.run(args, cwd=ROOT).returncode


QUICK_START = """Trade Check v{version} ({platform})
=================================

Reads the enemy name from the game's top-right nameplate and remembers whether that player
was TRADING, FIGHTING, CLIMBING, DROPPING, STALLING or TRAITOR the last time you met.

Quick start
-----------
1. Unzip anywhere and start {exe}. The first start takes a few seconds.
   Windows may show "Windows protected your PC" because the file is not code-signed:
   click "More info", then "Run anyway".
2. Click "Select region..." and drag a box around the enemy name: leave a little space
   above it and stop above the "Lvl." line. Auto-read starts right away.
3. When a name appears, click one of the state buttons (TRADING, FIGHTING, CLIMBING, DROPPING,
   STALLING, TRAITOR). Next time you meet that player,
   the previous state is shown. Use the Records tab to search or edit.
4. "Mini mode" shrinks the app to a small always-on-top overlay to keep over the game
   (windowed or borderless mode; exclusive fullscreen hides overlays).

Your records live in one shared per-user database (Windows: %APPDATA%/TradeCheck/records.sqlite),
so this exe and a source checkout on the same machine see the same data. Back that file up to keep
your records. A data folder from an older version next to the exe is merged in automatically.
Troubleshooting: run "{exe} --selftest" - it writes selftest.txt ending in OK.
"""


def package() -> Path:
    dist = ROOT / "dist"
    rel = ROOT / "release"
    rel.mkdir(exist_ok=True)
    exe = dist / (f"{NAME}.exe" if sys.platform == "win32" else NAME)
    out = rel / f"{NAME}-v{__version__}-{platform_tag()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, exe.name)
        app_bundle = dist / f"{NAME}.app"
        if app_bundle.is_dir():  # macOS also gets the .app bundle
            for f in app_bundle.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(dist))
        z.writestr("README.txt", QUICK_START.format(version=__version__, platform=platform_tag(), exe=exe.name))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.with_suffix(".zip.sha256")).write_text(f"{digest}  {out.name}\n", encoding="ascii")
    return out


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed:  python -m pip install pyinstaller")
        return 1
    prefetch_models()
    rc = build(console="--console" in sys.argv)
    if rc != 0:
        return rc
    print("\nBuilt:")
    for p in sorted((ROOT / "dist").iterdir()):
        size = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        print(f"  {p.name:30s} {size / 1e6:7.1f} MB")
    if "--release" in sys.argv:
        out = package()
        print(f"\nRelease package: {out}  ({out.stat().st_size / 1e6:.1f} MB)\n  checksum: {out.with_suffix('.zip.sha256').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
