"""Gauntlet: multi-window, tiny-target, timed training ground for Iris.

The original bench proves Iris can hit big text buttons in one window. Real
desktops are not like that. The Gauntlet spawns several instrumented canvas
windows and makes Iris find and click:

    tiny_text     7 to 11 pt labels                      OCR small text, 2x pass
    color_dots    squares from 24 px down to 2 px        pixel backend precision
    decoys        the exact color among near-shades      tolerance + ranking
    multi_window  same dots in every window, hit the     token scoping
                  one in window N
    overlap       another window covers the target        occlusion + auto-raise
    whack         target relocates after every hit,      latency, cache staleness
                  time budget
    template      12 to 16 px glyph icons                 template matching

Every attempt goes through the REAL server click() (find, clamp, auto-raise,
invoke/mouse, telemetry), not a re-implementation. Each window reports the
exact screen pixel that was hit, so misses are measured, not guessed.

    python -m iris.bench.gauntlet                    # all levels, 3 windows
    python -m iris.bench.gauntlet --levels color_dots,whack
    python -m iris.bench.gauntlet --idle 0           # do not wait for user idle
    python -m iris.bench.gauntlet --keep             # leave windows open at end

Exit code 0 when every level clears its threshold, 1 otherwise, so it can
gate changes like the original bench.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# DPI awareness before anything touches a Win32 coordinate (see runner.py).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageDraw  # noqa: E402

from iris import __main__ as server  # noqa: E402  (real click(), registry)
from iris import input as input_mod  # noqa: E402
from iris import spatial as spatial_mod  # noqa: E402
from iris.bench.runner import EventTailer  # noqa: E402
from iris.bench.scenarios import _move as move_window  # noqa: E402
from iris.tokens import FocusToken  # noqa: E402

_HERE = Path(__file__).parent
TMP = Path(tempfile.gettempdir()) / "iris_gauntlet"

RESET, RED, GREEN, YELLOW, CYAN, GRAY, BOLD = (
    "\033[0m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[36m",
    "\033[90m",
    "\033[1m",
)

WIN_W, WIN_H = 720, 520
STATUS_H = 26  # must match gauntlet_harness.STATUS_HEIGHT
BG_LIGHT = "#f2f2f2"
TEXT_COLOR = "#202020"
# Saturated, mutually distinct (> 24 per channel), none close to the window bg
# or status bar colors.
DOT_COLORS = [
    "#ff3b30",
    "#34c759",
    "#007aff",
    "#ff9500",
    "#af52de",
    "#5ac8fa",
    "#ffcc00",
    "#ff2d55",
    "#00c7be",
    "#8e5b1a",
]
WORDS = [
    "Apply", "Cancel", "Reset", "Mute", "Solo", "Loop", "Sync", "Cue", "Save", "Load",
    "Undo", "Redo", "Zoom", "Grid", "Snap", "Fade", "Gain", "Bass", "Play", "Stop",
    "Arm", "Tap", "Beat", "Deck", "Send", "Open", "Pause", "Next", "Home", "Help",
    "Filter", "Render", "Export", "Import", "Layer", "Track", "Clip", "Scene", "Bus", "Dock",
]  # fmt: skip

LEVELS = [
    "tiny_text",
    "color_dots",
    "decoys",
    "multi_window",
    "overlap",
    "whack",
    "template",
    "marks",
]

# Minimum correct rate per level for exit code 0.
THRESHOLDS = {
    "tiny_text": 0.75,
    "color_dots": 0.90,
    "decoys": 0.90,
    "multi_window": 0.90,
    "overlap": 0.75,
    "whack": 0.80,  # hit rate within the time budget
    "template": 0.80,
    "marks": 0.90,
}


# ---------------------------------------------------------------------------
# Idle gate: never fight the human for the mouse.
# ---------------------------------------------------------------------------
def user_idle_seconds() -> float:
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LII()
    lii.cbSize = ctypes.sizeof(LII)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0


def wait_for_idle(min_idle_s: float, max_wait_s: float = 3600.0) -> bool:
    if min_idle_s <= 0:
        return True
    t0 = time.time()
    last_print = 0.0
    while time.time() - t0 < max_wait_s:
        idle = user_idle_seconds()
        if idle >= min_idle_s:
            return True
        if time.time() - last_print > 15:
            print(
                f"{GRAY}waiting for {min_idle_s:.0f}s of user idle "
                f"(currently {idle:.0f}s). The gauntlet moves the mouse.{RESET}",
                flush=True,
            )
            last_print = time.time()
        time.sleep(2.0)
    return False


# ---------------------------------------------------------------------------
# Harness window lifecycle
# ---------------------------------------------------------------------------
class GauntletWindow:
    def __init__(self, index: int, geometry: str, bg: str = BG_LIGHT, harness: str = "tk"):
        self.index = index
        self.harness = harness
        self.title = f"IRIS_GAUNTLET_{chr(65 + index)}"
        self.geometry = geometry
        self.bg = bg
        TMP.mkdir(parents=True, exist_ok=True)
        self.events_path = TMP / f"events_{index}.jsonl"
        self.commands_path = TMP / f"commands_{index}.jsonl"
        self.tailer = EventTailer(self.events_path)
        self.proc: subprocess.Popen | None = None
        self.hwnd: int | None = None
        self.pid: int | None = None
        self.token: FocusToken | None = None
        self.seq = 0
        self.layout: dict | None = None

    def start(self) -> None:
        self.events_path.write_text("", encoding="utf-8")
        self.commands_path.write_text("", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            (
                "iris.bench.gauntlet_harness_win32"
                if self.harness == "win32"
                else "iris.bench.gauntlet_harness"
            ),
            "--title",
            self.title,
            "--events",
            str(self.events_path),
            "--commands",
            str(self.commands_path),
            "--geometry",
            self.geometry,
            "--bg",
            self.bg,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_HERE.parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, env=env, creationflags=flags)
        deadline = time.time() + 10.0
        while time.time() < deadline and self.hwnd is None:
            for w in spatial_mod.enumerate_windows():
                if w.title == self.title:
                    self.hwnd, self.pid = w.hwnd, w.pid
                    break
            if self.hwnd is None:
                time.sleep(0.1)
        if self.hwnd is None:
            self.stop()
            raise RuntimeError(f"{self.title} never appeared")
        self.tailer.wait_for(0, "harness_ready", timeout_ms=5000)
        self.refresh_token()

    def refresh_token(self) -> FocusToken:
        bounds = spatial_mod.current_bounds(self.hwnd)
        if bounds is None:
            raise RuntimeError(f"{self.title} has no bounds")
        tk = FocusToken.create(
            hwnd=self.hwnd,
            pid=self.pid,
            exe_name="python.exe",
            title=self.title,
            monitor_index=max(spatial_mod.get_monitor_for_window(bounds), 0),
            bounds=bounds,
        )
        server.registry.store(tk)
        self.token = tk
        return tk

    def send(self, cmd: dict) -> None:
        with self.commands_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(cmd) + "\n")

    def layout_targets(self, targets: list[dict], bg: str | None = None) -> dict:
        self.seq += 1
        self.send({"cmd": "layout", "seq": self.seq, "targets": targets, "bg": bg or self.bg})
        return self.wait_layout(self.seq)

    def move_target(self, tid: str, x: int, y: int) -> dict:
        self.seq += 1
        self.send({"cmd": "move", "seq": self.seq, "id": tid, "x": x, "y": y})
        return self.wait_layout(self.seq)

    def wait_layout(self, seq: int, timeout_ms: int = 3000) -> dict:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            for e in self.tailer.read_new(0):
                if e.get("kind") == "layout_snapshot" and int(e.get("seq", -1)) >= seq:
                    self.layout = e
            if self.layout is not None and int(self.layout.get("seq", -1)) >= seq:
                # Give Tk one more frame to actually paint before we capture.
                # NOTE: the OCR cache is deliberately NOT cleared here. Real
                # callers never clear it, so the whack level must prove the
                # cache notices a moved target on its own.
                time.sleep(0.08)
                return self.layout
            time.sleep(0.02)
        raise RuntimeError(f"{self.title}: no layout_snapshot for seq {seq}")

    def ground_truth(self, tid: str) -> dict | None:
        if not self.layout:
            return None
        for t in self.layout.get("targets", []):
            if t.get("id") == tid:
                return t
        return None

    def canvas_area(self) -> tuple[int, int]:
        w, h = (int(v) for v in self.geometry.split("+")[0].split("x"))
        return w, h - STATUS_H

    def raise_(self, activate: bool = False) -> None:
        """Put the window on top. activate=False (default) reorders without
        taking focus from the user; only the real-mouse path activates,
        and that click would have activated the window anyway."""
        try:
            if activate:
                spatial_mod.bring_to_front(self.hwnd)
            else:
                import win32con
                import win32gui

                win32gui.SetWindowPos(
                    self.hwnd,
                    win32con.HWND_TOP,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.send({"cmd": "quit"})
            time.sleep(0.15)
        except Exception:
            pass
        if self.proc is not None:
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
# Attempt = one find + click, measured against ground truth
# ---------------------------------------------------------------------------
@dataclass
class Attempt:
    level: str
    window: str
    target: str  # what we asked Iris to find
    expect_id: str
    status: str = "pending"  # hit | wrong_target | decoy | wrong_window | miss | <error>
    ok: bool = False
    backend: str | None = None
    click_method: str | None = None
    click_xy: list[int] | None = None
    miss_px: float | None = None
    total_ms: float = 0.0
    receipt_ms: float | None = None
    raised: bool = False
    cursor_moved: bool = False
    cursor_after: list[int] | None = None  # where the pointer was left after the attempt
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def _center(rect: list[int]) -> tuple[float, float]:
    x, y, w, h = rect
    return x + w / 2.0, y + h / 2.0


def run_attempt(
    level: str,
    win: GauntletWindow,
    target: str,
    expect_id: str,
    windows: list[GauntletWindow],
    *,
    prefer_invoke: bool = True,
    raise_first: bool = True,
    receipt_timeout_ms: int = 1500,
    click_method: str = "auto",
    mark: int | None = None,
) -> Attempt:
    a = Attempt(level=level, window=win.title, target=target, expect_id=expect_id)
    if raise_first and click_method == "mouse":
        win.raise_(activate=True)
        time.sleep(0.05)
    marks = {w.title: w.tailer.mark() for w in windows}
    sends_before = input_mod.sendinput_count()
    t0 = time.perf_counter()
    try:
        if mark is not None:
            out = server.click(
                token=win.token.id,
                mark=mark,
                prefer_invoke=prefer_invoke,
                click_method=click_method,
            )
        else:
            out = server.click(
                token=win.token.id,
                target=target,
                prefer_invoke=prefer_invoke,
                click_method=click_method,
            )
    except Exception as e:
        a.status = f"exception:{type(e).__name__}"
        a.detail = str(e)[:200]
        a.total_ms = (time.perf_counter() - t0) * 1000
        return a
    a.total_ms = (time.perf_counter() - t0) * 1000
    a.backend = out.get("backend")
    a.click_method = out.get("click_method")
    a.raised = bool(out.get("raised_before_click"))
    # Iris only moves the cursor through SendInput; the user moving it
    # while watching must not count against a quiet click.
    if input_mod.sendinput_count() > sends_before:
        a.notes.append("mouse_used")
        a.cursor_moved = True
    cx, cy = input_mod.position()
    a.cursor_after = [int(cx), int(cy)]
    if out.get("x") is not None:
        a.click_xy = [out.get("x"), out.get("y")]
    if not out.get("ok"):
        a.status = str(out.get("error", "click_failed"))
        fr = out.get("find_result") or {}
        near = [m.get("text") for m in (fr.get("nearest_matches") or [])[:3]]
        a.detail = ";".join(fr.get("notes", [])[:2]) + (f" nearest={near}" if near else "")
        return a
    t_click = time.perf_counter()
    receipt = win.tailer.wait_for(marks[win.title], "click_receipt", timeout_ms=receipt_timeout_ms)
    if receipt is not None:
        a.receipt_ms = (time.perf_counter() - t_click) * 1000
    # Did the click land in another gauntlet window instead?
    stray = None
    for w in windows:
        if w is win:
            continue
        for e in w.tailer.read_new(marks[w.title]):
            if e.get("kind") == "click_receipt":
                stray = (w.title, e)
    gt = win.ground_truth(expect_id)
    if receipt is None:
        if stray is not None:
            a.status = "wrong_window"
            a.detail = f"receipt in {stray[0]} at {stray[1].get('screen')}"
        else:
            a.status = "no_receipt"
            a.detail = "click reported ok but no window registered it"
        return a
    hit_id = receipt.get("hit_id")
    if gt is not None and receipt.get("screen"):
        cx, cy = _center(gt["rect"])
        sx, sy = receipt["screen"]
        a.miss_px = round(((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5, 2)
    if hit_id == expect_id:
        a.status = "hit"
        a.ok = True
    elif receipt.get("hit_decoy"):
        a.status = "decoy"
        a.detail = f"hit decoy {hit_id}"
    elif hit_id:
        a.status = "wrong_target"
        a.detail = f"hit {hit_id}"
    else:
        a.status = "miss"
        a.detail = f"nearest {receipt.get('nearest_id')} at {receipt.get('nearest_distance_px')}px"
    return a


def _print_attempt(a: Attempt) -> None:
    color = GREEN if a.ok else (YELLOW if a.status in ("miss",) else RED)
    miss = f"{a.miss_px:5.1f}px" if a.miss_px is not None else "   -   "
    print(
        f"  {color}{a.status:<18}{RESET} {a.window[-1]} {a.target[:34]:<34} "
        f"{GRAY}{a.backend or '-':<12}{RESET} {miss} {a.total_ms:6.0f}ms "
        f"{GRAY}{a.detail[:70]}{RESET}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Layout generation helpers
# ---------------------------------------------------------------------------
def place_rects(
    rng: random.Random, sizes: list[tuple[int, int]], area: tuple[int, int], margin: int = 16
) -> list[tuple[int, int]]:
    """Non-overlapping top-left positions for boxes of the given sizes."""
    aw, ah = area
    placed: list[tuple[int, int, int, int]] = []
    out: list[tuple[int, int]] = []
    for w, h in sizes:
        for _ in range(500):
            x = rng.randint(margin, max(margin, aw - w - margin))
            y = rng.randint(margin, max(margin, ah - h - margin))
            box = (x - margin, y - margin, x + w + margin, y + h + margin)
            if all(
                box[2] <= p[0] or p[2] <= box[0] or box[3] <= p[1] or p[3] <= box[1] for p in placed
            ):
                placed.append(box)
                out.append((x, y))
                break
        else:
            out.append((margin, margin))
    return out


def text_box_estimate(label: str, size_pt: int) -> tuple[int, int]:
    px = size_pt * 96 / 72
    return int(len(label) * px * 0.62) + 6, int(px * 1.5) + 4


def make_glyph_pngs(bg: str) -> list[tuple[str, int]]:
    """Six small distinct glyphs as PNGs (drawn on the window bg so no alpha)."""
    TMP.mkdir(parents=True, exist_ok=True)
    specs = [
        ("tri", 16, "#1f3a93"),
        ("cross", 16, "#8e1a1a"),
        ("ring", 16, "#146b3a"),
        ("diamond", 12, "#5a2d82"),
        ("chevron", 12, "#b35c00"),
        ("bar", 12, "#004d61"),
    ]
    out = []
    for name, s, color in specs:
        img = Image.new("RGB", (s, s), bg)
        d = ImageDraw.Draw(img)
        m = s - 1
        if name == "tri":
            d.polygon([(0, m), (m, m), (s // 2, 0)], fill=color)
        elif name == "cross":
            t = max(2, s // 5)
            d.rectangle([s // 2 - t // 2, 0, s // 2 + t // 2, m], fill=color)
            d.rectangle([0, s // 2 - t // 2, m, s // 2 + t // 2], fill=color)
        elif name == "ring":
            d.ellipse([0, 0, m, m], outline=color, width=max(2, s // 5))
        elif name == "diamond":
            d.polygon([(s // 2, 0), (m, s // 2), (s // 2, m), (0, s // 2)], fill=color)
        elif name == "chevron":
            d.polygon(
                [(0, 0), (s // 2, 0), (m, s // 2), (s // 2, m), (0, m), (s // 2, s // 2)],
                fill=color,
            )
        elif name == "bar":
            d.rectangle([0, s // 3, m, 2 * s // 3], fill=color)
            d.rectangle([s // 3, 0, 2 * s // 3, m], fill=bg)
            d.rectangle([s // 3, 0, 2 * s // 3, s // 3 - 1], fill=color)
        p = TMP / f"glyph_{name}_{s}.png"
        img.save(p)
        out.append((str(p), s))
    return out


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------
class UserTookMouse(RuntimeError):
    """The human moved the cursor between attempts: stop immediately."""


class Ctx:
    def __init__(self, windows: list[GauntletWindow], rng: random.Random, args):
        self.windows = windows
        self.rng = rng
        self.args = args
        self.attempts: list[Attempt] = []
        # Quiet clicks never move the cursor, so any movement means the user
        # is back; start the guard from wherever the pointer is now.
        self.last_mouse_xy: tuple[int, int] | None = input_mod.position()
        self.click_method: str = getattr(args, "click_method", "auto")
        self.mouse_used = False

    def _check_user_has_not_taken_mouse(self) -> None:
        # Only the real-mouse path competes with the user for the cursor.
        # Quiet delivery never touches it, so the user may move it freely.
        if self.click_method != "mouse" and not self.mouse_used:
            return
        # Our own SendInput resets the OS idle timer, so idle time cannot tell
        # us the user is back. The cursor position can: we leave it where the
        # last geometric click landed, and nothing else should move it.
        if self.last_mouse_xy is None:
            return
        x, y = input_mod.position()
        lx, ly = self.last_mouse_xy
        if abs(x - lx) > 40 or abs(y - ly) > 40:
            raise UserTookMouse(
                f"cursor moved from {self.last_mouse_xy} to {(x, y)} between attempts"
            )

    def run(self, *a, **kw) -> Attempt:
        self._check_user_has_not_taken_mouse()
        kw.setdefault("click_method", self.click_method)
        att = run_attempt(*a, windows=self.windows, **kw)
        if att.cursor_moved and att.cursor_after:
            # Guard against where the pointer was LEFT (click() restores it to
            # the user's position), not the click point: comparing against
            # the click point tripped the guard on our own restore.
            self.mouse_used = True
            self.last_mouse_xy = (int(att.cursor_after[0]), int(att.cursor_after[1]))
        self.attempts.append(att)
        _print_attempt(att)
        return att


def level_tiny_text(ctx: Ctx) -> None:
    win = ctx.windows[0]
    rng = ctx.rng
    sizes_pt = [7, 8, 8, 9, 9, 10, 11, 11]
    labels = rng.sample(WORDS, len(sizes_pt))
    boxes = [text_box_estimate(lb, pt) for lb, pt in zip(labels, sizes_pt, strict=False)]
    pos = place_rects(rng, boxes, win.canvas_area(), margin=18)
    targets = [
        {
            "id": f"txt_{i}",
            "kind": "text",
            "x": x,
            "y": y,
            "label": lb,
            "size": pt,
            "color": TEXT_COLOR,
        }
        for i, ((x, y), lb, pt) in enumerate(zip(pos, labels, sizes_pt, strict=False))
    ]
    win.layout_targets(targets)
    for t in targets:
        att = ctx.run("tiny_text", win, t["label"], t["id"])
        att.notes.append(f"{t['size']}pt")


def level_color_dots(ctx: Ctx) -> None:
    win = ctx.windows[0]
    rng = ctx.rng
    sizes = [24, 16, 12, 8, 6, 4, 3, 2]
    pos = place_rects(rng, [(s, s) for s in sizes], win.canvas_area(), margin=20)
    targets = [
        {"id": f"dot_{s}", "kind": "dot", "x": x, "y": y, "w": s, "h": s, "color": DOT_COLORS[i]}
        for i, (s, (x, y)) in enumerate(zip(sizes, pos, strict=False))
    ]
    win.layout_targets(targets)
    for t in targets:
        att = ctx.run("color_dots", win, f"color:{t['color']}", t["id"])
        att.notes.append(f"{t['w']}px")


def shade(hexcol: str, dr: int, dg: int, db: int) -> str:
    """Shift a color by (dr, dg, db) staying inside 0..255 by flipping the
    direction when a channel would clamp. A clamped shade can collide with
    the base color (red of #ff3b30 is already 255), which made one decoy
    identical to the target in the first live run."""
    r, g, b = int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16)

    def step(v: int, d: int) -> int:
        if d == 0:
            return v
        return v + d if 0 <= v + d <= 255 else v - d

    return f"#{step(r, dr):02x}{step(g, dg):02x}{step(b, db):02x}"


def level_decoys(ctx: Ctx) -> None:
    win = ctx.windows[0]
    rng = ctx.rng

    for round_no, delta in enumerate((40, 20)):
        base = DOT_COLORS[round_no]
        cols = [base, shade(base, delta, 0, 0), shade(base, 0, -delta, 0), shade(base, 0, 0, delta)]
        pos = place_rects(rng, [(10, 10)] * 4, win.canvas_area(), margin=24)
        targets = []
        for i, ((x, y), col) in enumerate(zip(pos, cols, strict=False)):
            targets.append(
                {
                    "id": f"d{round_no}_{'target' if i == 0 else f'decoy{i}'}",
                    "kind": "dot",
                    "x": x,
                    "y": y,
                    "w": 10,
                    "h": 10,
                    "color": col,
                    "decoy": i != 0,
                }
            )
        win.layout_targets(targets)
        att = ctx.run("decoys", win, f"color:{base}", targets[0]["id"])
        att.notes.append(f"delta={delta}")


def level_multi_window(ctx: Ctx) -> None:
    if len(ctx.windows) < 2:
        print(f"  {GRAY}multi_window needs >= 2 windows, skipped{RESET}")
        return
    rng = ctx.rng
    cols = DOT_COLORS[3:6]
    pos = place_rects(rng, [(10, 10)] * 3, ctx.windows[0].canvas_area(), margin=30)
    targets = [
        {"id": f"mw_{i}", "kind": "dot", "x": x, "y": y, "w": 10, "h": 10, "color": c}
        for i, ((x, y), c) in enumerate(zip(pos, cols, strict=False))
    ]
    for w in ctx.windows:
        w.layout_targets(targets)  # identical content in every window
    for w in ctx.windows:
        for t in targets:
            ctx.run("multi_window", w, f"color:{t['color']}", t["id"])


def level_overlap(ctx: Ctx) -> None:
    if len(ctx.windows) < 2:
        print(f"  {GRAY}overlap needs >= 2 windows, skipped{RESET}")
        return
    a, b = ctx.windows[0], ctx.windows[1]
    aw, ah = a.canvas_area()
    # Target in the lower-right quadrant of A; B will be parked over it.
    targets = [
        {
            "id": "ov_dot",
            "kind": "dot",
            "x": int(aw * 0.7),
            "y": int(ah * 0.65),
            "w": 12,
            "h": 12,
            "color": DOT_COLORS[0],
        },
        {
            "id": "ov_txt",
            "kind": "text",
            "x": int(aw * 0.62),
            "y": int(ah * 0.8),
            "label": "Render",
            "size": 10,
            "color": TEXT_COLOR,
        },
    ]
    a.layout_targets(targets)
    b.layout_targets([])
    a_bounds = spatial_mod.current_bounds(a.hwnd)
    b_bounds = spatial_mod.current_bounds(b.hwnd)
    try:
        # Park B so it covers A's lower-right quadrant, then bring B on top.
        move_window(b.hwnd, a_bounds.x + a_bounds.width // 2, a_bounds.y + a_bounds.height // 3)
        b.raise_()
        time.sleep(0.2)
        b.refresh_token()
        occluded = spatial_mod.is_occluded(a.hwnd)
        print(f"  {GRAY}A occluded by B: {occluded}{RESET}")
        for t in targets:
            spec = f"color:{t['color']}" if t["kind"] == "dot" else t["label"]
            att = ctx.run("overlap", a, spec, t["id"], raise_first=False)
            att.notes.append("occluded" if occluded else "not_occluded")
            # Put B back on top for the next target.
            b.raise_()
            time.sleep(0.15)
    finally:
        if b_bounds is not None:
            move_window(b.hwnd, b_bounds.x, b_bounds.y)
            b.refresh_token()


def level_whack(ctx: Ctx) -> None:
    win = ctx.windows[0]
    rng = ctx.rng
    aw, ah = win.canvas_area()
    budget = float(ctx.args.whack_seconds)
    rounds = [
        (
            "whack_dot",
            {"id": "wk", "kind": "dot", "x": 50, "y": 50, "w": 10, "h": 10, "color": DOT_COLORS[1]},
            f"color:{DOT_COLORS[1]}",
        ),
        (
            "whack_text",
            {
                "id": "wk",
                "kind": "text",
                "x": 50,
                "y": 50,
                "label": "Zap",
                "size": 10,
                "color": TEXT_COLOR,
            },
            "Zap",
        ),
    ]
    for name, spec, find_spec in rounds:
        win.layout_targets([spec])
        t_end = time.time() + budget
        n = 0
        while time.time() < t_end:
            n += 1
            x = rng.randint(20, aw - 60)
            y = rng.randint(20, ah - 40)
            win.move_target("wk", x, y)
            att = ctx.run("whack", win, find_spec, "wk", raise_first=(n == 1))
            att.notes.append(name)
        print(f"  {GRAY}{name}: {n} cycles in {budget:.0f}s{RESET}")


def level_template(ctx: Ctx) -> None:
    if getattr(ctx.args, "harness", "tk") == "win32":
        print(f"  {GRAY}template level needs image targets; skipped on the win32 harness{RESET}")
        return
    win = ctx.windows[0]
    rng = ctx.rng
    glyphs = make_glyph_pngs(win.bg)
    pos = place_rects(rng, [(s, s) for _, s in glyphs], win.canvas_area(), margin=24)
    targets = [
        {"id": f"gl_{Path(p).stem}", "kind": "glyph", "x": x, "y": y, "png": p}
        for (p, s), (x, y) in zip(glyphs, pos, strict=False)
    ]
    win.layout_targets(targets)
    for t in targets:
        att = ctx.run("template", win, f"image:{t['png']}~0.8", t["id"])
        att.notes.append(f"{Path(t['png']).stem}")


def level_marks(ctx: Ctx) -> None:
    """Set-of-Mark: number everything in the window, then click labels by id.
    No find() at all: the number IS the target."""
    from iris import marks as marks_mod

    win = ctx.windows[0]
    rng = ctx.rng
    sizes_pt = [8, 9, 10, 11, 12, 12]
    labels = rng.sample(WORDS, len(sizes_pt))
    boxes = [text_box_estimate(lb, pt) for lb, pt in zip(labels, sizes_pt, strict=True)]
    pos = place_rects(rng, boxes, win.canvas_area(), margin=22)
    targets = [
        {
            "id": f"mk_{i}",
            "kind": "text",
            "x": x,
            "y": y,
            "label": lb,
            "size": pt,
            "color": TEXT_COLOR,
        }
        for i, ((x, y), lb, pt) in enumerate(zip(pos, labels, sizes_pt, strict=True))
    ]
    win.layout_targets(targets)
    ms = marks_mod.collect_marks(win.token)
    try:  # evidence for any miss: what Iris saw, with its numbers drawn
        from iris import vision as _v

        cap = _v.capture_window(win.hwnd)
        live = spatial_mod.current_bounds(win.hwnd)
        drawn = _v.draw_marks(cap, ms.marks, (live.x, live.y), 1.0)
        drawn.save(TMP / f"marks_seed{ctx.args.seed}.png")
        (TMP / f"marks_seed{ctx.args.seed}.json").write_text(
            json.dumps(
                {
                    "legend": ms.legend(),
                    "targets": [win.ground_truth(t["id"]) for t in targets],
                    "window": live.to_dict(),
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  {GRAY}marks evidence dump failed: {e}{RESET}")
    n_ocr = sum(1 for m in ms.marks if m.source == "ocr")
    print(f"  {GRAY}marks collected: {len(ms.marks)} ({n_ocr} ocr){RESET}")
    exact = 0
    for t in targets:
        label = t["label"].lower()
        # Pick the mark the way a model reading the overlay does: the number
        # sitting ON the label, not an exact OCR spelling. The level still fails
        # when no mark covers the label. Exact spellings are counted so OCR
        # quality stays visible in the log.
        gt = win.ground_truth(t["id"])
        hit = None
        if gt is not None:
            gx, gy = _center(gt["rect"])
            covering = [m for m in ms.marks if m.bbox.contains_point(int(gx), int(gy))]
            covering.sort(key=lambda m: (m.source != "ocr", m.bbox.area))
            hit = covering[0] if covering else None
        if hit is not None and hit.text.strip().lower() == label:
            exact += 1
        if hit is None:
            att = Attempt(
                level="marks",
                window=win.title,
                target=t["label"],
                expect_id=t["id"],
                status="no_mark",
                detail="label not among marks",
            )
            ctx.attempts.append(att)
            _print_attempt(att)
            continue
        att = ctx.run("marks", win, f"mark {hit.id} ({t['label']})", t["id"], mark=hit.id)
        att.notes.append(f"ocr_text={hit.text!r}")
        att.notes.append(f"{t['size']}pt")
    print(f"  {GRAY}labels spelled exactly by OCR: {exact}/{len(targets)}{RESET}")


LEVEL_FNS = {
    "tiny_text": level_tiny_text,
    "color_dots": level_color_dots,
    "decoys": level_decoys,
    "multi_window": level_multi_window,
    "overlap": level_overlap,
    "whack": level_whack,
    "template": level_template,
    "marks": level_marks,
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def summarize(attempts: list[Attempt], level_wall_s: dict[str, float] | None = None) -> dict:
    per_level: dict[str, dict] = {}
    level_wall_s = level_wall_s or {}
    for lvl in LEVELS:
        rows = [a for a in attempts if a.level == lvl]
        if not rows:
            continue
        hits = [a for a in rows if a.ok]
        misses = [a.miss_px for a in hits if a.miss_px is not None]
        statuses: dict[str, int] = {}
        for a in rows:
            statuses[a.status] = statuses.get(a.status, 0) + 1
        backends: dict[str, int] = {}
        for a in rows:
            backends[a.backend or "-"] = backends.get(a.backend or "-", 0) + 1
        d = {
            "attempts": len(rows),
            "correct": len(hits),
            "cursor_moved": sum(1 for a in rows if a.cursor_moved),
            "click_methods": {
                k: sum(1 for a in rows if (a.click_method or "-") == k)
                for k in sorted({a.click_method or "-" for a in rows})
            },
            "correct_rate": round(len(hits) / len(rows), 3),
            "statuses": statuses,
            "backends": backends,
            "miss_px_mean": round(statistics.mean(misses), 2) if misses else None,
            "miss_px_p95": round(sorted(misses)[int(0.95 * (len(misses) - 1))], 2)
            if misses
            else None,
            "miss_px_max": round(max(misses), 2) if misses else None,
            "ms_per_attempt_mean": round(statistics.mean(a.total_ms for a in rows), 0),
            "threshold": THRESHOLDS.get(lvl),
        }
        if lvl == "whack":
            # Wall clock for the whole level (move + find + click + receipt),
            # which is what "how fast can Iris whack" actually means.
            wall = level_wall_s.get(lvl) or (sum(a.total_ms for a in rows) / 1000.0)
            d["hits_per_second"] = round(len(hits) / wall, 2) if wall else None
            d["wall_s"] = round(wall, 1)
        d["pass"] = d["correct_rate"] >= (THRESHOLDS.get(lvl) or 0)
        per_level[lvl] = d
    total = len(attempts)
    correct = sum(1 for a in attempts if a.ok)
    return {
        "total_attempts": total,
        "correct": correct,
        "correct_rate": round(correct / total, 3) if total else 0.0,
        "per_level": per_level,
        "all_pass": all(v["pass"] for v in per_level.values()) if per_level else False,
    }


def print_summary(report: dict) -> None:
    print(f"\n{BOLD}GAUNTLET SUMMARY{RESET}")
    print(f"  total attempts : {report['total_attempts']}")
    print(f"  correct        : {report['correct']}  ({report['correct_rate'] * 100:.1f}%)")
    print(f"\n  {'level':<14}{'ok/n':>7} {'rate':>7} {'miss p95':>9} {'ms/att':>7}  statuses")
    for lvl, d in report["per_level"].items():
        col = GREEN if d["pass"] else RED
        p95 = f"{d['miss_px_p95']:.1f}" if d["miss_px_p95"] is not None else "-"
        extra = f" hps={d['hits_per_second']}" if d.get("hits_per_second") is not None else ""
        extra += f" cursor_moved={d['cursor_moved']} {d['click_methods']}"
        print(
            f"  {col}{lvl:<14}{RESET}{d['correct']:>3}/{d['attempts']:<3} "
            f"{d['correct_rate'] * 100:6.1f}% {p95:>9} {d['ms_per_attempt_mean']:>7.0f}  "
            f"{GRAY}{d['statuses']}{extra}{RESET}"
        )
    verdict = (
        f"{GREEN}ALL LEVELS PASS{RESET}"
        if report["all_pass"]
        else f"{RED}SOME LEVELS FAILED{RESET}"
    )
    print(f"\n  {verdict}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def window_geometries(n: int, monitor: int = 0) -> list[str]:
    """Geometries for n harness windows on one monitor. monitor=0 means the
    primary (list_monitors() is primary-first on every machine, discovered
    not assumed); otherwise a capture_id from screen_info()."""
    if int(monitor) > 0:
        m = spatial_mod.monitor_for_capture_id(int(monitor)).bounds
    else:
        m = spatial_mod.list_monitors()[0]
    gap = 24
    cols = max(1, (m.width - 2 * gap) // (WIN_W + gap))
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x = m.x + gap + c * (WIN_W + gap)
        y = m.y + 90 + r * (WIN_H + 60)
        out.append(f"{WIN_W}x{WIN_H}+{x}+{y}")
    return out


def run_gauntlet(args) -> int:
    levels = [lv.strip() for lv in args.levels.split(",") if lv.strip()] if args.levels else LEVELS
    for lv in levels:
        if lv not in LEVEL_FNS:
            print(f"unknown level {lv!r}; choose from {LEVELS}")
            return 2
    n_windows = max(1, int(args.windows))
    if not wait_for_idle(args.idle):
        print("gave up waiting for idle")
        return 3
    rng = random.Random(args.seed)
    cursor = input_mod.position()
    windows = [
        GauntletWindow(i, g, harness=args.harness)
        for i, g in enumerate(window_geometries(n_windows, int(args.monitor)))
    ]
    print(
        f"{BOLD}Iris Gauntlet{RESET}  windows={n_windows} levels={levels} seed={args.seed} "
        f"harness={args.harness} click_method={args.click_method}"
    )
    ctx = Ctx(windows, rng, args)
    level_wall: dict[str, float] = {}
    aborted = False
    t0 = time.time()
    try:
        for w in windows:
            w.start()
            print(f"{GRAY}  {w.title} hwnd={w.hwnd} at {w.geometry}{RESET}")
        time.sleep(0.4)
        for lv in levels:
            print(f"\n{CYAN}{BOLD}== {lv}{RESET}")
            t_lv = time.time()
            try:
                # Per-level RNG: a level's layout must not depend on how many
                # whack cycles the previous level fit into its time budget.
                ctx.rng = random.Random(f"{args.seed}:{lv}")
                LEVEL_FNS[lv](ctx)
            except UserTookMouse as e:
                print(f"\n  {YELLOW}{BOLD}ABORTED: {e}. Giving the mouse back.{RESET}")
                aborted = True
                level_wall[lv] = time.time() - t_lv
                break
            except Exception as e:
                print(f"  {RED}level crashed: {type(e).__name__}: {e}{RESET}")
                ctx.attempts.append(
                    Attempt(
                        level=lv,
                        window="-",
                        target="-",
                        expect_id="-",
                        status="level_crash",
                        detail=str(e)[:200],
                    )
                )
            finally:
                level_wall[lv] = time.time() - t_lv
    finally:
        try:
            input_mod.move(*cursor)
        except Exception:
            pass
        if not args.keep:
            for w in windows:
                w.stop()
    report = summarize(ctx.attempts, level_wall)
    report["aborted_user_took_mouse"] = aborted
    report["elapsed_s"] = round(time.time() - t0, 1)
    report["windows"] = n_windows
    report["seed"] = args.seed
    report["levels"] = levels
    report["attempts"] = [asdict(a) for a in ctx.attempts]
    out_path = (
        Path(args.report)
        if args.report
        else Path(tempfile.gettempdir()) / "iris_gauntlet_report.json"
    )
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(report)
    print(f"{GRAY}report: {out_path}{RESET}")
    if aborted:
        return 4
    return 0 if report["all_pass"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Iris Gauntlet: multi-window tiny-target bench")
    ap.add_argument("--levels", default="", help="comma list, default all: " + ",".join(LEVELS))
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--idle",
        type=float,
        default=90.0,
        help="seconds of user idle required before moving the mouse (0 = now)",
    )
    ap.add_argument("--whack-seconds", type=float, default=10.0)
    ap.add_argument(
        "--harness",
        default="tk",
        choices=["tk", "win32"],
        help="tk = Tkinter canvas (ignores posted clicks, needs the mouse); win32 = plain Win32 window (message-driven, measures quiet clicks)",
    )
    ap.add_argument(
        "--monitor",
        type=int,
        default=0,
        help="park the windows on this capture_id from screen_info() (0 = primary)",
    )
    ap.add_argument(
        "--click-method",
        default="auto",
        choices=["auto", "quiet", "mouse", "invoke"],
        help="delivery for every click (auto = invoke > message > mouse)",
    )
    ap.add_argument("--keep", action="store_true", help="leave the windows open")
    ap.add_argument("--report", default="", help="JSON report path")
    args = ap.parse_args()
    return run_gauntlet(args)


if __name__ == "__main__":
    sys.exit(main())
