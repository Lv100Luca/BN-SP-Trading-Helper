# OCR samples

Drop game screenshots here (PNG or JPG). Two kinds work:

1. **Full screenshots** at the same resolution you play at. The region you selected in the
   app is applied to them automatically by `tools/ocr_tune.py`.
2. **Pre-cropped snippets** of just the name. Run the tuner with `--full` for these.

Optional: create `labels.txt` next to the images with the expected name per file, so the
tuner reports accuracy:

```
screenshot_001.png	SomePlayerName
screenshot_002.png	Another_Guy
```

(tab or `=` between file name and expected name; lines starting with `#` are ignored.)

Then, from the project root:

```
.venv\Scripts\python tools\ocr_tune.py --save-debug
```

`_debug/` will contain the crop and the preprocessed image for every sample so we can see
exactly what the OCR engine saw. `_debug/` is regenerated on every run.

## This game's enemy plate

The enemy name sits in the top-right plate and the HUD scales with the window width, so the same
relative crop works for every window size:

```
.venv\Scripts\python tools\ocr_tune.py --rel-region 0.054,0.005,0.135,0.025 --save-debug
```

(`R,T,W,H` as fractions of the image width; `R` is the gap between the crop's right edge and the
image's right edge.) Measured 2026-09-05 on five screenshots: 5/5 loose matches, misses were only
the "." / space in "Mr. Anomas". In the app, drag the region over the name text only, above the
"Lvl." line, and re-select it if you move or resize the game window.
