# Iris Accuracy Bench

A live, instrumented test rig that drives Iris primitives against a known-truth
target window and measures where clicks actually land.

## What it does

1. Spawns `iris.bench.harness` (a Tk window with a grid of buttons of varied
   sizes, label styles, and positions). The harness writes every click it
   receives to a JSONL event log along with the exact pixel-level hit location
   and the button's true center.
2. Drives `iris.resolver.find()` + click against each target under each
   scenario. Scenarios mutate the window between attempts (drag, resize,
   move to different monitors, raise from occlusion).
3. Reads the harness's click receipts to learn EXACTLY where each click
   landed and how far from the button's center.
4. Builds a JSON + on-screen report with per-target pixel error, backend
   distribution, click-method distribution, and OCR->UIA upgrade rate.

## Run it

```
bench.bat                              run all scenarios with defaults
bench.bat --scenarios baseline_static  one scenario only
bench.bat --no-invoke                  force geometric clicks (skips UIA invoke)
bench.bat --keep-harness               leave the harness window open after
```

Or directly: `python -m iris.bench.runner [args]`

The harness window stays visible the entire run so you can watch Iris drive
it in real time. The status bar at the top of the harness shows the latest
click receipt as each attempt completes.

## Scenarios

| id | What it tests |
|---|---|
| `baseline_static` | Window placed once, no mutation. Cleanest possible. |
| `window_dragged` | Window moved 400+250 between focus and click. Verifies live-bounds fix. |
| `window_resized` | Window shrunk to 700x500. Tests bounds tracking under reshape. |
| `per_monitor` | Run once per physical monitor. Tests DPI awareness across 100/125/150%. |
| `raise_then_click` | bring_to_front before each click. Tests focus-rescue + capture alignment. |

## Targets

Defined in `harness.py:TARGETS`. Mix of:

* `medium_center` baseline
* `tiny_btn`, `short_label` precision on small targets
* `wide_row` left-aligned label, wide button (widget-not-text upgrade target)
* `icon_label` icon+label offset
* `huge_btn` easy
* `edge_right` edge of window
* `ambiguous_a/b` fuzzy disambiguation (Crimson Falcon vs Crimson Falchion)
* `lowercase_only` case-insensitive OCR

## Metrics in the report

* `find_rate` fraction of targets the resolver located
* `correct_button_rate` fraction whose click landed on the right button
* `miss_px_p50/p95/max/mean` pixel distance from button center
* `backend_distribution` UIA vs OCR vs vision_handoff
* `click_method_distribution` mouse vs uia_pattern
* `ocr_to_uia_upgrades` how often OCR hits got widget-bounds substituted

## What the bench discovered

* **OCR translation was off by the window drag delta** before the live-bounds
  fix. The bench surfaces this as 0% pass on `window_dragged`. After fix,
  100% with 0px miss.
* **UIA Invoke is a silent no-op for Tk widgets.** Buttons drawn by Tk expose
  ButtonControl + InvokePattern through UIA but the Invoke doesn't fire the
  Tk command callback. The bench reports this as `NO-RECEIPT` for `uia_pattern`
  clicks. Fix shipped in `semantic.is_invoke_trusted()`: TkTopLevel/TkChild
  ancestors are denylisted from invoke; widget bounds are still upgraded.
* **Sub-pixel rounding on the 4K @ 150% monitor.** Mean miss 1.0px when the
  harness is parked on the 150%-scaled monitor. This is an OCR-bbox-center
  vs widget-true-center quantization, not an Iris bug.

## When to add to the bench

* New target style (icon-only button, list row, tab strip) -> add to
  `harness.py:TARGETS`.
* New regression you fixed -> add a scenario that exercises it.
* New finding that surfaces in production -> file as a scenario so the bench
  catches it next time.

## Output

* Console: live colored progress + summary block
* JSON: `$TEMP/iris_bench_report.json` (full attempt-by-attempt log)
* Failures: capture PNGs in `$TEMP/iris_bench_failures/` so you can see what
  Iris was looking at when a find missed

---

# Iris Gauntlet (multi-window, tiny targets, timed)

The bench above proves Iris can hit big text buttons in one window. The
Gauntlet is the training ground for what actually hurts on a real desktop.

```
python -m iris.bench.gauntlet                       all levels, 3 windows
python -m iris.bench.gauntlet --levels color_dots,whack
python -m iris.bench.gauntlet --idle 0              do not wait for user idle
python -m iris.bench.gauntlet --windows 2 --seed 3
python -m iris.bench.gauntlet --keep                leave the windows open
```

It spawns `iris.bench.gauntlet_harness` windows (Tk canvases: no UIA for
their content, on purpose), lays targets out from a seeded RNG, and drives
the REAL server `click()` (`iris.__main__`), so the clamp, the auto-raise,
the invoke/mouse choice and telemetry are all exercised. Every window reports
the exact screen pixel that was hit and which target (if any) it was.

**Harness.** `--harness tk` (default) is the Tkinter canvas; Tk ignores posted
clicks (it reads the physical mouse), so it exercises the mouse fallback.
`--harness win32` is a plain Win32 window (ctypes + GDI) that honors posted
messages: use it to measure the quiet path. Both open without taking focus.

**Delivery.** Clicks go through the server's normal policy (`--click-method`,
default `auto` = UIA Invoke, else quiet window messages, else mouse). With
quiet delivery the cursor never moves and every attempt asserts that
(`cursor_moved` in the summary). `--click-method mouse` exercises the real
SendInput path. Either way the run waits for 90 s of user idle by default
(`--idle 0` to skip), because the harness windows still take focus when
they open, and it aborts if the user moves the pointer mid-run.

## Levels

| level | targets | exercises | pass at |
|---|---|---|---|
| `tiny_text` | 8 labels at 7-11 pt | OCR small text, 2x pass | 75% |
| `color_dots` | squares 24, 16, 12, 8, 6, 4, 3, 2 px | pixel backend, precision | 90% |
| `decoys` | exact color among shades 40 and 20 apart | tolerance, ranking | 90% |
| `multi_window` | same 3 dots in every window, hit window N's | token scoping | 90% |
| `overlap` | window B parked over A's target | occlusion + auto-raise | 75% |
| `whack` | target relocates after each hit, 10 s budget, dot then text | latency, cache staleness | 80% |
| `template` | six 12-16 px glyph PNGs | template matching | 80% |
| `marks` | six 8-12 pt labels, clicked by Set-of-Mark number | marks enumeration, click(mark=) | 90% |

Statuses per attempt: `hit`, `wrong_target`, `decoy`, `wrong_window`,
`miss` (click arrived, hit nothing; nearest target and distance reported),
`no_receipt`, or the server's refusal (`target_not_found`,
`click_outside_window`, ...).

## Output

* Console: per-attempt lines (status, window, target, backend, miss px, ms)
  and a per-level summary with pass/fail against the thresholds
* JSON: `$TEMP/iris_gauntlet_report.json` (or `--report <path>`)
* Exit code 0 only when every level clears its threshold

## Adding a level

Write a `level_<name>(ctx)` function in `gauntlet.py`, lay targets out with
`win.layout_targets([...])`, and call `ctx.run(level, win, target_spec,
expect_id)` per attempt. Register it in `LEVEL_FNS`, `LEVELS`, `THRESHOLDS`.
Target specs are plain dicts (`kind` = `dot` | `text` | `glyph`).
