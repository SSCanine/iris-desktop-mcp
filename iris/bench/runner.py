"""Iris accuracy bench runner.

Spawns the instrumented harness, drives Iris find+click against each target
under each scenario, reads the harness's JSONL click receipts to know exactly
where each click landed, and builds a structured report.

Usage:
    python -m iris.bench.runner [--scenarios baseline_static,window_dragged]
                                [--out C:/path/report.json]
                                [--keep-harness]   (don't kill harness after)
                                [--monitor 0]      (restrict to one monitor)

The harness window stays VISIBLE the whole time so you can watch Iris drive
it in real time. The status bar at the top of the harness shows the most
recent click receipt as each attempt completes.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# DPI awareness BEFORE we import anything that touches Win32.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Make iris importable when run as a script.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from iris import input as input_mod
from iris import resolver as resolver_mod
from iris import semantic as semantic_mod
from iris import spatial as spatial_mod
from iris import vision as vision_mod
from iris.bench.harness import TARGETS, TargetSpec
from iris.bench.scenarios import SCENARIOS, Scenario, scenario_by_id
from iris.tokens import FocusToken


# ---------------------------------------------------------------------------
# Attempt result
# ---------------------------------------------------------------------------
@dataclass
class AttemptResult:
    scenario_id: str
    monitor_index: int
    target_id: str
    target_label: str
    # Find outcome
    find_backend: str | None = None
    find_elapsed_ms: float = 0.0
    find_found: bool = False
    upgraded_to_uia: bool = False
    # Click outcome
    click_method: str | None = None
    click_pattern: str | None = None
    click_x: int | None = None
    click_y: int | None = None
    click_error: str | None = None
    # Truth from the harness (None if the click didn't land on a tracked button)
    receipt_button: str | None = None
    receipt_hit_screen: list[int] | None = None
    receipt_center: list[int] | None = None
    miss_distance_px: float | None = None
    # Overall status
    landed_on_correct_button: bool = False
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Event tailer: reads the harness JSONL file as it grows
# ---------------------------------------------------------------------------
class EventTailer:
    """Tails a JSONL file the harness writes to. Records read-position so we
    can ask 'any new events since attempt-start?' without rereading old ones.
    """

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0

    def mark(self) -> int:
        """Snapshot the current end-of-file. Returns the mark token."""
        if self.path.exists():
            self._pos = self.path.stat().st_size
        return self._pos

    def read_new(self, since_mark: int) -> list[dict]:
        """Return all JSONL records appended after `since_mark`."""
        if not self.path.exists():
            return []
        events: list[dict] = []
        with self.path.open("r", encoding="utf-8") as f:
            f.seek(since_mark)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def wait_for(
        self, since_mark: int, kind: str, timeout_ms: int = 1500, poll_ms: int = 25
    ) -> dict | None:
        """Block (poll) until an event of `kind` appears after `since_mark`."""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            for e in self.read_new(since_mark):
                if e.get("kind") == kind:
                    return e
            time.sleep(poll_ms / 1000.0)
        return None


# ---------------------------------------------------------------------------
# Harness lifecycle
# ---------------------------------------------------------------------------
class HarnessProcess:
    """Manage spawning the harness GUI in a subprocess."""

    def __init__(self, title: str, events_path: Path, geometry: str = "900x650+200+200"):
        self.title = title
        self.events_path = events_path
        self.geometry = geometry
        self.proc: subprocess.Popen | None = None
        self.hwnd: int | None = None
        self.pid: int | None = None

    def start(self) -> None:
        # Empty the events file so we don't pick up old runs.
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.write_text("", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "iris.bench.harness",
            "--title",
            self.title,
            "--events",
            str(self.events_path),
            "--geometry",
            self.geometry,
        ]
        # Run the harness with the iris/ package importable.
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_HERE.parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(cmd, env=env, creationflags=creationflags)
        # Wait for the harness to appear in window enumeration.
        deadline = time.time() + 8.0
        while time.time() < deadline and self.hwnd is None:
            for w in spatial_mod.enumerate_windows():
                if self.title in w.title:
                    self.hwnd = w.hwnd
                    self.pid = w.pid
                    break
            if self.hwnd is None:
                time.sleep(0.1)
        if self.hwnd is None:
            self.stop()
            raise RuntimeError(f"harness window {self.title!r} never appeared")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# ---------------------------------------------------------------------------
# The driver: find + click + measure
# ---------------------------------------------------------------------------
def _make_token(hwnd: int, pid: int, title: str) -> FocusToken:
    info_bounds = spatial_mod.current_bounds(hwnd)
    if info_bounds is None:
        raise RuntimeError(f"hwnd {hwnd} has no bounds")
    return FocusToken.create(
        hwnd=hwnd,
        pid=pid,
        exe_name="python.exe",
        title=title,
        monitor_index=spatial_mod.get_monitor_for_window(info_bounds),
        bounds=info_bounds,
    )


def _latest_layout(tailer: EventTailer) -> dict | None:
    """Return the most recent layout_snapshot from the event log, or None."""
    events = tailer.read_new(0)
    snap = None
    for e in events:
        if e.get("kind") == "layout_snapshot":
            snap = e
    return snap


def _ground_truth_for(target_id: str, tailer: EventTailer) -> dict | None:
    snap = _latest_layout(tailer)
    if snap is None:
        return None
    for b in snap.get("buttons", []):
        if b.get("id") == target_id:
            return b
    return None


def _attempt_one(
    tk_token: FocusToken,
    target: TargetSpec,
    tailer: EventTailer,
    scenario_id: str,
    monitor_index: int,
    hwnd: int,
    prefer_invoke: bool = True,
) -> AttemptResult:
    """Drive Iris to find + click a single target and measure outcome."""
    r = AttemptResult(
        scenario_id=scenario_id,
        monitor_index=monitor_index,
        target_id=target.id,
        target_label=target.label,
    )
    t0 = time.perf_counter()

    # Make sure the harness is the foreground window so its mainloop processes
    # the synthesized click events promptly. Without this, Windows still
    # delivers the click to the window under the cursor (which IS the
    # harness), but the Tk event queue can lag behind other foreground
    # activity and we time out waiting for the receipt.
    try:
        spatial_mod.bring_to_front(hwnd)
    except Exception:
        pass
    time.sleep(0.1)

    # Always flush the OCR cache so we don't reuse pre-mutation pixels.
    vision_mod.clear_ocr_cache(tk_token.id)

    fr = resolver_mod.find(tk_token, target.label)
    r.find_backend = fr.backend
    r.find_elapsed_ms = fr.elapsed_ms
    r.find_found = fr.found
    if not fr.found:
        r.click_error = f"target_not_found ({fr.backend})"
        # Dump the captured window so the user can SEE what Iris had to work with.
        try:
            img = vision_mod.capture_window(tk_token.hwnd)
            if img is not None:
                dump_dir = Path(tempfile.gettempdir()) / "iris_bench_failures"
                dump_dir.mkdir(parents=True, exist_ok=True)
                path = dump_dir / f"{scenario_id}_{target.id}.png"
                img.save(path)
                r.notes.append(f"capture_saved:{path}")
                # Also list what nearest_matches OCR suggested.
                if fr.nearest_matches:
                    names = [m.get("text") for m in fr.nearest_matches[:3]]
                    r.notes.append(f"nearest:{names}")
        except Exception as e:
            r.notes.append(f"capture_failed:{e}")
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        return r

    top = fr.hits[0]
    r.upgraded_to_uia = bool(top.get("upgraded_to_uia"))
    bbox = top.get("bbox") or top.get("bounds")
    target_x = bbox["x"] + bbox["width"] // 2
    target_y = bbox["y"] + bbox["height"] // 2
    r.click_x = target_x
    r.click_y = target_y

    # Mark the event log before clicking so we read ONLY the post-click events.
    mark = tailer.mark()

    # Click via the same logic as server.click(), inline so we don't need the
    # MCP layer running. Prefer UIA invoke when available (configurable).
    invoke_ctrl = fr.controls[0] if fr.controls else None
    if prefer_invoke and invoke_ctrl is not None and semantic_mod.is_invoke_trusted(invoke_ctrl):
        ir = semantic_mod.try_pattern_click(invoke_ctrl)
        if ir.get("ok"):
            r.click_method = "uia_pattern"
            r.click_pattern = ir.get("pattern")
        else:
            input_mod.click(x=target_x, y=target_y, button="left", clicks=1)
            r.click_method = "mouse_after_invoke_failed"
            r.notes.append(f"invoke_attempt:{ir}")
    else:
        input_mod.click(x=target_x, y=target_y, button="left", clicks=1)
        r.click_method = "mouse"

    # Wait for the harness to confirm the click landed somewhere.
    receipt = tailer.wait_for(mark, "click_receipt", timeout_ms=1500)
    if receipt is None:
        r.click_error = "no_click_receipt_within_1500ms"
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        return r

    r.receipt_button = receipt.get("button_id")
    r.receipt_hit_screen = receipt.get("hit_screen")
    r.receipt_center = receipt.get("button_center")
    r.miss_distance_px = receipt.get("miss_distance_px")
    r.landed_on_correct_button = r.receipt_button == target.id
    r.elapsed_ms = (time.perf_counter() - t0) * 1000

    # If no click_receipt arrived, check if an `any_click` event landed
    # somewhere (i.e. the click DID reach the harness, just not on a button).
    if not r.landed_on_correct_button and r.receipt_button is None:
        leak_events = [e for e in tailer.read_new(0) if e.get("kind") == "any_click"]
        if leak_events:
            last = leak_events[-1]
            r.notes.append(f"click_landed_on:{last.get('widget')}@{last.get('screen')}")
        # Pull ground truth (where the button ACTUALLY is on screen) so we
        # can diagnose why the click missed.
        gt = _ground_truth_for(target.id, tailer)
        if gt and r.click_x is not None:
            dx = r.click_x - gt["center"][0]
            dy = r.click_y - gt["center"][1]
            r.notes.append(
                f"ground_truth_center={gt['center']} "
                f"computed_click=({r.click_x},{r.click_y}) "
                f"delta=({dx},{dy})"
            )
    return r


# ---------------------------------------------------------------------------
# Live progress printer
# ---------------------------------------------------------------------------
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GRAY = "\033[90m"
BOLD = "\033[1m"


def _color_status(r: AttemptResult) -> str:
    if r.landed_on_correct_button and (r.miss_distance_px or 0) < 4:
        return f"{GREEN}PASS{RESET}"
    if r.landed_on_correct_button:
        return f"{YELLOW}PASS*{RESET}"  # right button but offset center
    if r.receipt_button:
        return f"{RED}WRONG-BUTTON{RESET}"
    if r.find_found:
        return f"{RED}NO-RECEIPT{RESET}"
    return f"{RED}NOT-FOUND{RESET}"


def _print_attempt(r: AttemptResult) -> None:
    status = _color_status(r)
    backend = r.find_backend or "-"
    method = r.click_method or "-"
    miss = f"{r.miss_distance_px:.1f}px" if r.miss_distance_px is not None else "n/a"
    coords = f"({r.click_x},{r.click_y})" if r.click_x is not None else "(-,-)"
    line = (
        f"  {status:>22} {CYAN}{r.target_id:<15}{RESET} "
        f"backend={backend:<14} method={method:<8} "
        f"coord={coords:<13} miss={miss:<8} {GRAY}{r.elapsed_ms:6.0f}ms{RESET}"
    )
    print(line, flush=True)
    if r.click_error:
        print(f"    {RED}error{RESET}: {r.click_error}", flush=True)
    for note in r.notes:
        print(f"    {GRAY}note{RESET}: {note}", flush=True)


def _print_section(title: str) -> None:
    bar = "=" * 78
    print(f"\n{BOLD}{bar}{RESET}", flush=True)
    print(f"{BOLD}{title}{RESET}", flush=True)
    print(f"{BOLD}{bar}{RESET}", flush=True)


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------
def _aggregate(results: list[AttemptResult]) -> dict:
    total = len(results)
    if total == 0:
        return {"total": 0}
    correct = sum(1 for r in results if r.landed_on_correct_button)
    found = sum(1 for r in results if r.find_found)
    misses = [r.miss_distance_px for r in results if r.miss_distance_px is not None]
    backend_dist: dict[str, int] = {}
    method_dist: dict[str, int] = {}
    upgrades = 0
    for r in results:
        backend_dist[r.find_backend or "none"] = backend_dist.get(r.find_backend or "none", 0) + 1
        method_dist[r.click_method or "none"] = method_dist.get(r.click_method or "none", 0) + 1
        if r.upgraded_to_uia:
            upgrades += 1
    misses_sorted = sorted(misses)

    def pct(p: float) -> float | None:
        if not misses_sorted:
            return None
        idx = min(int(p * (len(misses_sorted) - 1)), len(misses_sorted) - 1)
        return round(misses_sorted[idx], 2)

    return {
        "total": total,
        "find_rate": round(found / total, 3),
        "correct_button_rate": round(correct / total, 3),
        "miss_px_p50": pct(0.50),
        "miss_px_p95": pct(0.95),
        "miss_px_max": round(max(misses), 2) if misses else None,
        "miss_px_mean": round(sum(misses) / len(misses), 2) if misses else None,
        "backend_distribution": backend_dist,
        "click_method_distribution": method_dist,
        "ocr_to_uia_upgrades": upgrades,
    }


def _build_report(results: list[AttemptResult]) -> dict:
    by_scenario: dict[str, list[AttemptResult]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario_id, []).append(r)
    return {
        "ts": time.time(),
        "overall": _aggregate(results),
        "per_scenario": {sid: _aggregate(rs) for sid, rs in by_scenario.items()},
        "attempts": [asdict(r) for r in results],
    }


def _print_summary(report: dict) -> None:
    _print_section("SUMMARY")
    overall = report["overall"]
    print(f"  total attempts        : {overall['total']}", flush=True)
    print(f"  find rate             : {overall['find_rate'] * 100:.1f}%", flush=True)
    print(f"  correct-button rate   : {overall['correct_button_rate'] * 100:.1f}%", flush=True)
    if overall.get("miss_px_mean") is not None:
        print(f"  miss distance  mean   : {overall['miss_px_mean']} px", flush=True)
        print(f"  miss distance  p50    : {overall['miss_px_p50']} px", flush=True)
        print(f"  miss distance  p95    : {overall['miss_px_p95']} px", flush=True)
        print(f"  miss distance  max    : {overall['miss_px_max']} px", flush=True)
    print(f"  backend distribution  : {overall['backend_distribution']}", flush=True)
    print(f"  click method dist     : {overall['click_method_distribution']}", flush=True)
    print(f"  OCR -> UIA upgrades   : {overall['ocr_to_uia_upgrades']}", flush=True)

    print(f"\n{BOLD}per-scenario:{RESET}", flush=True)
    for sid, agg in report["per_scenario"].items():
        print(
            f"  {CYAN}{sid:<22}{RESET} correct={agg['correct_button_rate'] * 100:5.1f}%  "
            f"miss_p50={agg.get('miss_px_p50')}  "
            f"miss_p95={agg.get('miss_px_p95')}  "
            f"backends={agg['backend_distribution']}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------
def _expand_scenarios(scenarios: list[Scenario]) -> list[tuple[Scenario, int]]:
    """Fan out per_monitor scenarios into one entry per monitor."""
    expanded: list[tuple[Scenario, int]] = []
    for s in scenarios:
        if s.per_monitor:
            n_mons = len(spatial_mod.list_monitors())
            for idx in range(n_mons):
                expanded.append((s, idx))
        else:
            expanded.append((s, 0))
    return expanded


def _initial_harness_geometry(width: int = 900, height: int = 650) -> str:
    """Pick a starting geometry that lands the harness inside the primary
    monitor regardless of user setup. The window will still be moved by
    scenarios that mutate position; this just makes sure it STARTS visible
    on a single-monitor laptop and a multi-monitor desktop alike.
    """
    try:
        monitors = spatial_mod.list_monitors()
    except Exception:
        monitors = []
    if not monitors:
        return f"{width}x{height}+100+100"
    primary = monitors[0]  # list_monitors() is primary-first (discovered per machine)
    # If the requested size doesn't fit, shrink to 80% of the monitor.
    w = min(width, int(primary.width * 0.8))
    h = min(height, int(primary.height * 0.8))
    x = primary.x + (primary.width - w) // 2
    y = primary.y + (primary.height - h) // 2
    return f"{w}x{h}+{x}+{y}"


def run_bench(
    scenarios: list[Scenario],
    out_path: Path | None = None,
    keep_harness: bool = False,
    prefer_invoke: bool = True,
) -> dict:
    events_path = Path(tempfile.gettempdir()) / "iris_bench_events.jsonl"
    title = "IRIS_BENCH_HARNESS"
    harness = HarnessProcess(
        title=title,
        events_path=events_path,
        geometry=_initial_harness_geometry(),
    )
    tailer = EventTailer(events_path)
    results: list[AttemptResult] = []

    _print_section(f"IRIS ACCURACY BENCH  --  {len(scenarios)} scenario(s)")
    print(f"  events log: {events_path}", flush=True)

    harness.start()
    print(f"  harness started  hwnd={harness.hwnd}  pid={harness.pid}", flush=True)
    # Drain the harness_ready event so we don't confuse it with click receipts.
    tailer.wait_for(0, "harness_ready", timeout_ms=2000)
    targets_by_id = {t.id: t for t in TARGETS}

    try:
        for scenario, monitor_index in _expand_scenarios(scenarios):
            ctx = {"monitor_index": monitor_index}
            label = f"{scenario.id}"
            if scenario.per_monitor:
                mons = spatial_mod.list_monitors()
                m = mons[monitor_index] if monitor_index < len(mons) else None
                label += f"  (monitor {monitor_index}: {m.width}x{m.height})" if m else ""
            _print_section(f"SCENARIO  {label}")
            print(f"  {scenario.description}", flush=True)

            err = scenario.setup(harness.hwnd, ctx)
            if err:
                print(f"  {YELLOW}skip{RESET}: {err}", flush=True)
                continue

            # Build a fresh token for each scenario (sim what a real caller does).
            try:
                tk_token = _make_token(harness.hwnd, harness.pid, title)
            except Exception as e:
                print(f"  {RED}token creation failed: {e}{RESET}", flush=True)
                continue

            for tid in scenario.target_ids:
                target = targets_by_id.get(tid)
                if target is None:
                    continue
                r = _attempt_one(
                    tk_token,
                    target,
                    tailer,
                    scenario.id,
                    monitor_index,
                    harness.hwnd,
                    prefer_invoke=prefer_invoke,
                )
                results.append(r)
                _print_attempt(r)
                # Tiny pause so the UI catches up; also lets the user see it.
                time.sleep(0.15)
    finally:
        report = _build_report(results)
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\n{GRAY}report written: {out_path}{RESET}", flush=True)
        _print_summary(report)
        if not keep_harness:
            harness.stop()

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenarios", default="", help="Comma-separated scenario ids. Default: all."
    )
    parser.add_argument(
        "--out", default="", help="Path to write JSON report. Default: $TEMP/iris_bench_report.json"
    )
    parser.add_argument(
        "--keep-harness", action="store_true", help="Leave the harness window open after the run."
    )
    parser.add_argument(
        "--no-invoke",
        action="store_true",
        help="Disable UIA invoke fast-path. Forces geometric "
        "mouse clicks so we measure pixel accuracy.",
    )
    args = parser.parse_args()

    if args.scenarios:
        ids = [s.strip() for s in args.scenarios.split(",") if s.strip()]
        chosen = [s for sid in ids for s in [scenario_by_id(sid)] if s]
        if not chosen:
            print(f"no scenarios matched {ids!r}. Known: {[s.id for s in SCENARIOS]}")
            return 2
    else:
        chosen = SCENARIOS

    out_path = (
        Path(args.out) if args.out else Path(tempfile.gettempdir()) / "iris_bench_report.json"
    )

    try:
        report = run_bench(
            chosen,
            out_path=out_path,
            keep_harness=args.keep_harness,
            prefer_invoke=not args.no_invoke,
        )
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        return 130

    overall = report.get("overall", {})
    correct = overall.get("correct_button_rate", 0.0)
    # Exit non-zero if accuracy is poor so CI can flag regressions.
    return 0 if correct >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(main())
