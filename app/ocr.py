"""OCR: image preprocessing, pluggable engines (RapidOCR default, Tesseract optional), name picking.

Everything that affects recognition quality lives here so it can be tuned with
tools/ocr_tune.py against the screenshots in samples/.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps


# --------------------------------------------------------------------------- data
@dataclass
class OcrLine:
    text: str
    confidence: float  # 0..1
    box: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1 in the OCR'd image


def _quad_to_box(quad) -> tuple[float, float, float, float]:
    pts = np.asarray(quad, dtype=float).reshape(-1, 2)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))


@dataclass
class PreprocessConfig:
    scale: float = 3.0          # upscale factor before OCR; 3x measured best on the enemy-plate samples
    grayscale: bool = True
    invert: bool = False        # light-on-dark text -> dark-on-light (Tesseract likes this)
    threshold: int | None = None  # 0..255 binarisation cut-off; None = off
    pad: int = 30               # border (in source pixels) added around the crop before OCR; text detectors
                                # miss short words that touch the image edge ("Bz" was found only with padding)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PreprocessConfig":
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


# ------------------------------------------------------------------ preprocessing
def border_colour(img: Image.Image) -> tuple[int, int, int]:
    """Median colour of the outermost pixel ring, used to pad without creating a fake edge."""
    a = np.asarray(img.convert("RGB"))
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return tuple(int(v) for v in np.median(edge, axis=0))


def preprocess(img: Image.Image, cfg: PreprocessConfig) -> Image.Image:
    im = img.convert("RGB")
    if cfg.pad:
        im = ImageOps.expand(im, border=int(cfg.pad), fill=border_colour(im))
    if cfg.grayscale:
        im = ImageOps.grayscale(im)
    if cfg.scale and cfg.scale != 1:
        w, h = im.size
        im = im.resize((max(1, round(w * cfg.scale)), max(1, round(h * cfg.scale))), Image.LANCZOS)
    if cfg.invert:
        im = ImageOps.invert(im.convert("L") if im.mode != "L" else im)
    if cfg.threshold is not None:
        t = int(cfg.threshold)
        im = im.convert("L").point(lambda p: 255 if p > t else 0)
    return im.convert("RGB")


# ------------------------------------------------------------------------ engines
class OcrEngine(Protocol):
    name: str

    def read(self, img: Image.Image) -> list[OcrLine]: ...


class RapidOcrEngine:
    """PaddleOCR models via ONNX runtime. pip install rapidocr_onnxruntime (no external binary)."""

    name = "rapidocr"

    # Detector input: cap the LONG side at 960 px. RapidOCR's default instead stretches the SHORT
    # side up to 736 px, which turns an already upscaled nameplate crop into giant glyphs the
    # detector fragments or misses ("Bz", "Mr. Anomas" -> "Mr.1Anomas"). Measured 12/12 vs 9/12.
    DET_LIMIT_SIDE = 960
    TEXT_SCORE = 0.4  # RapidOCR drops results below this; short names score ~0.5, so leave margin

    # Angle classifier OFF. It only ever decides "0 or 180 degrees", and on this game's slab-serif
    # nameplate font it sometimes votes 180 and hands the recogniser an upside-down crop:
    # "Wonzgonz" came back as "Zuo6zuoM" (conf 0.70); with the classifier off, "Wonzgonz" at 0.88.
    # Nameplates are never rotated, so there is nothing for it to fix.
    USE_ANGLE_CLS = False

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            try:
                self._ocr = RapidOCR(
                    det_model_path=None, det_limit_side_len=self.DET_LIMIT_SIDE, det_limit_type="max",
                    text_score=self.TEXT_SCORE, use_angle_cls=self.USE_ANGLE_CLS,
                )
            except (TypeError, KeyError):  # other 1.x versions with a different override scheme
                self._ocr = RapidOCR()
        except ImportError:
            from rapidocr import RapidOCR  # type: ignore  # newer package name

            try:
                self._ocr = RapidOCR(params={
                    "Det.limit_side_len": self.DET_LIMIT_SIDE, "Det.limit_type": "max",
                    "Global.text_score": self.TEXT_SCORE, "Global.use_cls": self.USE_ANGLE_CLS,
                })
            except Exception:  # noqa: BLE001
                self._ocr = RapidOCR()

    def read(self, img: Image.Image) -> list[OcrLine]:
        arr = np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])  # RGB -> BGR
        out = self._ocr(arr)
        lines: list[OcrLine] = []
        if isinstance(out, tuple):  # rapidocr_onnxruntime: (result, elapse)
            for box, text, score in out[0] or []:
                lines.append(OcrLine(str(text), float(score), _quad_to_box(box)))
        elif out is not None and getattr(out, "txts", None) is not None:  # rapidocr >= 2
            boxes = out.boxes if getattr(out, "boxes", None) is not None else [None] * len(out.txts)
            for text, score, box in zip(out.txts, out.scores, boxes):
                lines.append(OcrLine(str(text), float(score), _quad_to_box(box) if box is not None else None))
        return lines


class TesseractEngine:
    """Requires the tesseract binary on PATH plus `pip install pytesseract`."""

    name = "tesseract"

    def __init__(self, cmd: str | None = None, psm: int = 6) -> None:
        import pytesseract  # type: ignore

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        self._pt = pytesseract
        self._psm = psm

    def read(self, img: Image.Image) -> list[OcrLine]:
        data = self._pt.image_to_data(
            img, config=f"--psm {self._psm}", output_type=self._pt.Output.DICT
        )
        grouped: dict[tuple, list[tuple]] = {}
        for i, word in enumerate(data["text"]):
            word = (word or "").strip()
            if not word:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x0, y0 = data["left"][i], data["top"][i]
            grouped.setdefault(key, []).append(
                (word, conf / 100.0, (x0, y0, x0 + data["width"][i], y0 + data["height"][i]))
            )
        lines = []
        for words in grouped.values():
            text = " ".join(w for w, _, _ in words)
            conf = sum(c for _, c, _ in words) / len(words)
            bx = [b for _, _, b in words]
            box = (min(b[0] for b in bx), min(b[1] for b in bx), max(b[2] for b in bx), max(b[3] for b in bx))
            lines.append(OcrLine(text, conf, box))
        return lines


def make_engine(name: str = "auto") -> OcrEngine:
    errors = []
    if name in ("auto", "rapidocr"):
        try:
            return RapidOcrEngine()
        except ImportError as exc:
            errors.append(f"rapidocr: {exc}")
    if name in ("auto", "tesseract"):
        try:
            return TesseractEngine()
        except ImportError as exc:
            errors.append(f"tesseract: {exc}")
    raise RuntimeError(
        "No OCR engine available (" + "; ".join(errors) + "). "
        "Install one with: pip install rapidocr_onnxruntime"
    )


# --------------------------------------------------------------------- name logic
# Characters allowed in a player name: word characters (letters, digits, "_"), space, and the
# punctuation players actually put in names and clan tags. Kept as an allowlist so control
# characters and stray unicode from a bad read are dropped, but wide enough that nothing a
# player can legitimately type is silently deleted -- a name that loses a bracket or a dash no
# longer matches its own record.
_ALLOWED = re.compile(r"""[^\w \-.\[\](){}<>|'"`~!?@#$%^&*+=,:;/]+""", re.UNICODE)
# Rows that are never a name: bare numbers and the "Lvl. 45" line under the nameplate.
_NOT_A_NAME = re.compile(r"^\W*(lvl|lv|level)?\W*\d+\W*$", re.IGNORECASE)


def clean_name(text: str) -> str:
    text = _ALLOWED.sub("", text or "")
    return " ".join(text.split())


def _overlap(left: str, right: str, max_len: int = 3) -> int:
    """Length of the longest suffix of `left` that is also a prefix of `right`."""
    for k in range(min(max_len, len(left), len(right)), 0, -1):
        if left[-k:] == right[:k]:
            return k
    return 0


def merge_rows(lines: list[OcrLine], same_row: float = 0.5, glue_gap: float = 0.3) -> list[OcrLine]:
    """Join fragments that sit on the same text row, left to right.

    Detectors often split a name at underscores or brackets ("Trader_Joe99" -> "Trader." + "Joe99").
    Fragments whose vertical centres differ by less than `same_row` x text height are one row;
    neighbours closer than `glue_gap` x text height are glued without a space.
    """
    boxed = [l for l in lines if l.box is not None]
    if len(boxed) < 2:
        return list(lines)
    rows: list[list[OcrLine]] = []
    for line in sorted(boxed, key=lambda l: (l.box[1] + l.box[3]) / 2):
        cy, h = (line.box[1] + line.box[3]) / 2, line.box[3] - line.box[1]
        for row in rows:
            ref = row[0]
            rcy, rh = (ref.box[1] + ref.box[3]) / 2, ref.box[3] - ref.box[1]
            if abs(cy - rcy) < same_row * max(h, rh, 1):
                row.append(line)
                break
        else:
            rows.append([line])
    merged: list[OcrLine] = []
    for row in rows:
        row.sort(key=lambda l: l.box[0])
        text = row[0].text.strip()
        for prev, cur in zip(row, row[1:]):
            gap = cur.box[0] - prev.box[2]
            h = max(prev.box[3] - prev.box[1], cur.box[3] - cur.box[1], 1)
            piece = cur.text.strip()
            if gap < 0:  # boxes overlap: the same characters may appear at the seam
                piece = piece[_overlap(text, piece):]
            text += ("" if gap < glue_gap * h else " ") + piece
        conf = sum(l.confidence for l in row) / len(row)
        box = (min(l.box[0] for l in row), min(l.box[1] for l in row),
               max(l.box[2] for l in row), max(l.box[3] for l in row))
        merged.append(OcrLine(text, conf, box))
    merged.extend(l for l in lines if l.box is None)
    return merged


def pick_name(lines: list[OcrLine], min_conf: float = 0.0) -> tuple[str, float]:
    """Highest-confidence line that still has text after cleaning."""
    best: tuple[str, float] = ("", 0.0)
    for line in sorted(lines, key=lambda l: l.confidence, reverse=True):
        if line.confidence < min_conf:
            continue
        name = clean_name(line.text)
        if name and not _NOT_A_NAME.match(name):
            return name, line.confidence
    return best


def read_name(
    img: Image.Image, engine: OcrEngine, cfg: PreprocessConfig | None = None
) -> tuple[str, float, list[OcrLine], Image.Image]:
    """Full pipeline. Returns (name, confidence, all_lines, preprocessed_image)."""
    cfg = cfg or PreprocessConfig()
    processed = preprocess(img, cfg)
    lines = merge_rows(engine.read(processed))
    lines.sort(key=lambda l: l.confidence, reverse=True)
    name, conf = pick_name(lines)
    return name, conf, lines, processed
