# Iris

[![CI](https://github.com/SSCanine/iris-desktop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SSCanine/iris-desktop-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12-blue.svg)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%20|%2011-0078D4.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7C3AED.svg)](https://modelcontextprotocol.io/)

**High-precision Windows desktop control for AI agents.**
A Model Context Protocol (MCP) server that lets LLMs see, find, and click
real UI on Windows, with sub-pixel accuracy on mixed-DPI multi-monitor
setups, and without requiring the target app to expose an accessibility
tree.

Iris uses four backends behind one API:

1. **Spatial** (Win32) for window enumeration, focus, occlusion, bounds,
   and live monitor topology (any count, mixed DPI, hot-plug).
2. **Semantic** (UIA) for accessibility-aware element queries and pattern
   invocation.
3. **Vision** (`mss` + Tesseract OCR, exact-digest cache) for apps that
   draw their own widgets and skip the accessibility tree.
4. **Pixel** (numpy + OpenCV) for targets with no text at all: `color:#hex`
   blobs and `image:` templates.

A resolver routes between them: pixel grammar, else UIA (ranked), then OCR
(native, then 2x), then a "vision handoff" that returns a screenshot so the
LLM can decide directly. The result is
that Iris works on apps that pure-UIA tools cannot reach: Tkinter, custom Qt
widgets, OBS dock widgets, Resolume panels, game launchers, anything visible.

## Why Iris

A live, measurable accuracy benchmark on a mixed-DPI multi-monitor setup
(four monitors at 100% / 100% / 150% / 175% at the time of writing):

```
total attempts        : 59        (4 monitors at 100 / 100 / 150 / 175 %)
find rate             : 100.0%
correct-button rate   : 100.0%
miss distance  mean   : 0.02 px
miss distance  p95    : 0.0 px
miss distance  max    : 1.0 px
```

And the Gauntlet (`python -m iris.bench.gauntlet`), which is the hard part:
three windows, 7 to 11 pt text, color squares down to 2 px, near-shade
decoys, the same target in several windows, an occluding window, a
whack-a-mole target and glyph templates, all clicked through the real
server `click()`:

```
level            ok/n    rate  miss p95  ms/att
tiny_text       8/8    100.0%       2.0     249
color_dots      8/8    100.0%       0.0      43
decoys          2/2    100.0%       0.0      40
multi_window    9/9    100.0%       0.0      76
overlap         2/2    100.0%       0.0     206
whack          80/80   100.0%       2.7     103   (3.9 hits/s)
template        6/6    100.0%       0.0      47
```

And the same Gauntlet on a message-driven Win32 window with QUIET delivery
(no cursor movement, no focus change, 2026-09-06): 93/94, all levels pass,
SendInput never called. Transcript: `assets/gauntlet-quiet-output.txt`.

Run them yourself (see below). Both spawn instrumented windows, drive
Iris's find+click against them, and read back exact pixel-level hit
locations. No vendor claims without proof.

## How Iris differs from other Windows MCP servers

| Capability | Iris | Pure-UIA servers |
|---|---|---|
| Works on Tk / custom Qt / canvas apps | yes (OCR fallback) | no |
| Per-Monitor V2 DPI awareness | yes | usually not |
| UIA `Invoke` fast path (no mouse motion) | yes | no |
| OCR→UIA widget upgrade (text bbox -> widget bbox) | yes | n/a |
| Pre-flight off-screen / off-window clamp | yes | no |
| `SendInput` for synthesized clicks | yes | usually `mouse_event` |
| Closed-loop click verification (UIA drift / text appears) | yes | no |
| Live accuracy bench | yes | rarely |
| YAML recipe engine | yes | no |
| Tk-widget `Invoke` silent-failure denylist | yes | no |
| Set-of-Mark numbered overlays + click-by-number | yes | rarely |
| Quiet clicks (no cursor movement, no focus change) | yes | no |

## Quick start

Requires Windows 10/11 and Python 3.10+ ([python.org](https://www.python.org/downloads/);
tick "Add python.exe to PATH" during install). Tesseract OCR is optional but
recommended; the setup window can install it for you.

```powershell
git clone https://github.com/SSCanine/iris-desktop-mcp.git
cd iris-desktop-mcp
.\setup.bat
```

`setup.bat` opens the **Iris setup** window. Work through it top to bottom:

1. **Python** picks the interpreter Iris runs under and shows any missing
   packages. *Install dependencies* runs `pip install -r requirements.txt`
   with that exact interpreter.
2. **OCR (Tesseract)** auto-detects Tesseract, lets you point at a custom
   `tesseract.exe`, or installs it with winget.
3. **Options** sets the log folder, an `apps.yaml` of launch shortcuts, the
   local event log, and OCR modes. Blank fields use the defaults.
4. **Connect your AI clients** registers Iris with **Claude Code**,
   **Claude Desktop**, and **Codex** (CLI, desktop app, and IDE extension
   share one config). Installed clients are ticked for you; press
   *Connect selected*. Each row shows `connected`, `not connected`, or
   `connected (different settings)`, and has *Copy config* (paste it by
   hand) and *Remove*. Optionally add the short Iris usage guide to your
   `CLAUDE.md` / `AGENTS.md` (the server also sends it to every client as MCP
   instructions).
5. **Test** runs the doctor and the self-test, and opens the log folder.

Restart the client afterwards (new Claude Code / Codex session; quit Claude
Desktop from the tray and reopen it). Every config file the setup touches is
backed up first as `<file>.iris-backup-<timestamp>`.

The window remembers your choices in `%LOCALAPPDATA%\iris-mcp\iris-mcp\setup.json`.
Once the dependencies are installed, `iris-mcp-setup` opens it too.

### Without the window

```powershell
python -m pip install -r requirements.txt
python -m iris.configure                          # every installed client
python -m iris.configure --client codex --print   # show the config, change nothing
python -m iris.configure --client claude-desktop --remove
```

### By hand

Use the ABSOLUTE path of the interpreter that ran `pip install`. A bare
`python` can resolve differently for GUI-launched clients than in your shell
(the Windows Store stub may win and has none of the dependencies, so the
server dies with `No module named ...`).

Claude Code:

```powershell
$py = python -c "import sys; print(sys.executable)"
claude mcp add --scope user iris -- $py "<full-path-to-iris>\server.py"
```

Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "iris": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\iris-desktop-mcp\\server.py"]
    }
  }
}
```

Codex (`%USERPROFILE%\.codex\config.toml`):

```toml
[mcp_servers.iris]
command = "C:\\path\\to\\python.exe"
args = ["C:\\path\\to\\iris-desktop-mcp\\server.py"]
startup_timeout_sec = 30
```

### Settings (environment variables)

The setup window writes these into each client's config. Only non-defaults
are written.

| Variable | Default | Meaning |
|---|---|---|
| `IRIS_LOG_DIR` | `<repo>\logs` from a checkout, else the user log dir | where `iris.log` and `events.jsonl` go |
| `IRIS_TELEMETRY` | `1` | `0` turns off the local `events.jsonl` log (nothing is ever sent anywhere) |
| `IRIS_APPS` | search order in `python -m iris.doctor` | path to an `apps.yaml` of launch shortcuts |
| `IRIS_TESSERACT` | auto-detect | path to `tesseract.exe` |
| `IRIS_OCR_PSM` / `IRIS_OCR_PASS2_PSM` | `11` / `6` | Tesseract page modes for the first and fallback OCR pass |
| `IRIS_UIA_WALK` | off | `1` forces full UIA tree walks (slower, finds more) |

If the server "fails to launch", run *Run doctor* in the setup window, then
read the client's own log. For Claude Code that is
`%LOCALAPPDATA%\claude-cli-nodejs\Cache\<project>\mcp-logs-iris\`; the
interpreter problem above shows up there as an `ImportError` on stderr.

### Benches

```powershell
# Self-test (no MCP client needed)
python -c "from iris.self_test import run_self_test; import json; print(json.dumps(run_self_test(), indent=2))"

# Live accuracy bench (opens a Tk window, drives clicks against it)
python -m iris.bench.runner
```

## Try the examples

Standalone scripts that work with just the Python API (no MCP client
needed). Read, run, copy:

```powershell
python examples/01_hello_iris.py             # status + monitor topology
python examples/02_clipboard_pipeline.py     # read/transform/write clipboard
python examples/03_open_url_in_chrome.py     # recipe-driven Chrome control
python examples/04_find_and_click_real_app.py # spawn Notepad, find + UIA invoke
```

See `examples/README.md` for the index.

## See it actually work

Reference machine: Windows 11 + Python 3.12, four monitors at 100% / 100% /
150% / 175% DPI (discovered live, nothing configured). 2026-09-06:

```
total attempts        : 59
find rate             : 100.0%
correct-button rate   : 100.0%
miss distance  mean   : 0.02 px
miss distance  p50    : 0.0 px
miss distance  p95    : 0.0 px
miss distance  max    : 1.0 px
backend distribution  : {'ocr': 59}
OCR -> UIA upgrades   : 59
```

Per-scenario:
```
baseline_static  correct=100.0%  miss_p50=0.0  miss_p95=0.0
window_dragged   correct=100.0%  miss_p50=0.0  miss_p95=0.0
window_resized   correct=100.0%  miss_p50=0.0  miss_p95=0.0
per_monitor      correct=100.0%  miss_p50=0.0  miss_p95=0.0   (4 monitors)
raise_then_click correct=100.0%  miss_p50=0.0  miss_p95=0.0
```

Full transcripts: `assets/bench-output.txt` and `assets/gauntlet-output.txt`.
Doctor diagnostic: `assets/doctor-output.txt`. Run `python -m iris.bench.runner`
and `python -m iris.bench.gauntlet` on your own hardware to compare.

## Tools the MCP server exposes

### Diagnostics
- `iris_status` backend availability, OCR + UIA readiness, DPI mode, cache stats
- `self_test` runs the 15-check battery against a built-in Tk harness

### Window discovery
- `list_windows` enumerate top-level windows, filterable by process / title
- `find_window` match a single window from a spec
- `screen_info` monitor topology, discovered live: count, virtual desktop,
  and per monitor bounds / work area / DPI scale / primary flag
- `focus` bring a window forward and get a `FocusToken`

Iris never assumes a display layout. One laptop panel or five mixed-DPI
monitors both work; a monitor plugged in while the server runs shows up on
the next call. Monitors are ordered primary first, then top-to-bottom,
left-to-right, and that order is shared by every tool: `monitors[i].index`
is what tokens and `inspect` report, `monitors[i].capture_id` (1-based) is
what `see(monitor=)` / `screenshot(monitor=)` take (0 = whole desktop).

### Vision
- `see` window-cropped screenshot (recommended default)
- `see_full` whole-desktop screenshot
- `screenshot` / `screenshot_window` / `screenshot_full` variants
- `zoom(x, y, size, magnify)` magnified full-res crop with a coordinate grid
- `see(token, marks=True)` numbers every element (Set-of-Mark) and returns
  the legend in `meta.marks`; `list_marks(token)` is the legend alone

Every capture returns `[image, meta]`. The image is shrunk to save tokens
(3-6x on a multi-monitor desktop), so `meta.scale` / `meta.origin` are the
only way to turn a point in the picture into a screen pixel. Pass `grid=True`
to draw SCREEN coordinates onto the image, then `zoom` to refine.

### Find and act
- `find(token, target)` resolver-routed locate. `target` is plain text
  (UIA ranked by exactness + role, then OCR at 1x and 2x), or the pixel
  grammar: `color:#ff3b30~30@4-400` (color blob, tolerance 30, area 4..400)
  or `image:C:/icons/gear.png~0.8` (template). `prefer_roles=["Button"]`
  steers text-heavy apps.
- `events_tail(n)` last find/click telemetry events (`<log_dir>/events.jsonl`)
- `inspect(token)` walk the UIA tree
- `discover(token)` full ground-truth dump (UIA + OCR + screenshot)
- `discover_panels` Qt/Win32/Electron panel discovery
- `suggest_alternatives` lower-threshold fuzzy matches when exact text not found
- `wait_for` poll for text/state changes with backoff

### Input
- `do(steps, token)` run a whole sequence in one call (focus, click, set,
  type, key, hotkey, scroll, wait, wait_for, find, marks); stops at the
  first failure. Fewer round trips is the biggest speed win for an agent.
- `click(token, mark=N)` click element number N from the last marks pass
- `click(token, target, ...)` find + click. Delivery: UIA Invoke, else a
  quiet click (mouse messages posted to the window, cursor untouched,
  focus untouched) when the target's window class is known to honor
  them or proves it by repainting, else the real mouse
- `drag(token, from_*, to_* | dx/dy)`, `knob(token, target, amount, mode)`, `hold(...)`
  press-hold-move gestures: drag and drop, sliders, faders, knobs
- `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll`, `mouse_pos`
- `type_text`, `press_key`, `hotkey`, `release`

### App lifecycle
- `launch(app)` start an app from the `apps.yaml` registry
- `list_apps` what the registry knows about

### Recipes (named workflows)
- `list_recipes` enumerate YAML-defined workflows
- `run_recipe(name, args)` execute a chained recipe by name

## Architecture

```
                          MCP client (Claude, etc.)
                                    |
                                    v
                            +---------------+
                            |   server.py    |
                            +-------+-------+
                                    |
                                    v
+--------------+    +---------------+----------------+    +-----------------+
|              |    |                                |    |                 |
|  spatial.py  |--->|          resolver.py           |<---|   semantic.py   |
|  Win32 / mss |    |    UIA -> OCR -> handoff       |    |   uiautomation  |
|              |    |                                |    |                 |
+--------------+    +---------------+----------------+    +-----------------+
                                    |
                                    v
                            +---------------+
                            |   vision.py    |
                            | mss + Tesseract |
                            +---------------+
```

Each `find()` call goes:

0. Pixel grammar (`color:` / `image:`): PrintWindow-capture the window and
   search pixels directly (`iris/pixel.py`). No text or UIA needed.
1. Try UIA. Pull up to 100 name matches, rank by exactness, role,
   visibility and invokability (the window's own title node and off-screen
   containers sink), return the ranked list.
2. Try OCR. PrintWindow-capture the window (works through occlusion),
   Tesseract `--psm 11` at native scale, then 2x upscale if nothing matched,
   fuzzy-match the target (multi-word joins stay on one OCR line).
3. After OCR finds text, call UIA `ControlFromPoint` to upgrade the text
   bbox to the enclosing widget bbox, but only if that widget belongs to
   the token's window. Click the widget center, not the glyphs.
4. If nothing matches, return a `vision_handoff` with a screenshot crop so
   the LLM can decide.

Every `find` and `click` is appended to `<log_dir>/events.jsonl`.

`click()` calls `find()`, then either:

- Invokes a UIA pattern (`InvokePattern`, `TogglePattern`, etc.) directly
  with no mouse motion. Bypasses coord math, DPI, occlusion, animation
  timing. Most reliable.
- Falls back to a `SendInput` move + click at the resolved coords, with a
  pre-flight check that the target is inside the current window AND on a
  monitor.

A denylist (`TkTopLevel`, `TkChild`) catches classes whose UIA `Invoke` is a
silent no-op so Iris uses geometric clicks for those.

## Recipes

Recipes are YAML files in `recipes/` that chain primitives. Example:

```yaml
name: obs.start_recording
description: Focus OBS, click Start Recording, wait for Stop to appear.
steps:
  - id: tok
    action: focus
    args: { match: { process: "obs64.exe" } }
  - action: click
    args: { token: "${tok.token}", target: "Start Recording" }
  - action: wait_for
    args: { token: "${tok.token}", target: "Stop Recording", timeout_ms: 5000 }
```

Run with `mcp__iris__run_recipe(name="obs.start_recording")`. Add new
recipes by dropping `whatever.yaml` into the recipes directory.

### Built-in recipe gallery

| Recipe | Inputs | What it does |
|---|---|---|
| `obs.start_recording` | none | Focus OBS, click Start Recording, wait for Stop to appear |
| `obs.stop_recording` | none | Focus OBS, click Stop Recording, wait for Start to come back |
| `chrome.open_url` | `url` | Focus Chrome, Ctrl+L, type URL, Enter |
| `vscode.command_palette` | `command` | Focus VS Code, Ctrl+Shift+P, type command, Enter |
| `alt_tab_to` | `title_contains` | Bring a matching window to foreground |

## Live accuracy bench

```powershell
python -m iris.bench.runner                              # all scenarios
python -m iris.bench.runner --scenarios baseline_static  # one scenario
python -m iris.bench.runner --no-invoke                  # force geometric clicks
python -m iris.bench.runner --keep-harness               # leave window open
```

The bench spawns an instrumented Tk harness with a grid of buttons of
varied sizes and label styles, then drives Iris find+click against each
under five scenarios: static, dragged window, resized window, parked on
each monitor, raise-from-occlusion. Every button reports exact pixel-level
hit location so the bench measures actual miss distance, not just
"did we click somewhere".

See `iris/bench/README.md` for details.

### Gauntlet (multi-window, tiny targets, timed)

```powershell
python -m iris.bench.gauntlet                    # all 7 levels, 3 windows
python -m iris.bench.gauntlet --levels color_dots,whack
python -m iris.bench.gauntlet --idle 0           # do not wait for user idle
python -m iris.bench.gauntlet --harness win32    # message-driven window: measures quiet clicks
```

The bench above is one window with big text buttons. The Gauntlet spawns
three instrumented canvas windows and makes Iris hit 7 to 11 pt labels,
color squares down to 2 px, the exact shade among near-shade decoys, the
same dot in a specific window, a target covered by another window, a
whack-a-mole target that relocates after every hit, and 12 to 16 px glyph
templates. It drives the real server `click()` and every window reports the
exact pixel that was hit. It takes the mouse, so it waits for 90 s of user
idle by default. Results: `assets/gauntlet-output.txt`.

## Status

Early. Working and tested on Windows 11 with Python 3.10-3.12. Pull
requests welcome, see CONTRIBUTING.md.

## License

MIT. See LICENSE.
