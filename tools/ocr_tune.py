"""Run OCR over the screenshots in samples/ so preprocessing can be tuned.

Examples (run from the project root with the venv python):
  python tools/ocr_tune.py                        # crop with the region saved by the app
  python tools/ocr_tune.py --region 100,50,300,40 # crop with an explicit x,y,w,h
  python tools/ocr_tune.py --full                 # no crop (samples are already snippets)
  python tools/ocr_tune.py --scale 3 --threshold 140 --invert --save-debug
  python tools/ocr_tune.py --engine tesseract
  python tools/ocr_tune.py --scale 3 --save       # persist these options for the app
  python tools/ocr_tune.py --readings             # re-run OCR over the captures the app logged
                                                  # (Readings tab); names you fixed there are the labels

Optional samples/labels.txt (one per line, "filename<TAB>expected name" or
"filename=expected name") turns the run into an accuracy check.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app import capture, ocr  # noqa: E402
from app.db import Database, name_key  # noqa: E402
from app.readings import READINGS_DIR  # noqa: E402

SAMPLES = ROOT / "samples"
DEBUG_DIR = SAMPLES / "_debug"
EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_labels() -> dict[str, str]:
    path = SAMPLES / "labels.txt"
    labels: dict[str, str] = {}
    if not path.exists():
        return labels
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sep = "\t" if "\t" in line else "="
        if sep not in line:
            continue
        fname, expected = line.split(sep, 1)
        labels[fname.strip()] = expected.strip()
    return labels


def reading_labels(db: Database) -> dict[str, str]:
    """image file -> name a human confirmed or fixed in the app's Readings tab."""
    return {r["image"]: r["fixed_name"] for r in db.readings(limit=100000) if r["image"] and r["fixed_name"]}


def parse_region(text: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must be x,y,w,h")
    return tuple(parts)  # type: ignore[return-value]


def parse_rel_region(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("rel-region must be R,T,W,H (fractions of image width)")
    return tuple(parts)  # type: ignore[return-value]


def rel_to_abs(rel: tuple[float, float, float, float], width: int) -> tuple[int, int, int, int]:
    r, t, w, h = rel
    return (round(width - (r + w) * width), round(t * width), round(w * width), round(h * width))


def _grow(region: tuple[int, int, int, int], margin: int, size: tuple[int, int]):
    """`region` grown by `margin` on every side, clipped to the image -- capture.grab's margin."""
    x, y, w, h = region
    iw, ih = size
    left, top = max(0, x - margin), max(0, y - margin)
    right, bottom = min(iw, x + w + margin), min(ih, y + h + margin)
    return (left, top, max(1, right - left), max(1, bottom - top))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", type=parse_region, help="x,y,w,h crop (default: region saved by the app)")
    ap.add_argument("--full", action="store_true", help="do not crop; OCR the whole image")
    ap.add_argument("--rel-region", type=parse_rel_region, metavar="R,T,W,H",
                    help="crop given as fractions of the image WIDTH, anchored top-right: "
                         "R = distance from the right edge, T = top, W/H = size. Works across window sizes.")
    ap.add_argument("--engine", default="auto", choices=("auto", "rapidocr", "tesseract"))
    ap.add_argument("--scale", type=float, default=None, help="upscale factor (default from app settings or 2.0)")
    ap.add_argument("--threshold", type=int, default=None, help="binarise at this gray level (0-255)")
    ap.add_argument("--pad", type=int, default=None, help="border added around the crop before OCR (default 30)")
    ap.add_argument("--invert", action="store_true", help="invert colours before OCR")
    ap.add_argument("--color", action="store_true", help="keep colour (skip grayscale)")
    ap.add_argument("--save-debug", action="store_true", help="write crops + preprocessed images to samples/_debug/")
    ap.add_argument("--save", action="store_true", help="persist these preprocessing options and engine for the app")
    ap.add_argument("--margin", type=int, default=None,
                    help=f"extra real pixels grabbed around --region, as the app does "
                         f"(default {capture.GRAB_MARGIN}; use 0 to crop exactly)")
    ap.add_argument("--only", help="only process files whose name contains this text")
    ap.add_argument("--readings", action="store_true",
                    help=f"use the app's logged captures ({READINGS_DIR}) instead of samples/: already "
                         f"cropped, so no region/margin; names fixed or confirmed in the Readings tab act as labels")
    args = ap.parse_args()

    db = Database()
    saved_cfg = ocr.PreprocessConfig.from_dict(json.loads(db.get_setting("preprocess", "{}") or "{}"))
    cfg = ocr.PreprocessConfig(
        scale=args.scale if args.scale is not None else saved_cfg.scale,
        grayscale=not args.color,
        invert=args.invert,
        threshold=args.threshold,
        pad=args.pad if args.pad is not None else saved_cfg.pad,
    )

    region = None
    if args.readings:
        args.full = True
    if not args.full and args.rel_region is None:
        region = args.region
        if region is None:
            raw = db.get_setting("region")
            if raw:
                region = tuple(int(v) for v in json.loads(raw))
        if region is None:
            print("No region given and none saved by the app; OCR-ing whole images (use --region x,y,w,h).")

    source_dir = READINGS_DIR if args.readings else SAMPLES
    files = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in EXTS) if source_dir.is_dir() else []
    if args.only:
        files = [p for p in files if args.only.lower() in p.name.lower()]
    if not files:
        hint = "Run the app from source and read some names first." if args.readings else "Drop some screenshots there first."
        print(f"No images found in {source_dir}. {hint}")
        return 1

    margin = capture.GRAB_MARGIN if args.margin is None else args.margin
    print(f"engine={args.engine}  region={region}  margin={margin}  preprocess={cfg.to_dict()}")
    t0 = time.perf_counter()
    engine = ocr.make_engine(args.engine)
    print(f"loaded {engine.name} in {time.perf_counter() - t0:.1f}s\n")

    labels = reading_labels(db) if args.readings else load_labels()
    hits = loose_hits = labelled = 0
    if args.save_debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    for path in files:
        img = Image.open(path).convert("RGB")
        crop = img
        file_region = rel_to_abs(args.rel_region, img.width) if args.rel_region else region
        if file_region and not args.rel_region:
            file_region = _grow(file_region, margin, img.size)
        if file_region:
            x, y, w, h = file_region
            if x + w <= img.width and y + h <= img.height:
                crop = img.crop((x, y, x + w, y + h))
            else:
                print(f"{path.name}: region {file_region} does not fit {img.width}x{img.height}; using full image")
        t0 = time.perf_counter()
        name, conf, lines, processed = ocr.read_name(crop, engine, cfg)
        ms = (time.perf_counter() - t0) * 1000

        mark = ""
        expected = labels.get(path.name)
        if expected is not None:
            labelled += 1
            if name.lower() == expected.lower():
                hits += 1
                loose_hits += 1
                mark = "  OK"
            elif name_key(name) == name_key(expected):
                loose_hits += 1
                mark = f"  ~OK loose match (expected {expected!r}; same record in the app)"
            else:
                mark = f"  MISS (expected {expected!r})"
        print(f"{path.name:40s} -> {name!r:28s} conf={conf:.2f}  {ms:5.0f}ms{mark}")
        for line in lines:
            print(f"    {line.confidence:.2f}  {line.text}")

        if args.save_debug:
            crop.save(DEBUG_DIR / f"{path.stem}_crop.png")
            processed.save(DEBUG_DIR / f"{path.stem}_pre.png")

    if labelled:
        print(f"\nAccuracy: exact {hits}/{labelled} ({hits / labelled:.0%}),"
              f" loose {loose_hits}/{labelled} ({loose_hits / labelled:.0%})")
    if args.save_debug:
        print(f"Debug images written to {DEBUG_DIR}")
    if args.save:
        db.set_setting("preprocess", json.dumps(cfg.to_dict()))
        db.set_setting("ocr_engine", args.engine)
        print("Saved preprocessing options + engine to the app settings.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
