# Iris accuracy overhaul + Gauntlet training ground (2026-09-06)

## Why
Iris benches at 100% on its own harness, yet feels inaccurate and messy in real
use. Investigation (this session) found the bench and reality diverge:

| # | Root cause | Where | Severity |
|---|-----------|-------|----------|
| 1 | `see`/`screenshot` capture ALL monitors (~8320 px wide), shrink ~5x to a 1568 px JPEG and return NO scale/origin metadata. Any coordinate Claude reads off the image is wrong by the shrink factor. This is the most used tool (155 calls in the log). | `__main__.py` see/screenshot, `vision.encode_jpeg` | Critical |
| 2 | Iris is not registered as an MCP server in any Claude config (not in `~/.claude.json` since at least 2026-08-20). Every real use has been improvised via direct Python imports with hand-rolled coordinate offsets (see docs/improvement-ideas-2026-06-10.md #4). | config | Critical |
| 3 | No pixel-level backend. A target with no text and no UIA node (dot, icon, canvas widget) is unfindable except by eyeballing a downscaled JPEG. | resolver | High |
| 4 | OCR tuned for documents, not UI: default psm 3, 2x upscale only when window height < 400 px, whole window in one pass. Text under ~12 px tall is invisible. | `vision._preprocess_for_ocr`, `ocr_text` | High |
| 5 | OCR cache keyed on an 8x8 perceptual hash of the whole window. Small UI changes (a dot moving, a checkbox toggling, a button appearing) do not change the hash, so stale words are returned. | `vision.cached_ocr` | High |
| 6 | UIA `query` is case-insensitive substring, stops at the first 10 matches in tree order, no ranking by role or exactness. "Create" in Telegram returns 10 chat rows and never reaches the Create button. | `semantic.query`, `resolver.find` | High |
| 7 | `click` takes `hits[0]` (highest fuzzy similarity, ties in list order). Duplicate labels resolve arbitrarily. | `__main__.click` | Medium |
| 8 | Zero observability: the log records `CallToolRequest` with no tool name, target, backend or outcome. Failures cannot be mined. | logging | Medium |
| 9 | Bench is one Tk window, 10 large text buttons (min 68x28 px), OCR path only, no speed metric. 100% there says little about tiny/multi-window/moving targets. | `iris/bench` | Medium |
| 10 | Known gaps still open: `focus(raise_window=True)` unreliable; `supports_uia` cached per PID forever. | spatial/semantic | Medium |

## What ships in this branch

### A. Pixel backend (`iris/pixel.py`)
- `find_color(img, rgb, tolerance, min_area, max_area)`: tolerance mask +
  connected components -> hits with bbox/center/area. cv2 when present,
  pure-Python BFS fallback.
- `find_template(img, template, threshold)`: cv2 `matchTemplate` (TM_CCOEFF_NORMED),
  non-max suppression.
- Resolver target grammar: `color:#RRGGBB[~tol]`, `image:<png path>`; plain
  text keeps the UIA -> OCR -> handoff path. Backend name `pixel`.

### B. OCR for UI (`iris/vision.py`)
- Tesseract `--psm 11` (sparse text) by default, env override `IRIS_OCR_PSM`.
- Two-pass: 1x first, then 2x upscale if no match (bounded by image area).
- Cache key: sha1 of full-resolution bytes + scale. Exact, cheap (~3 ms/2 MP).

### C. UIA ranking (`iris/resolver.py`)
- Pull up to 100 candidates, score by name exactness, role class, invokability,
  visible bounds. Optional `prefer_roles` on `find`/`click`.

### D. Screenshot truth (`iris/__main__.py`)
- `see`/`see_full`/`screenshot*` return `[image, meta]` where meta carries
  `origin`, `captured_size`, `image_size`, `scale`, and the formula
  `screen = origin + image_xy / scale`.
- `grid=True` draws a labeled coordinate grid in SCREEN coordinates.
- New `zoom(x, y, size, magnify)` tool: full-res crop around a point with a
  fine grid, for honing in on tiny targets.

### E. Telemetry (`iris/telemetry.py`)
- Every `find` and `click` appends one JSON line to `<log_dir>/events.jsonl`.

### F. Gauntlet (`iris/bench/gauntlet_harness.py`, `iris/bench/gauntlet.py`)
Multi-window, tiny-target, timed training ground. Canvas-based Tk windows
(no UIA for widgets by design) with exact pixel ground truth. Levels:

| Level | Targets | Exercises |
|-------|---------|-----------|
| tiny_text | 7 to 10 pt labels, 3 to 6 chars | OCR small text, two-pass upscale |
| color_dots | squares 24, 12, 8, 4, 2 px, unique colors | pixel backend, click precision |
| decoys | target color among near-shades (delta ~40) | tolerance, ranking |
| multi_window | same dot in 3 windows, click the one in window N | token scoping, focus |
| overlap | window B covers the target in A | raise/occlusion path |
| whack | target relocates after each hit, 12 s budget | latency, cache staleness |
| template | 12 to 16 px glyph icons | template matching |

Scoring per level: attempts, correct, wrong-target, wrong-window, miss px
(mean/p50/p95/max), ms per hit, hits per second (whack). JSON report to
`assets/gauntlet-last.json`; console table; non-zero exit on regressions
below thresholds so it can gate changes like the existing bench.

## Verification
- `pytest tests/unit` green (was 150 passing before this branch).
- `python -m iris.bench.runner` still 100% correct-button.
- `python -m iris.bench.gauntlet` run recorded in `assets/gauntlet-output.txt`.
- Live runs wait for >= 90 s of user idle time; the mouse is restored after.

## Out of scope (follow-ups)
- Screen-wide (tokenless) pixel search.
- A reliable `raise_window`; still needs a real fix (AttachThreadInput dance).
- Telegram recipe pack (improvement-ideas #3).
