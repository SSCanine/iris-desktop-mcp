# Changelog

All notable changes to Iris will be documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to semantic versioning.

## [Unreleased]

### Added (2026-10-15 setup window)
- **Setup window** (`setup.bat`, `python -m iris.setup_gui`, `iris-mcp-setup`):
  checks the Python interpreter and its dependencies, finds or installs
  Tesseract, sets options, and connects Iris to Claude Code, Claude Desktop
  and Codex with one click (status per client, copy config, remove). Runs
  on the standard library, so it works on a fresh clone before `pip install`.
- **`python -m iris.configure`**: the same registration from the command
  line (`--client`, `--print`, `--remove`, `--guide`). Config files are
  backed up before every write and Codex TOML is validated before saving.
- **MCP server instructions**: the server now sends a short usage guide
  (`iris/agent_guide.md`) to every client.
- **`IRIS_TESSERACT`** env var points Iris at a specific `tesseract.exe`.

### Added (2026-09-29 gestures)
- **`drag`**: press, hold, glide, release between any two endpoints (text
  target, color/image target, Set-of-Mark number, or x/y), or a relative
  dx/dy. The end may be in another window. Hold time, glide speed, modifier
  keys held throughout (e.g. shift for fine control, ctrl to copy on drop),
  and a before/after pixel check reported as `changed`.
- **`knob`**: turn knobs / move faders and sliders by vertical drag,
  horizontal drag or wheel notches, with a `fine` modifier mode.
- **`hold`**: long-press without moving.
- All three are `do()` steps. Gestures use the real mouse (drags cannot be
  delivered as posted messages); the cursor is restored and the button and
  modifiers are always released, even when a gesture fails midway.
- Verified on Windows Paint on a 150% monitor: drag drew a 300 px line (56/56
  sampled points changed); knob moved the Zoom slider 100% -> 334% (read via
  UIA RangeValue).

### Added (2026-09-29 speed)
- **`do(steps, token)`**: run a whole sequence (focus, click, set, type,
  key, hotkey, scroll, wait, wait_for, find, marks) in ONE tool call, stop
  at the first failure, compact per-step results. For an agent each tool
  round trip costs far more than Iris itself; measured: focus + 2 finds +
  wait in 245 ms total through MCP.
- `set` step: quiet text entry via UIA ValuePattern (no focus, no keys).
  Keyboard steps in a token batch activate the token's window first and
  say so, instead of typing into whatever the user has focused (quiet
  clicks deliberately do not focus).

### Changed (2026-09-29 speed)
- Filtered UIA queries (every find/click by name) run as one cached
  `FindAllBuildCache` evaluated inside the provider instead of a Python
  walk with 7 COM calls per node. Measured: File Explorer 2.35 s -> 0.76 s,
  NordVPN 0.79 s -> 0.18 s. It also reaches deep Electron trees the
  depth-limited walk missed: VS Code `find("Explorer")` went from a 3.8 s
  OCR fallback to a 32 ms UIA hit. Unfiltered enumeration (marks) keeps the
  walk for now: the two return different element sets there.
  `IRIS_UIA_WALK=1` forces the old walk.
- OCR on captures over 1.2 MP runs in parallel horizontal bands (one
  Tesseract process each, overlapping, deduped): VS Code 2571x1403
  1.46 s -> 0.66 s, Chrome 2062x1118 0.72 s -> 0.40 s; small windows
  unchanged. `IRIS_OCR_BAND_MIN_PIXELS` tunes the threshold.

### Added (2026-09-06 Set-of-Mark + quiet clicks)
- **Set-of-Mark.** `see(token, marks=True)` / `see_full(..., marks=True)`
  number every element Iris can enumerate (UIA controls in cyan, OCR text
  runs in magenta), draw the numbers on the image and return the legend
  in `meta.marks`. `list_marks(token)` returns the legend without an
  image. `click(token, mark=N)` acts on a number: UIA Invoke when the
  element has it, else a quiet click at its center. No coordinate
  estimation anywhere. Stale sets (window changed) are flagged.
- **Quiet clicks.** `click` delivers by UIA pattern first, then by posting
  mouse messages straight to the target window at exact client
  coordinates (`click_method: "message"`): the cursor never moves, focus
  never changes, and a covered window still receives it. The real mouse
  is used only for `click(x, y)` without a token or `click_method="mouse"`
  (which now restores the cursor afterwards). `click_method` accepts
  `auto` (default), `quiet`, `mouse`, `invoke`. Apps that read raw input
  (games, some DirectX overlays) need `mouse`; verify clicks that matter.
- Quiet delivery is decided by the target's window class (`iris/quiet.py`):
  classic Win32 controls (Button, Edit, ListView, dialogs, menus) get the
  posted message outright; Tk, UWP CoreWindow and Java AWT read the
  physical mouse and ignore posted messages, so they go straight to the
  real mouse; unknown classes get the message plus a repaint check
  around the click point, with automatic mouse fallback
  (`click_method: "mouse_after_quiet_no_reaction"`). Results carry
  `target_class`, `reaction` and the delivery used. Measured: Character
  Map's Select button via message with zero SendInput calls; the Tk
  gauntlet produced no receipts for 52/53 posted clicks.
- Gauntlet: `marks` level (click tiny labels by number), a per-attempt
  mouse-used assertion (SendInput counter, so the user moving the mouse
  does not count), `--click-method` to compare delivery paths, `--monitor`
  placement, and `--harness win32`: a pure Win32 (ctypes + GDI) harness that
  honors posted messages, so the quiet path is measured on a real
  message-driven window. Test windows are created with WS_EX_NOACTIVATE
  and shown without activation; z-order raises never take focus.
- OCR->UIA widget upgrade rejects container-sized controls (on a childless
  window `ControlFromPoint` returns the window itself).

### Added (2026-09-06 accuracy overhaul)
- **Pixel backend** (`iris/pixel.py`). `find`/`click` accept
  `color:#RRGGBB[~tol][@min-max]` (connected-component color search) and
  `image:<png>[~thr]` (template match with DPI-scale fallback). Targets
  with no text and no UIA node (dots, icons, canvas widgets) are now
  findable. Backend name `pixel`.
- **Screenshot truth.** `see`, `see_full`, `screenshot`, `screenshot_full`,
  `screenshot_window` return `[image, meta]`; `meta` carries `origin`,
  `captured_size`, `image_size`, `scale` and the formula to map an image
  point back to a screen pixel. `grid=True` draws a labeled grid in SCREEN
  coordinates. New `zoom(x, y, size, magnify)` tool: full-res magnified crop
  with a fine grid and crosshair for honing in on tiny targets.
- **UIA ranking.** Name matches are scored by exactness (exact > word >
  substring), role (Button/MenuItem/Tab > ListItem > Text > containers >
  the window itself), visibility and invokability. Up to 100 candidates are
  considered instead of the first 10 in tree order. `prefer_roles=[...]`
  on `find`/`click` steers text-heavy apps.
- **Telemetry.** Every `find` and `click` (refusals included) appends one
  JSON line to `<log_dir>/events.jsonl`. `events_tail(n)` tool reads it;
  `iris_status` reports the path. Disable with `IRIS_TELEMETRY=0`.
- **Gauntlet** (`python -m iris.bench.gauntlet`). Multi-window, tiny-target,
  timed bench: 7 to 11 pt text, 24 to 2 px color squares, near-shade
  decoys, same target in three windows, an occluding window, whack-a-mole
  with a time budget, and 12 to 16 px glyph templates. Drives the real
  server `click()`; each window reports the exact pixel hit. Waits for
  user idle before taking the mouse.
- `click` results echo `resolved` (what find picked, how many candidates)
  and `ambiguous: true` when more than one hit matched.
- **Monitor topology discovery** (`spatial.list_monitor_info`). Monitors are
  enumerated through Win32 with per-monitor DPI scale, work area, device
  name and primary flag, ordered primary first, then top-to-bottom,
  left-to-right. The cache refreshes at once when a display is plugged or
  unplugged. `screen_info` exposes it all plus the virtual desktop;
  `see`/`screenshot(monitor=k)` resolve `k` through the same list instead
  of mss's index order. Goal: Iris works unchanged on any machine.

### Changed (2026-09-06)
- OCR runs Tesseract with `--psm 11` (sparse text) instead of the document
  default, and retries at 2x upscale when the native pass finds nothing
  (bounded by capture size). Multi-word joins only combine words on the
  same OCR line. Override the mode with `IRIS_OCR_PSM`.
- OCR cache key is now an exact digest of the capture pixels (+ scale)
  instead of an 8x8 perceptual hash, which could not see a dot move or a
  label flip and returned stale words.
- `click(token, target)` raises the token's window before a geometric click
  when it is occluded (find sees through occlusion via PrintWindow; the
  click otherwise landed on the covering window).
- `click_outside_window` refusals include the window center and what was
  resolved.

### Fixed (2026-09-06)
- `server.py` now explains on stderr when it is started by an interpreter
  without Iris's dependencies (the usual cause of "failed to launch":
  a bare `python` in the MCP config resolving to the Windows Store stub for
  GUI-launched Claude sessions) and the README shows the absolute-interpreter
  registration.
- OCR->UIA widget upgrade could adopt a control from a DIFFERENT window
  (`ControlFromPoint` hit-tests the whole desktop). Foreign controls are
  now rejected.
- `try_pattern_click` stopped at the first pattern that raised; it now tries
  the remaining patterns.
- SendInput coordinate normalization truncated instead of rounding (up to
  1 px bias toward the top-left).
- Source-checkout log directory detection was dead code; logs went to
  AppData. Logs now land in `<repo>/logs` when running from a checkout.
- `see(token)` fallback capture used creation-time bounds; now live bounds.
- Fresh installs pulled `mcp` 2.x, which removed `mcp.server.fastmcp`, so
  the server could not start anywhere but on a machine that already had 1.x.
  Dependencies now pin `mcp>=1.10,<2`; CI caught it once a unit test
  imported the server module.
- `screen_info` reported `primary_index: 0` unconditionally; the first
  enumerated monitor is often not the primary (it was not on the reference
  machine). Primary is now detected from MONITORINFOF_PRIMARY.
- `iris_status().dpi_mode` said `system_aware` when an earlier importer had
  already set Per-Monitor V2; it now reads back the effective context.

### Added
- Per-Monitor V2 DPI awareness is set at server startup so synthesized
  clicks land on the correct pixel across mixed-DPI multi-monitor setups.
  Mode visible via `iris_status().dpi_mode`.
- OCR-to-UIA widget upgrade: after OCR finds text, the resolver asks UIA
  `ControlFromPoint` and substitutes the enclosing clickable widget's
  bounds. Buttons with offset labels and list rows now click the widget
  center rather than the glyphs. Hits carry `upgraded_to_uia: true`.
- UIA Invoke fast path: when the resolved hit is a UIA control exposing
  `Invoke`/`Toggle`/`SelectionItem.Select`/`ExpandCollapse.Expand`, the
  click is delivered without mouse motion. Result includes
  `click_method: "uia_pattern"`. Disable per-call with `prefer_invoke=False`.
- `is_invoke_trusted` denylist for known-broken UIA Invoke classes
  (currently `TkTopLevel`, `TkChild`). Iris falls back to a geometric
  click while still using the upgraded widget bounds.
- SendInput-backed mouse primitives in `iris/input.py`. Atomic delivery
  so cursor move and button down/up cannot be interrupted by other
  synthesized input.
- Live window bounds: `spatial.current_bounds(hwnd)` and
  `FocusToken.current_bounds()`. OCR coord translation reads these at
  find time, not at token creation time.
- Pre-flight click clamp: `click(token, target)` refuses with
  `error: click_outside_window` when the resolved coords would land
  outside the token's current window. Off-monitor clicks refused with
  `error: click_off_screen`.
- Real-time accuracy bench at `iris.bench.runner`. Spawns an instrumented
  Tk harness, drives find+click across baseline + drag + resize +
  per-monitor + raise scenarios, measures actual landed-pixel error.
- System integration tools: clipboard get/set, list/find/kill processes
  (with force/confirm guards), Windows toast notifications, window state
  (minimize/maximize/restore/close), registry read/list/write/delete
  (writes require `confirm=True`).
- Config externalized: apps.yaml is searched via `$IRIS_APPS`, CWD, user
  config dir (`platformdirs`), then bundled example. Logs likewise
  searched via `$IRIS_LOG_DIR`, user log dir, or repo `logs/` when
  running from a source checkout.

### Changed
- `mouse_click`, `mouse_move`, `mouse_drag`, `mouse_scroll`, `mouse_pos`
  now go through `iris/input.py` (SendInput) instead of pyautogui's
  legacy `mouse_event` path. pyautogui is still used for keyboard
  primitives.

### Notes
- Public test posture: 172 unit + integration tests passing, 15 self
  tests green, bench at 100% correct-button rate on a 3-monitor mixed-DPI
  reference machine.
