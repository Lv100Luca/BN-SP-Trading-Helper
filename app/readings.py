"""Reading log: keep the capture behind every name the app read, so a misread can be checked
and fixed after the fact (and the record that was saved under the wrong name repaired).

On by default when running from source, off in the packaged exe. The TRADECHECK_READ_LOG
environment variable overrides either way (1/0). Images are PNGs in <data dir>/readings/,
one per row of the `readings` table in the shared database (see Database.add_reading).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image

from . import ocr
from .db import DATA_DIR, Database

READINGS_DIR = DATA_DIR / "readings"
# Readings nobody saved a state from or checked are pruned beyond this many; a nameplate crop is
# a few KB, so even the cap is only a few MB. Saved/checked readings are kept for good.
KEEP_UNSAVED = 500


def enabled() -> bool:
    env = os.environ.get("TRADECHECK_READ_LOG")
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    return not getattr(sys, "frozen", False)


class ReadingLog:
    def __init__(self, db: Database, folder: Path | None = None) -> None:
        self.db = db
        self.dir = Path(folder) if folder else READINGS_DIR

    def add(self, img: Image.Image, *, engine: str, name: str, conf: float, lines: list[ocr.OcrLine],
            region, cfg: ocr.PreprocessConfig, source: str) -> int:
        """Store the capture that produced `name` (the raw grab, exactly what OCR was given)."""
        alts = [[line.text, round(line.confidence, 3)] for line in lines[:8]]
        rid = self.db.add_reading(engine=engine, read_name=name, confidence=conf, alternatives=alts,
                                  region=region, preprocess=cfg.to_dict(), source=source)
        fname = f"{rid:06d}.png"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            img.save(self.dir / fname)
            self.db.set_reading_image(rid, fname)
        except OSError as exc:
            print(f"reading image not saved: {exc}", file=sys.stderr)
        self.prune()
        return rid

    def path(self, row) -> Path | None:
        return self.dir / row["image"] if row is not None and row["image"] else None

    def image(self, row) -> Image.Image | None:
        p = self.path(row)
        if p is None or not p.exists():
            return None
        try:
            with Image.open(p) as im:
                return im.convert("RGB")
        except OSError:
            return None

    def delete(self, ids: Iterable[int]) -> int:
        images = self.db.delete_readings(ids)
        for name in images:
            try:
                (self.dir / name).unlink()
            except OSError:
                pass
        return len(images)

    def prune(self, keep: int = KEEP_UNSAVED) -> int:
        stale = self.db.stale_reading_ids(keep)
        return self.delete(stale) if stale else 0
