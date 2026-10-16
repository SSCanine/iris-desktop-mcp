# Iris v2 Design

**Date:** 2026-04-27
**Status:** Implemented

---

## Overview

Iris is a desktop control MCP server for Windows. v1 was a single-file
FastMCP server that captured screens and drove mouse/keyboard at the pixel
level. It worked, but every interaction cost a screenshot and an LLM
reasoning round-trip.

v2 evolves Iris from a vision-only tool into a **hybrid sensorimotor system**
with three internal layers (Spatial, Vision, Semantic) coordinated by a
Resolver. The metaphor: v2 gives Iris a proper occipital lobe (vision + OCR
for "seeing" pixels) AND a frontal cortex (Win32 spatial reasoning + UIA
semantic queries for "knowing" structure without looking).

The architectural commitment: **backend-agnostic primitives**. Claude calls `find(token, "Start Recording")` and the resolver picks UIA, OCR, or vision-handoff transparently. UIA-friendly apps (Chrome, File Explorer, parts of OBS) become near-instant and zero-token. Apps without UIA fall back to OCR with no API change.

### Goals

- Window-aware capture (know which monitor each app lives on, capture only that window)
- Backend-transparent find/click primitives (UIA when supported, OCR fallback, vision handoff)
- Stateful focus tokens with revalidation, drift detection, popup follow
- Rich error handoffs to Claude when the occipital lobe can't resolve
- Aggressive performance targets (`focus()` < 30ms, `find()` via UIA < 20ms)
- Zero-config migration from v1 (existing tool calls keep working)
- Self-testable via `iris.self_test()` MCP tool

### Non-Goals (Phase 1)

- Recipes / named workflows (deferred to Phase 1.5)
- MIDI/OSC for apps like Resolume/Traktor (better served by a separate MCP)
- Voice triggers (Phase 2)
- Cloud OCR services (we go local: faster, private, no network dependency)
- Screen recording / video capture (still images only)
- Cross-machine control (out of scope: Iris drives the local desktop only)

---

## Section 1: Architecture

Iris v2 grows from a single ~360-line file into a small package. Same MCP entry point, same install footprint, same MCP config. Internally split so each piece does one thing.

### Three internal layers

```
┌─────────────────────────────────────────────────────┐
│  MCP Tool Surface (server.py)                       │
│  Backend-agnostic primitives Claude calls           │
│  focus(), see(), find(), click(), type(), launch()  │
└────────────────┬────────────────────────────────────┘
                 │
       ┌─────────┴─────────┐
       │  Resolver (core)  │  picks: UIA? OCR? Vision?
       └──────┬─────┬──────┘
              │     │
   ┌──────────┘     └──────────┐
   ▼                            ▼
┌────────────────┐    ┌────────────────────┐
│ Spatial layer  │    │ Semantic layer     │
│ (Win32)        │    │ (UIA via comtypes) │
│ - windows      │    │ - control tree     │
│ - geometry     │    │ - find by name     │
│ - monitors     │    │ - invoke patterns  │
│ - process IDs  │    │                    │
└────────────────┘    └────────────────────┘
        │
        ▼
┌────────────────┐
│ Vision layer   │
│ (mss + PIL)    │
│ - capture      │
│ - OCR (Tesser) │
│ - text-find    │
└────────────────┘
```

### The resolver picks the backend

When Claude calls `iris.find("Start Recording", in=obs_token)`:
1. Does this window expose UIA? Query the UIA tree, return matched control
2. If no UIA hit: OCR the captured pixels, return matched text bounds
3. If neither hit: return "not found" with a screenshot attached so Claude can decide

Claude only sees one tool. The backend it actually used shows up in the response metadata (`{"backend": "uia"}` or `{"backend": "ocr"}`) so we can debug, but Claude doesn't need to think about it.

### File layout

```
iris-mcp/
├── server.py             MCP entry, tool definitions, ~150 lines
├── iris/
│   ├── __init__.py
│   ├── tokens.py         Focus tokens + revalidation
│   ├── spatial.py        Win32 window enum, geometry, monitors
│   ├── vision.py         mss capture, PIL encoding, OCR
│   ├── semantic.py       UIA queries (uiautomation lib)
│   ├── resolver.py       The "which backend" router
│   ├── recipes.py        Optional: named workflows (Phase 1.5)
│   └── verify.py         Action verification helpers
├── tests/
│   ├── unit/             Pure-logic pytest, mocked Win32
│   ├── integration/      Real-Win32 against test harness
│   ├── fixtures/
│   │   └── iris_test_harness.py    Tkinter app with known controls
│   └── bench.py          Performance benchmarks
├── docs/specs/           This design doc lives here
├── apps.yaml             User-editable app registry
├── vendor/tesseract/     Bundled OCR binary
├── archive/              Old v1 server.py for rollback
├── logs/
├── requirements.txt
├── run.bat
└── README.md
```

The current single `server.py` is fine for 360 lines. Once we add focus tokens + UIA + OCR + recipes + verification, we'd be staring at 1500+ lines in one file. Splitting now keeps each module small enough to hold in a single mental context.

---

## Section 2: Components

### server.py, MCP tool surface

The only file Claude's MCP client sees. ~20 tools, organized by concept:

| Group | Tools |
|-------|-------|
| Discovery | `screen_info`, `list_windows(filter)`, `find_window(match)` |
| Focus | `focus(match, raise=False)` -> token, `release(token)`, `inspect(token)` |
| Sight | `see(token?)`, `see_full(token?)`, `screenshot(monitor?, region?)` |
| Locate | `find(token, target)`, `find_text(token, text)`, `find_control(token, role?, name?)` |
| Act | `click(token?, target?, x?, y?)`, `type_text`, `press_key`, `hotkey`, `scroll` |
| Lifecycle | `launch(app)`, `wait_for(token, target, timeout)`, `verify(token, expected)` |
| Diagnostics | `iris_status()`, `self_test()`, `discover(token)`, `suggest_alternatives(token, target)` |

server.py contains zero logic. Every tool is a 3-5 line wrapper that delegates to a layer module and formats the response.

### iris/tokens.py, Focus token model

```
FocusToken {
  id: uuid,
  hwnd: int,
  pid: int,
  exe_name: str,
  title_at_creation: str,
  monitor_index: int,
  bounds_at_creation: Rect,
  fingerprint: str,           # UIA tree shape hash
  parent_hwnd: int | None,    # set when token shifts to a popup
  created_at: timestamp
}
```

Stored in a process-local dict (one MCP process per session). Two key functions:
- `revalidate(token)` checks hwnd still exists and belongs to same pid. If hwnd died (window closed and reopened), tries to re-resolve via `pid + exe_name + title heuristics`. Returns None if truly gone.
- `inspect(token)` returns full state report: alive? bounds changed? moved monitor? anything occluding? any popups belonging to same pid?

Token revalidation cached for 250ms (one Claude turn typically fits in this window) to avoid re-probing.

### iris/spatial.py, Win32 window operations

The "where things are" layer. Pure geometry, no pixels.
- `enumerate_windows(filter)` uses `EnumWindows`, returns title + pid + exe + bounds + monitor + visible flag
- `match_window(spec)` flexible matching by title regex, exe name, pid, or hwnd
- `get_monitor_for_window(hwnd)` largest-area-overlap algorithm
- `is_occluded(hwnd)` Z-order check: anything above this window also intersecting its rect?
- `bring_to_front(hwnd)` AttachThreadInput dance to bypass the SetForegroundWindow restriction
- `find_popups_for(pid, since)` top-level windows belonging to same pid that appeared after a timestamp

### iris/vision.py, Capture + OCR

Refactored from current code, plus OCR.
- mss capture, but takes `bounds` arg instead of inline conditional logic
- OCR via Tesseract (`pytesseract`). Local, free, no network. Bundled binary under `vendor/tesseract/`
- `ocr_text(image)` returns list of `{text, bbox, confidence}` for every word/line
- `find_text_in_image(image, query, fuzzy=True)` searches OCR results, supports fuzzy matching
- Preprocessing: grayscale, threshold, upscale tiny text. Big accuracy gain on UI fonts
- LRU cache on OCR results keyed on (token, perceptual hash of pixels)

### iris/semantic.py, UIA queries

The "ask Windows directly" layer.
- Library: `uiautomation` Python package. Battle-tested wrapper around UIA, simpler than raw comtypes
- `query(hwnd, role?, name?, automation_id?)` returns list of `Control` objects with bounds + properties
- `invoke(control, action)` picks the right UIA pattern automatically (Button -> InvokePattern, Checkbox -> TogglePattern, Edit -> ValuePattern.SetValue)
- `walk_tree(hwnd, max_depth=8)` returns full hierarchical dump for `discover()`
- Graceful failure is mandatory: if a window has no usable UIA tree, return empty list, never crash. The resolver decides what to do
- Per-pid UIA-support cache: first call probes, result cached for pid lifetime

### iris/resolver.py, The router

The brain that picks the backend.

```
def find(token, target):
    1. revalidate token, abort if dead
    2. try semantic.query(token.hwnd, name=target)
       if hits: return with backend="uia"
    3. cache: this hwnd's pid doesn't support UIA for this query, don't retry next call
    4. capture window via vision, ocr_text, fuzzy-match target
       if hits: return with backend="ocr"
    5. return not_found + small screenshot for Claude to eyeball
       backend="vision_handoff"
       include nearest_matches via fuzzy search across both backends
```

### iris/verify.py, Action verification

Polling helpers used by `verify=True` and `wait_for(...)`.
- `wait_for_text(token, text, timeout=3000ms)` polls OCR every 100ms with backoff
- `wait_for_control(token, role/name, timeout)` polls UIA
- `wait_for_window(match, timeout)` polls spatial
- `wait_for_no_text(...)` useful for "Start Recording" -> "Stop Recording" transitions

### iris/recipes.py, Named workflows (Phase 1.5)

Deferred. Ships in a follow-up after we've used v2 and know what to recipify.
- `recipes.yaml` config: named multi-step workflows
- `iris.run_recipe(name)` tool

### apps.yaml, App registry

Outside the code, in config. User-editable.

```yaml
obs:
  launch: "C:\Program Files\obs-studio\bin\64bit\obs64.exe"
  match: { process: "obs64.exe", title_contains: "OBS" }
chrome:
  launch: "shell:start chrome"
  match: { process: "chrome.exe" }
explorer:
  launch: "explorer.exe"
  match: { process: "explorer.exe", class: "CabinetWClass" }
```

Iris reads it at startup. Adding a new app is a 5-line config change, no code.

### Library choices summary

| Need | Choice | Why |
|------|--------|-----|
| OCR engine | Tesseract (pytesseract) | Local, free, mature, bundled |
| UIA wrapper | uiautomation (PyPI) | Battle-tested, simpler than raw comtypes |
| Win32 | pywin32 + ctypes hybrid | pywin32 for compiled bindings, ctypes for niche APIs |
| Config | pyyaml | Standard, human-readable, comment-friendly |
| MCP | FastMCP (existing) | No change |


---

## Section 3: Data Flow

Three end-to-end traces show how the layers cooperate.

### Scenario A: "OBS, start recording" (happy path)

OBS is already running on a secondary monitor, windowed. The user asks "OBS, start recording." The LLM calls:

```
focus("obs", raise=False)
  -> resolver asks spatial.match_window({process: "obs64.exe", title_contains: "OBS"})
  -> spatial returns hwnd=0x4A12, pid=8842, bounds=(3840, 0, 1920, 1080), monitor=2
  -> tokens.create() returns FocusToken{ id: "tk_a1", ... }
  <- MCP returns { token: "tk_a1", monitor: 2, bounds: [3840,0,1920,1080], occluded: false }
```

Claude now knows exactly where OBS lives. No screenshot taken. ~2ms.

```
find(token="tk_a1", target="Start Recording")
  -> resolver: tokens.revalidate(tk_a1), still alive
  -> resolver: try semantic first
  -> semantic.query(hwnd=0x4A12, name="Start Recording")
  -> UIA tree for OBS Qt window returns Button{ name: "Start Recording", bounds: (4252, 88, 80, 28) }
  <- MCP returns { hits: [{ name: "Start Recording", bbox: [4252,88,80,28] }], backend: "uia" }
```

UIA hit. Zero pixels sent to Claude, ~5ms total.

```
click(token="tk_a1", target="Start Recording", verify=True)
  -> resolver: same find as above (cached for 1s)
  -> click center of bbox via pyautogui.click(4292, 102)
  -> because verify=True: verify.wait_for_text(tk_a1, "Stop Recording", timeout=2000ms)
  -> polls UIA, after ~400ms finds Button "Stop Recording" where "Start Recording" used to be
  <- MCP returns { ok: true, clicked: [4292, 102], verified: true, backend: "uia" }
```

Whole sequence: 3 tool calls, ~500ms wall clock, zero pixels sent to Claude.

### Scenario B: "Open OBS settings, change scene transition to Fade" (with popup)

OBS is open. Settings is a modal dialog, a separate top-level window owned by the same pid.

```
focus("obs") -> token tk_b1 -> OBS main window
click(tk_b1, "Settings", verify=True, follow_popup=True)
  -> UIA finds and clicks Settings button
  -> because follow_popup=True: spatial.find_popups_for(pid=8842, since=t0)
  -> after 200ms, a new top-level window appears: hwnd=0x5B23, title="Settings"
  -> tokens.update(tk_b1) -> focus auto-shifts to the popup, original window saved as parent
  <- MCP returns { ok: true, popup_detected: true, new_focus: { title: "Settings", monitor: 2 } }
```

Claude now knows focus moved. Same `tk_b1` token, but pointing at the popup. Original window saved as `parent_hwnd`.

```
find(tk_b1, "Scene Transitions")
  -> semantic.query on the Settings dialog finds the section
click(tk_b1, "Scene Transitions tab")
find(tk_b1, "Type")
  -> semantic returns ComboBox "Type", current value "Cut"
click(tk_b1, target="Type", action="open")     UIA dropdown invocation
click(tk_b1, "Fade")
click(tk_b1, "OK")
  -> click triggers dialog dismiss
  -> spatial.find_popups_for() returns no new popup AND popup window closed
  -> tokens.update(tk_b1) -> focus snaps back to parent_hwnd (OBS main)
  <- MCP returns { ok: true, popup_closed: true, focus_restored: "OBS Studio" }
```

The "popup follow" flag is the user-controlled escape hatch. Default `follow_popup=False`, Iris reports the popup so Claude decides. With it on, focus auto-shifts and reverts when the dialog dismisses.

### Scenario C: UIA-blind app (the OCR fallback path)

```
find(tk_c1, "Some Button")
  -> semantic.query(hwnd) returns [] (or unsupported error)
  -> resolver caches: pid 9921 doesn't support UIA queries by name
  -> resolver: try OCR
  -> vision.see(token) screenshot of just the window's bounds
  -> vision.ocr_text(image) list of words with bboxes
  -> vision.find_text_in_image(words, "Some Button", fuzzy=True)
  -> match found at bbox (in window-local coords)
  -> resolver translates window-local to screen-absolute by adding window bounds origin
  <- MCP returns { hits: [{ text: "Some Button", bbox: [...] }], backend: "ocr" }
```

Same `find()` call. Same return shape. Backend swapped transparently.

### Three properties worth calling out

1. **Tokens are cheap and revalidated.** Every call that takes a token starts with `revalidate()`. If OBS crashed and reopened with a new hwnd, we either re-resolve via pid+title (best case) or return a "token expired" error (clean failure). No silent stale clicks.

2. **Coordinate translation lives in the resolver, never in Claude.** UIA returns screen-absolute coords; OCR returns window-local. The resolver normalizes to screen-absolute before returning. Claude never has to add offsets.

3. **`see()` on a token is window-local.** Calling `see(tk_a1)` returns a screenshot cropped to OBS's bounds. Big token savings vs full-desktop capture, no noise from other monitors / apps.


---

## Section 4: Error Handling and Recalibration

The mental model: Iris's occipital lobe tries to resolve fast. If it can't, it hands a rich situation report up to Claude's frontal cortex to reason about. Failures aren't dead ends, they're handoffs with full context.

### Failure taxonomy

Every failure returns a structured response. No exceptions thrown to the MCP layer except in genuinely impossible situations (Win32 unavailable, etc.).

| Failure | Trigger | Response shape |
|---------|---------|----------------|
| Token died | hwnd no longer valid | Auto-attempt recovery via `pid + exe + title` heuristic. Returns repaired token or clean expiry |
| Window minimized | bounds = (-32000, -32000) sentinel | Returns `captured: false` plus options: raise / wait / ignore |
| Window occluded | Z-order check finds window on top | Returns capture anyway plus warning, or auto-raise if `raise=True` |
| Target not found | UIA + OCR both empty | Returns nearest matches plus small screenshot for Claude |
| Ambiguous match | Multiple hits | Returns all with context paths, asks Claude to disambiguate |
| Click verified-fail | `verify=True`, expected state didn't appear | Returns what did change, lets Claude diagnose |
| App not running | `focus("obs")` and OBS not open | Returns `not_running`, lists similar processes, suggests `launch()` |
| Permission denied | UAC prompt, secure desktop | Returns honest "blocked by Windows", no silent retry loop |

### Recalibration tools

When Claude hits a "not found" or a verified-fail, three tools enable recovery.

**1. Auto-included nearest-matches**

Every "not found" response includes fuzzy alternatives from both backends:

```json
find(tk, "Start Recording") -> {
  "found": false,
  "target": "Start Recording",
  "backends_tried": ["uia", "ocr"],
  "nearest_matches": [
    { "text": "Start Stream/Record", "similarity": 0.65, "backend": "ocr",
      "bbox": [4252, 88, 110, 28] },
    { "text": "Recording", "similarity": 0.71, "backend": "uia",
      "bbox": [4280, 110, 80, 22] }
  ],
  "screenshot": "<small window jpeg>",
  "hint": "No exact match. Window may have changed structure since the target was last seen."
}
```

Claude sees the screenshot, recognizes "Start Stream/Record" exists (OBS combined the buttons in an update), and calls `click(tk, target="Start Stream/Record")` next. Self-healing.

**2. `discover(token)` ground-truth dump**

When fuzzy alternatives aren't enough, call `discover()` for the full picture:

```json
discover(tk) -> {
  "window": { title, bounds, monitor, occluded, minimized },
  "uia_tree": [... full hierarchical tree, every named control with bounds],
  "ocr_text": [... every line of text with bbox and confidence],
  "screenshot": "<full window jpeg, optimized>",
  "fingerprint": "sha256-of-uia-tree-shape"
}
```

This is "tell me everything." Costs more tokens than `find()` but it's how Claude recovers from total disorientation. The `fingerprint` is useful for drift detection (see below).

**3. `suggest_alternatives(token, target)` explicit fuzzy search**

Runs UIA and OCR fuzzy search at lower thresholds (0.4+), returns top 10 candidates ranked by similarity plus visual prominence (size/centrality). Lets Claude peek without committing to a different target.

### Drift detection (proactive)

When `focus()` is called, Iris snapshots a `fingerprint` of the window:
- UIA tree shape hash (structure, not values, so text content changing doesn't trigger drift)
- Bounds + monitor
- Top-level button names (set, sorted)

On every later token use, this is cheap to recompute. If the fingerprint changes radically (>50% of the structure shifted), the response includes:

```json
{
  ...normal_payload...,
  "drift_detected": true,
  "drift_summary": {
    "buttons_added": ["Start Stream/Record"],
    "buttons_removed": ["Start Streaming", "Start Recording"],
    "bounds_changed": false
  }
}
```

This proactively warns Claude that the app shape changed mid-session, usually meaning the user clicked into a different mode, the app got updated/relaunched, or a major panel toggled.

### The recovery loop in practice

```
1. Call fails -> response has nearest_matches -> try the obvious one
   ! still failing
2. Call discover(token) -> see full state -> reason about what changed
   ! still failing
3. Hand back to user, "OBS looks structurally different than I expected, here's the
   screenshot, can you tell me what changed?"
```

Step 3 is the honest fail. No silent retry storms, no infinite loops. Three strikes and we ask the human.

### Logging

Every failure logs to `<repo>/logs/iris.log` with:
- Timestamp, token id, target, backends tried, why each failed
- When `IRIS_DEBUG=1`: dump UIA tree to a sibling debug file


---

## Section 5: Testing

Iris drives the real desktop, so we can't run "did the click land?" tests in headless CI. Testing splits into five layers, each trading some realism for some automation.

### Layer 1: Unit tests (pytest, runs anywhere)

Pure logic with no Windows dependency. Mocked everything.

| Module | What gets tested |
|--------|------------------|
| `tokens.py` | Token creation, revalidation logic, repair via pid+exe+title heuristics, fingerprint hashing |
| `vision.py` | OCR text-matching, fuzzy similarity scoring, image preprocessing |
| `resolver.py` | Backend selection tree with all three layers mocked. UIA-first, OCR fallback, vision-handoff order |
| `spatial.py` | Monitor-overlap algorithm with synthetic window lists |
| `semantic.py` | UIA tree parsing with mock COM responses |
| `verify.py` | Polling timeouts, backoff timing, condition matching |

Goal: ~80% coverage of pure logic. Fast. Run on any machine including CI.

### Layer 2: Integration tests (Windows-only, real Win32)

A small test harness app ships with Iris at `tests/fixtures/iris_test_harness.py`. It's a Tkinter window with known controls at known positions:
- Buttons named "Click Me", "Spawn Dialog", "Move Window", "Minimize Self"
- A text input
- A multi-line label that changes content (for OCR fuzzy testing)
- A button that creates a popup (for popup-follow testing)
- A button that simulates an "app update" by remapping its own controls (for drift testing)

Pytest spawns it, runs Iris against it, kills it. Tests cover:
- Focus + token revalidation: focus the harness, kill it, verify token expires cleanly
- Find + click via UIA: Tkinter exposes UIA, semantic queries should work end-to-end
- Find + click via OCR fallback: temporarily disable UIA backend in resolver, verify OCR finds the same controls
- Popup detection: click "Spawn Dialog", verify a new top-level window is detected
- Drift detection: click "Simulate Update", verify fingerprint changes, drift_detected fires
- Multi-monitor: if `screen_info.monitor_count > 1`, move harness window across monitors and verify spatial layer tracks it correctly

### Layer 3: Self-test MCP tool (`iris.self_test()`)

Same battery as integration tests, exposed as a tool Claude can invoke. Returns a structured report:

```json
{
  "passed": 14,
  "failed": 1,
  "skipped": 2,
  "duration_ms": 4280,
  "results": [
    { "name": "spatial.enumerate_windows", "status": "pass", "ms": 12 },
    { "name": "semantic.uia_query_tkinter", "status": "pass", "ms": 230 },
    { "name": "vision.ocr_finds_button_text", "status": "pass", "ms": 410 },
    { "name": "resolver.uia_first_then_ocr", "status": "pass", "ms": 180 },
    { "name": "tokens.repair_after_close", "status": "fail",
      "ms": 1200, "reason": "Window relaunched faster than detection window" }
  ]
}
```

Run `iris.self_test()` after install or upgrade. Iris confirms it works, or tells exactly what broke.

### Layer 4: Manual e2e runbook

`docs/test-runbook.md` (planned) with a checklist for things only a real human can verify:

- [ ] Open OBS. Run `focus + find Start Recording + click + verify`. Recording started?
- [ ] Open Chrome. `focus + navigate to a URL + verify page loaded`.
- [ ] Open File Explorer. `focus + navigate to a folder + verify`.
- [ ] With OBS on monitor 2 and Chrome on monitor 1, `focus("obs")` returns monitor 2.
- [ ] Drag OBS to monitor 1 mid-session. Next `inspect(token)` shows monitor=1.
- [ ] Trigger drift: Open OBS Settings popup. Verify popup is detected and focus shifts.

Run before tagging a new Iris version. ~10 minutes manual. Findings logged in `tasks/iris-test-log.md`.

### Layer 5: Performance benchmarks

`tests/bench.py` measures latency of common ops. Updated targets reflecting "fastest possible":

| Operation | Target | How we hit it |
|-----------|--------|---------------|
| `focus()` (UIA available) | < 30ms | Cache `uiautomation.GetRootControl()`. Single Win32 enum + property read |
| `find()` via UIA | < 20ms | Use `automation_id` when available. Per-pid UIA-support cache |
| `find()` via OCR (window-cropped) | < 200ms | Crop to window bounds. Pre-warm Tesseract. LRU cache on perceptual hash |
| `see()` of 1920x1080 window | < 50ms | Persistent `mss` instance. JPEG q=60 optimize=False |
| `discover()` full dump | < 500ms | UIA tree walk + OCR + screenshot in parallel via asyncio |
| Token `revalidate()` | < 2ms | 250ms result cache (fits one Claude turn) |

Results append to `logs/bench.jsonl`. Future regressions are visible as deltas.

### Key performance optimizations

1. **Pre-warm Tesseract at server startup.** First OCR call would otherwise be 1-2 seconds (model load). Dummy OCR pass during `mcp.run()` startup so first real call is sub-300ms.
2. **UIA support cache, per-pid.** First call probes UIA. Cached for pid lifetime.
3. **OCR result cache, per-token.** Cache keyed on perceptual hash of pixels. Static screen, repeat calls return instantly.
4. **Parallel discover.** UIA walk + OCR + screenshot via `asyncio.gather`. Wall-clock is dominated by slowest, not sum.
5. **Window-cropped capture is the default.** OCR a 1920x1080 cropped window in 200ms vs full multi-monitor capture in ~1.5s.
6. **Win32 over ctypes where possible.** `pywin32` has compiled C bindings. Avoid the `ctypes` overhead.
7. **No animation delays anywhere.** `pyautogui.PAUSE = 0`, `FAILSAFE = False` (already done).

### CI strategy

- **GitHub Actions / pre-commit:** Layer 1 only (pure unit tests). Linting via ruff. Type-checking via mypy. Fast.
- **Local Windows pre-merge:** Layers 1+2+3+5. Harness-based tests + self-test + benchmarks.
- **Layer 4 (manual runbook):** Run on version-bump only.


---

## Section 6: Rollout, Migration and Performance

### Backwards compatibility

Zero-config migration. The old `server.py` path stays the same. Every existing tool keeps working.

| Old tool | v2 status |
|----------|-----------|
| `screenshot(monitor, region, quality)` | Kept as-is. Internally delegates to `vision.capture()` |
| `screenshot_full(...)` | Kept as-is |
| `screenshot_window(quality)` | Kept as-is. Internally: `focus(foreground) + see(token)` |
| `screen_info()` | Kept as-is, slightly enriched response (adds primary monitor index) |
| `mouse_pos / move / click / drag / scroll` | Kept as-is |
| `type_text / press_key / hotkey` | Kept as-is |

All new tools (`focus`, `find`, `discover`, `launch`, `self_test`, etc.) are added alongside. No breakage.

### Dependencies

**Python (requirements.txt additions):**
- `uiautomation` (UIA wrapper), pure Python, pip-installable
- `pytesseract` (OCR Python binding), pip-installable
- `pyyaml` (apps.yaml plus future recipes.yaml)
- `pillow-simd` (faster Pillow drop-in for image preprocessing)
- Existing: `mss`, `pyautogui`, `pywin32`, `mcp`

**Native (one-time install for OCR):**
Tesseract binary located via `tesseract_bootstrap.py` which checks `vendor/tesseract/`, then `C:\Program Files\Tesseract-OCR\`, then PATH. The repo ships without the binary (users install via `winget install UB-Mannheim.TesseractOCR` or place it in `vendor/`).

### Phased rollout

**Phase 1: v2.0 (the big build)**

Everything in this design except recipes:
- Token model + revalidation + drift detection
- Spatial layer (window enum, monitor mapping, occlusion, popup detection)
- Vision layer (refactored capture, OCR, fuzzy text matching)
- Semantic layer (UIA queries, invocation patterns)
- Resolver (backend routing, transparent fallback)
- All 20 MCP tools
- App launching via `apps.yaml`
- `self_test()` and the Tkinter test harness
- Performance targets above
- Full backwards compat with v1

**Phase 1.5: v2.1 (recipes)**

Once v2.0 has been used for a couple weeks:
- `recipes.yaml` config
- `iris.run_recipe(name)` tool
- Curated initial set: `obs.start_recording`, `obs.start_streaming`, `chrome.new_tab`, etc.

**Phase 2: v2.2+ (driven by real usage)**

Reserved for things we discover we want after living with v2:
- Voice trigger integration (with GoXLR)
- Cross-monitor "what's on each screen" awareness summary
- Event subscriptions ("notify me when OBS Recording state changes")
- A separate MCP for MIDI/OSC

Phase 2 only happens if we feel the pain. No speculative building.

### Migration mechanics (Phase 1 ship)

1. Build v2 using the new package layout. Old `server.py` keeps running until v2 is ready.
2. When v2 is feature-complete and self-test passes and manual runbook passes:
   - Move old `server.py` to `archive/server_v1_2026-04-27.py` (preserved for rollback)
   - Activate new package `server.py` (the thin MCP entry point)
   - Restart Claude Code so MCP picks up the new server (same path, same command, no config change)
3. Run `iris.self_test()` to confirm everything works
4. If anything breaks: revert is a single-file swap. Keep v1 archived for at least 30 days.

### What's explicitly out of scope (so we don't drift)

- Recipes (Phase 1.5)
- MIDI/OSC (separate MCP, future)
- Voice triggers (Phase 2)
- Cloud OCR services (going local for speed, privacy, no network dependency)
- Screen recording / video capture (still images only)
- Cross-machine control (out of scope: Iris drives the local desktop only)

---

## Implementation order (preview, full plan in writing-plans output)

1. Package skeleton + dependency install + bundled Tesseract
2. `tokens.py` plus unit tests
3. `spatial.py` plus unit tests (mocked) plus harness-based integration test
4. `vision.py` capture refactor plus OCR plus unit tests
5. `semantic.py` UIA plus unit tests plus harness integration
6. `resolver.py` plus unit tests
7. `verify.py` plus unit tests
8. `iris_test_harness.py` Tkinter fixture
9. `server.py` rewrite as thin MCP entry with backwards-compat shims
10. `self_test()` MCP tool wiring up Layer 2 tests
11. Performance benchmarks
12. Manual e2e runbook
13. Cutover (move v1 to archive, restart MCP, run self_test)

---

*Design doc complete. Self-review next, then writing-plans skill for ordered implementation milestones.*
