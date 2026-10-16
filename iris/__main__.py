"""Iris MCP server entry point.

Run via:
    python -m iris        # uses this module
    iris-mcp              # console script (preferred; matches pyproject)

Both paths land here. server.py at the repo root is a thin shim kept for
people running directly from a source checkout (`python server.py`); pip
installs use this module.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# DPI awareness MUST be set BEFORE pyautogui, win32gui, mss, uiautomation,
# or anything else asks Windows for a coordinate. Once any thread has touched
# a Win32 coord API, the process's DPI mode is locked.
#
# PER_MONITOR_AWARE_V2 (-4) reports physical pixels per monitor, which is the
# only mode that works correctly on mixed-DPI multi-monitor setups (e.g. one
# display at 100%, another at 125%, a third at 150%). Older modes (System
# Aware, PMA_v1) fall back to virtualized coords on the non-primary monitors
# and synthesized clicks miss the visible target by the DPI scale factor.
# ---------------------------------------------------------------------------
def _actual_dpi_mode() -> str | None:
    """Read back the awareness actually in effect for this process. Needed
    because SetProcessDpiAwarenessContext fails once ANY earlier importer
    (bench, tests, an embedding app) has set it, and the old fallthrough then
    misreported a PMv2 process as 'system_aware'."""
    import ctypes

    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        if not ctx:
            return None
        if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-4)):
            return "per_monitor_v2"
        level = user32.GetAwarenessFromDpiAwarenessContext(ctx)
        return {0: "unaware", 1: "system_aware", 2: "per_monitor_v1"}.get(int(level))
    except (AttributeError, OSError, ValueError):
        return None


def _set_dpi_awareness() -> str:
    import ctypes

    try:
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return "per_monitor_v2"
        actual = _actual_dpi_mode()
        if actual:
            return actual
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor_v1"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system_aware"
    except (AttributeError, OSError):
        pass
    return "unaware"


_DPI_MODE = _set_dpi_awareness()

import pyautogui
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage

# When running as a module inside the iris package (pip install or `python -m
# iris`), the package is already importable. The sys.path manipulation that
# the legacy server.py shim performs is unnecessary here.
_HERE = Path(__file__).parent

from iris import fingerprint as fingerprint_mod
from iris import input as input_mod
from iris import launcher as launcher_mod
from iris import marks as marks_mod
from iris import panels as panels_mod
from iris import quiet as quiet_mod
from iris import recipes as recipes_mod
from iris import resolver as resolver_mod
from iris import semantic as semantic_mod
from iris import spatial as spatial_mod
from iris import system as system_mod
from iris import telemetry
from iris import verify as verify_mod
from iris import vision as vision_mod
from iris.geometry import Rect
from iris.self_test import run_self_test as run_self_test_impl
from iris.tokens import (
    FocusToken,
    default_registry,
)
from iris.tokens import (
    inspect as token_inspect,
)
from iris.tokens import (
    revalidate as token_revalidate,
)

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.0


# ---------------------------------------------------------------------------
# Logging - file only. STDOUT/STDERR MUST stay clean for MCP stdio transport.
#
# Log location priority: $IRIS_LOG_DIR -> user data dir (when platformdirs is
# available) -> <repo>/logs (when running from a checkout). This keeps
# pip-installed instances out of site-packages but lets a develop-from-source
# checkout keep its logs co-located with the code.
# ---------------------------------------------------------------------------
def _resolve_log_dir() -> Path:
    env = os.environ.get("IRIS_LOG_DIR")
    if env:
        return Path(env)
    # _HERE is the package dir (<repo>/iris); the repo root is its parent. The
    # old probe looked for <repo>/iris/logs and <repo>/iris/iris, which never
    # exist, so source checkouts silently logged to AppData instead.
    repo_root = _HERE.parent
    repo_logs = repo_root / "logs"
    if (repo_root / "pyproject.toml").exists():
        # Running from a source checkout: keep logs alongside the code.
        return repo_logs
    try:
        from platformdirs import user_log_dir

        return Path(user_log_dir("iris-mcp"))
    except ImportError:
        return repo_logs


LOG_DIR = _resolve_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)
telemetry.configure(LOG_DIR)  # events.jsonl lands next to iris.log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "iris.log", encoding="utf-8")],
)
log = logging.getLogger("iris")


def _agent_guide() -> str | None:
    # Sent to MCP clients as server instructions, so every client (Claude,
    # Codex, ...) gets the usage rules without editing its own config.
    try:
        return (_HERE / "agent_guide.md").read_text(encoding="utf-8")
    except OSError:
        return None


mcp = FastMCP("iris", instructions=_agent_guide())
registry = default_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_token(token_id: str) -> FocusToken:
    tk = registry.get(token_id)
    if tk is None:
        raise ValueError(f"unknown_token:{token_id}")
    return tk


def _point_on_any_monitor(x: int, y: int) -> bool:
    """True if (x, y) is inside any physical monitor's bounds.

    Used as a sanity gate before a click. Catches clicks that would land in
    the dead zone between monitors (with staggered displays a point can be
    inside the virtual desktop rectangle yet on no display) or wildly
    off-screen. Monitor bounds are discovered live, never assumed.
    """
    try:
        for m in spatial_mod.list_monitors():
            if m.contains_point(x, y):
                return True
    except Exception:
        return True  # if we can't enumerate monitors, don't refuse the click
    return False


def _make_token_for_window(info) -> FocusToken:
    monitor = max(spatial_mod.get_monitor_for_window(info.bounds), 0)
    fp = None
    if semantic_mod.HAS_UIA and semantic_mod.supports_uia(info.hwnd, info.pid):
        try:
            dump = semantic_mod.walk_tree(info.hwnd, max_depth=4, max_nodes=80)
            fp = fingerprint_mod.compute_fingerprint(dump)
        except Exception:
            fp = None
    tk = FocusToken.create(
        hwnd=info.hwnd,
        pid=info.pid,
        exe_name=info.exe_name,
        title=info.title,
        monitor_index=monitor,
        bounds=info.bounds,
        fingerprint=fp,
    )
    registry.store(tk)
    return tk


def _record_tool(kind: str):
    """Decorator: append one telemetry event per call with args + result.

    Covers every return path (refusals included) and exceptions, so the
    events log finally says what was asked, what was resolved, and what
    happened. functools.wraps keeps the signature FastMCP introspects.
    """

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                out = fn(*args, **kwargs)
            except Exception as e:
                telemetry.record(
                    kind,
                    args=kwargs,
                    error=f"{type(e).__name__}:{e}",
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
                raise
            summary = (
                {k: v for k, v in out.items() if k != "find_result"}
                if isinstance(out, dict)
                else {"result": str(out)[:120]}
            )
            telemetry.record(
                kind, args=kwargs, elapsed_ms=(time.perf_counter() - t0) * 1000, **summary
            )
            return out

        return wrapper

    return deco


# ===========================================================================
#  V1 BACKWARDS COMPAT TOOLS (keep these stable!)
# ===========================================================================
def _emit_image(
    img,
    origin: tuple[int, int],
    *,
    quality: int,
    optimize_tokens: bool,
    grid: bool,
    label: str,
    marks: list | None = None,
) -> list[Any]:
    """Encode a capture and return [image, meta].

    Every screenshot is shrunk to save tokens. Without `meta` the caller has
    no way to map a point in the picture back to a screen pixel (on a wide
    multi-monitor desktop the shrink factor is 5x or more, and a click at the
    picture coordinates lands nowhere near the target). `grid=True` also
    draws SCREEN coordinates onto the image so they can be read directly.
    `marks` draws numbered Set-of-Mark tags so the caller can click by number.
    """
    cw, ch = img.width, img.height
    if grid or marks:
        s = vision_mod.optimal_scale(cw, ch) if optimize_tokens else 1.0
        if s < 1.0:
            img = img.resize((int(cw * s), int(ch * s)), vision_mod.Image.Resampling.LANCZOS)
        if grid:
            img = vision_mod.draw_grid(img, origin, img.width / cw)
        if marks:
            img = vision_mod.draw_marks(img, marks, origin, img.width / cw)
        data, w, h = vision_mod.encode_jpeg(img, quality=quality, optimize_tokens=False)
    else:
        data, w, h = vision_mod.encode_jpeg(img, quality=quality, optimize_tokens=optimize_tokens)
    meta = vision_mod.capture_meta(origin, (cw, ch), (w, h))
    meta["grid"] = bool(grid)
    log.info("%s %dx%d scale=%.3f origin=%s", label, w, h, meta["scale"], meta["origin"])
    return [MCPImage(data=data, format="jpeg"), meta]


def _region_rect(region: dict[str, int] | None) -> Rect | None:
    if not region:
        return None
    return Rect(
        int(region.get("x", 0)),
        int(region.get("y", 0)),
        int(region.get("width", 800)),
        int(region.get("height", 600)),
    )


@mcp.tool(structured_output=False)
def screenshot(
    monitor: int = 0,
    region: dict[str, int] | None = None,
    quality: int = 60,
    grid: bool = False,
) -> list[Any]:
    """Capture the screen, token-optimized (~1590 tokens). Default tool for seeing the desktop.

    Returns [image, meta]. meta has origin/scale and the formula to turn a
    point in the image into a screen coordinate. On a multi-monitor desktop
    the image is shrunk 3-6x, so NEVER click at image coordinates directly;
    apply meta, or call zoom(x, y) to refine, or use find/click with a token.

    Args:
        monitor: 0 = whole virtual desktop; otherwise a capture_id from
            screen_info() (1 = primary, then top-to-bottom, left-to-right).
        region: Optional {x, y, width, height} to capture a specific area.
        quality: JPEG quality 1-100 (default 60).
        grid: Draw a labeled grid in SCREEN coordinates onto the image.
    """
    bounds = _region_rect(region)
    img = vision_mod.capture(bounds=bounds, monitor=monitor)
    origin = vision_mod.capture_origin(bounds=bounds, monitor=monitor)
    return _emit_image(
        img, origin, quality=quality, optimize_tokens=True, grid=grid, label="screenshot"
    )


@mcp.tool(structured_output=False)
def screenshot_full(
    monitor: int = 0,
    region: dict[str, int] | None = None,
    quality: int = 85,
    grid: bool = False,
) -> list[Any]:
    """Full-resolution screenshot. WARNING: 6K-44K+ tokens. Use only for tiny text.

    Returns [image, meta] (see screenshot). Prefer zoom(x, y) for a small area.

    Args:
        monitor: 0 = whole virtual desktop; otherwise a capture_id from
            screen_info() (1 = primary, then top-to-bottom, left-to-right).
        region: Optional {x, y, width, height}.
        quality: JPEG quality 1-100 (default 85).
        grid: Draw a labeled grid in SCREEN coordinates onto the image.
    """
    bounds = _region_rect(region)
    img = vision_mod.capture(bounds=bounds, monitor=monitor)
    origin = vision_mod.capture_origin(bounds=bounds, monitor=monitor)
    return _emit_image(
        img, origin, quality=quality, optimize_tokens=False, grid=grid, label="screenshot_full"
    )


@mcp.tool(structured_output=False)
def screenshot_window(quality: int = 60, grid: bool = False) -> list[Any]:
    """Capture only the currently-focused window. Huge token savings vs full screen.

    Uses Win32 PrintWindow so the result is the window's actual contents even
    if the window is partially covered by other windows. Returns [image, meta].
    """
    info = spatial_mod.get_foreground_window_info()
    if info is None:
        raise RuntimeError("No foreground window")
    try:
        img = vision_mod.capture_window(info.hwnd)
    except Exception as e:
        log.warning("PrintWindow failed for hwnd=%d (%s), falling back to bounds", info.hwnd, e)
        img = vision_mod.capture(bounds=info.bounds)
    live = spatial_mod.current_bounds(info.hwnd) or info.bounds
    return _emit_image(
        img,
        (live.x, live.y),
        quality=quality,
        optimize_tokens=True,
        grid=grid,
        label=f"screenshot_window '{info.title}'",
    )


@mcp.tool(structured_output=False)
def zoom(x: int, y: int, size: int = 200, magnify: int = 3, grid: bool = True) -> list[Any]:
    """Hone in: full-resolution crop centered on screen (x, y), magnified with a
    fine grid in SCREEN coordinates and a crosshair at (x, y).

    Use after a screenshot to pin down a tiny target (a 4 px dot, a 9 pt
    label, an icon) before clicking: read the coordinate off the grid, then
    click(x, y). Returns [image, meta]; meta.scale == magnify.

    Args:
        x, y: screen coordinates to center on.
        size: crop edge in screen pixels (default 200).
        magnify: integer zoom factor 1-8 (default 3, NEAREST so pixels stay crisp).
        grid: draw the coordinate grid (default True).
    """
    img, meta = vision_mod.zoom_capture(int(x), int(y), size=size, magnify=magnify, grid=grid)
    data, w, h = vision_mod.encode_jpeg(img, quality=85, optimize_tokens=False)
    meta["image_size"] = [w, h]
    log.info("zoom center=(%d,%d) size=%d magnify=%d", x, y, size, magnify)
    return [MCPImage(data=data, format="jpeg"), meta]


@mcp.tool()
def screen_info() -> dict[str, Any]:
    """Monitor topology of THIS machine, discovered live: count, virtual desktop,
    and per monitor its bounds, work area, DPI scale and primary flag.

    Call this first on an unfamiliar device. Iris never assumes a layout: one
    laptop panel or five mixed-DPI displays both work, and a monitor plugged in
    while the server runs shows up on the next call.

    Indexing: `monitors[i].index` (0-based) is what tokens / inspect() report;
    `monitors[i].capture_id` (1-based) is what see(monitor=) / screenshot(monitor=)
    take, where 0 means the whole virtual desktop. Primary is always first.
    `scale` matters for text: a 10 pt label is 1.75x more pixels on a 175%
    monitor than on a 100% one.
    """
    infos = spatial_mod.list_monitor_info(force_refresh=True)
    vd = spatial_mod.virtual_desktop()
    primary = spatial_mod.primary_monitor()
    return {
        "monitor_count": len(infos),
        "virtual_desktop": vd.to_dict(),
        "primary_index": primary.index if primary else None,
        "primary_capture_id": primary.capture_id if primary else None,
        "primary_width": primary.bounds.width if primary else 0,
        "primary_height": primary.bounds.height if primary else 0,
        "monitors": [m.to_dict() for m in infos],
        "how_to_use": (
            "see(monitor=capture_id) or screenshot(monitor=capture_id) for one display; "
            "monitor=0 for everything. Coordinates everywhere are physical screen pixels "
            "(the virtual desktop can start at negative x/y). zoom(x, y) for detail."
        ),
    }


@mcp.tool()
def mouse_pos() -> dict[str, int]:
    """Get current cursor position (physical pixels)."""
    x, y = input_mod.position()
    return {"x": int(x), "y": int(y)}


@mcp.tool()
def mouse_move(x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    """Move cursor to (x, y). duration is honored only when > 0."""
    if duration and float(duration) > 0:
        pyautogui.moveTo(int(x), int(y), duration=float(duration))
    else:
        input_mod.move(int(x), int(y))
    return {"ok": True, "x": int(x), "y": int(y)}


@mcp.tool()
def mouse_click(
    x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
) -> dict[str, Any]:
    """Click the mouse at (x, y) or current position. Atomic SendInput delivery."""
    return input_mod.click(
        x=int(x) if x is not None else None,
        y=int(y) if y is not None else None,
        button=button,
        clicks=int(clicks),
    )


@mcp.tool()
def mouse_drag(
    start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", duration: float = 0.3
) -> dict[str, Any]:
    """Drag the mouse from start to end. Stepped motion so apps detect a drag."""
    return input_mod.drag(
        int(start_x),
        int(start_y),
        int(end_x),
        int(end_y),
        button=button,
        duration_ms=int(max(0.0, float(duration)) * 1000),
    )


@mcp.tool()
def mouse_scroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    """Scroll the mouse wheel. Positive = up, negative = down."""
    return input_mod.scroll(
        int(amount),
        x=int(x) if x is not None else None,
        y=int(y) if y is not None else None,
    )


@mcp.tool()
def type_text(text: str, interval: float = 0.0) -> dict[str, Any]:
    """Type text as keyboard input."""
    pyautogui.write(str(text), interval=max(0.0, float(interval)))
    return {"ok": True, "chars": len(text)}


@mcp.tool()
def press_key(key: str, modifiers: list[str] | None = None) -> dict[str, Any]:
    """Press a single key, optionally with modifiers held."""
    mods = [m.lower() for m in (modifiers or [])]
    if mods:
        pyautogui.hotkey(*mods, str(key).lower())
    else:
        pyautogui.press(str(key).lower())
    return {"ok": True, "key": key, "modifiers": mods}


@mcp.tool()
def hotkey(keys: list[str]) -> dict[str, Any]:
    """Press a keyboard combination (all keys at once)."""
    if not keys:
        raise ValueError("keys must be non-empty")
    normalized = [str(k).lower() for k in keys]
    pyautogui.hotkey(*normalized)
    return {"ok": True, "keys": normalized}


# ===========================================================================
#  V2 NEW TOOLS
# ===========================================================================
@mcp.tool()
def list_windows(
    filter: dict | None = None, visible_only: bool = True, titled_only: bool = True
) -> dict[str, Any]:
    """List top-level windows. Optionally filter by spec dict.

    filter spec: {process, title_contains, title_regex, hwnd, pid, title}
    """
    pool = spatial_mod.enumerate_windows(visible_only=visible_only, titled_only=titled_only)
    if filter:
        pool = spatial_mod.match_window(filter, candidates=pool)
    return {"count": len(pool), "windows": [w.to_dict() for w in pool[:50]]}


@mcp.tool()
def find_window(match: dict) -> dict[str, Any]:
    """Find a window by spec. Returns first match (or none)."""
    wins = spatial_mod.match_window(match)
    if not wins:
        return {"found": False, "match": match}
    w = wins[0]
    return {"found": True, "window": w.to_dict(), "all_matches": len(wins)}


@mcp.tool()
def focus(match: dict, raise_window: bool = False) -> dict[str, Any]:
    """Create a focus token for a window. Subsequent see/find/click can use it.

    Args:
        match: spec dict, e.g. {"process": "obs64.exe", "title_contains": "OBS"}
        raise_window: bring the window to front first
    """
    wins = spatial_mod.match_window(match)
    if not wins:
        return {"ok": False, "error": "no_window_matched", "match": match}
    w = wins[0]
    if raise_window:
        spatial_mod.bring_to_front(w.hwnd)
        # bring_to_front is partly async on Win32 (paint/move from -32000,-32000
        # lags). Poll for fresh bounds so the token reflects post-restore state.
        fresh = spatial_mod.wait_for_window_visible(w.hwnd, timeout_ms=500)
        if fresh is not None:
            w = fresh
    tk = _make_token_for_window(w)
    return {
        "ok": True,
        "token": tk.id,
        "monitor": tk.monitor_index,
        "bounds": tk.bounds_at_creation.to_dict(),
        "title": tk.title_at_creation,
        "exe": tk.exe_name,
        "pid": tk.pid,
        "fingerprint": tk.fingerprint,
        "uia_supported": semantic_mod.supports_uia(tk.hwnd, tk.pid)
        if semantic_mod.HAS_UIA
        else False,
    }


@mcp.tool()
def release(token: str) -> dict[str, Any]:
    """Release a focus token (cleanup)."""
    registry.remove(token)
    vision_mod.clear_ocr_cache(token)
    marks_mod.clear_marks(token)
    return {"ok": True, "released": token}


@mcp.tool()
def inspect(token: str) -> dict[str, Any]:
    """Inspect token state: alive, current bounds, monitor, occluded, popups."""
    tk = _resolve_token(token)
    return token_inspect(tk)


def _capture_token_or_bounds(tk):
    """Capture the actual window via PrintWindow; LIVE bounds fallback if it fails."""
    try:
        return vision_mod.capture_window(tk.hwnd)
    except Exception as e:
        log.warning("capture_window fallback for hwnd=%d: %s", tk.hwnd, e)
        return vision_mod.capture(bounds=tk.current_bounds() or tk.bounds_at_creation)


def _see_impl(
    token: str | None,
    quality: int,
    optimize_tokens: bool,
    grid: bool,
    monitor: int,
    marks: bool = False,
):
    mark_list = None
    legend = None
    notes: list[str] = []
    if token is None:
        img = vision_mod.capture(monitor=monitor)
        origin = vision_mod.capture_origin(monitor=monitor)
        label = "see" if optimize_tokens else "see_full"
        if marks:
            notes.append("marks need a window token: focus() a window, then see(token, marks=True)")
    else:
        tk = _resolve_token(token)
        token_revalidate(tk)
        img = _capture_token_or_bounds(tk)
        live = tk.current_bounds() or tk.bounds_at_creation
        origin = (live.x, live.y)
        label = f"see '{tk.title_at_creation}'"
        if marks:
            ms = marks_mod.collect_marks(tk, img=img)
            mark_list = ms.marks
            legend = ms.legend()
    out = _emit_image(
        img,
        origin,
        quality=quality,
        optimize_tokens=optimize_tokens,
        grid=grid,
        label=label,
        marks=mark_list,
    )
    meta = out[1]
    if legend is not None:
        meta["marks"] = legend
        meta["marks_count"] = len(legend)
        meta["how_to_click"] = (
            "Each numbered tag is an element (cyan = UIA control, magenta = OCR text). "
            "Click one with click(token, mark=<number>); no coordinates needed. "
            "Re-run see(token, marks=True) after the window changes."
        )
    if notes:
        meta["notes"] = notes
    return out


@mcp.tool(structured_output=False)
def see(
    token: str | None = None,
    quality: int = 60,
    grid: bool = False,
    monitor: int = 0,
    marks: bool = False,
) -> list[Any]:
    """See: token-optimized capture. With token = window only. Without = whole desktop.

    Returns [image, meta]. The image is shrunk (often 3-6x on a multi-monitor
    desktop); meta.scale and meta.origin turn image points into screen points.
    Do not click at raw image coordinates. For a small target call zoom(x, y).

    marks=True (needs a token) numbers every element Iris can enumerate (UIA
    controls + OCR text runs), draws the numbers on the image and returns the
    legend in meta.marks. Then click(token, mark=N). This is the most precise
    way to act on what you see: no coordinate estimation at all.

    With a token, uses Win32 PrintWindow so the capture is the actual window
    contents even when the window is occluded by other windows.

    Args:
        grid: draw a labeled grid in SCREEN coordinates onto the image.
        monitor: without a token, 0 = whole virtual desktop, else a capture_id
            from screen_info() (1 = primary). Discovered per machine.
        marks: Set-of-Mark overlay + legend (token required).
    """
    return _see_impl(token, quality, True, grid, monitor, marks)


@mcp.tool(structured_output=False)
def see_full(
    token: str | None = None,
    quality: int = 85,
    grid: bool = False,
    monitor: int = 0,
    marks: bool = False,
) -> list[Any]:
    """Full-resolution capture. With token = window. Without = whole desktop.

    Returns [image, meta]. Expensive on a full desktop; prefer see() then
    zoom(x, y) for detail. marks=True as in see().
    """
    return _see_impl(token, quality, False, grid, monitor, marks)


@mcp.tool()
def list_marks(token: str) -> dict[str, Any]:
    """Enumerate and number the window's elements WITHOUT an image: the same
    legend see(token, marks=True) returns (id, text, role, source, bbox,
    center). Cheap way to refresh the numbers after a change, or to pick a
    mark by text. Then click(token, mark=N)."""
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"ok": False, "error": "token_dead", "token": token}
    ms = marks_mod.collect_marks(tk)
    return {
        "ok": True,
        "token": token,
        "count": len(ms.marks),
        "marks": ms.legend(),
        "window_bounds": ms.window_bounds.to_dict(),
    }


@mcp.tool()
def find(
    token: str,
    target: str,
    fuzzy: bool = True,
    threshold: float = 0.6,
    prefer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Find target inside the focused window.

    Target forms:
      - plain text: UIA name match (ranked by exactness + role), then OCR
        (native, then 2x upscale for tiny text), then vision handoff.
      - color:#RRGGBB[~tol][@min-max]: pixel search for a color blob, e.g.
        color:#ff3b30~30@4-400 (tolerance 30, area 4..400 px). For dots,
        indicators, canvas widgets with no text.
      - image:<png path>[~thr]: template match for an icon/glyph.

    prefer_roles (e.g. ["Button"], ["MenuItem", "TabItem"]) boosts UIA controls
    of those roles: use it in text-heavy apps where the target word also
    appears in list rows or messages. Results carry `ambiguous: true` when
    more than one hit matched; check hits[0] before clicking.
    """
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"found": False, "error": "token_dead", "token": token}
    result = resolver_mod.find(
        tk, target, fuzzy=fuzzy, threshold=threshold, prefer_roles=prefer_roles
    )
    return result.to_dict()


@mcp.tool()
@_record_tool("click")
def click(
    token: str | None = None,
    target: str | None = None,
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    verify: bool = False,
    verify_text: str | None = None,
    verify_text_disappears: bool = False,
    verify_timeout_ms: int = 2000,
    prefer_invoke: bool = True,
    prefer_roles: list[str] | None = None,
    mark: int | None = None,
    click_method: str = "auto",
    restore_cursor: bool = True,
) -> dict[str, Any]:
    """Click. Four call modes:
    - click(token, mark=17) - click the element numbered 17 by the last
      see(token, marks=True) / list_marks(token). Most precise: no coordinates.
    - click(token, target='Save') - find target in focused window then click center
      target also accepts the pixel grammar: 'color:#ff3b30~30@4-400' or
      'image:C:/icons/gear.png' (see find). prefer_roles=["Button"] steers
      UIA matches away from list rows / message text that contain the word.
    - click(x, y) - direct screen coords
    - click(token) - click center of the focused window

    Delivery (click_method):
    - "auto" (default): UIA Invoke when the element exposes it, else a QUIET
      click: mouse messages posted straight to the target window at exact
      client coordinates. The cursor never moves, focus never changes, and a
      covered window still receives it. Falls back to the real mouse only for
      click(x, y) without a token. Result says which: click_method =
      uia_pattern | message | mouse.
    - "quiet": Invoke or message only; errors instead of touching the mouse.
    - "mouse": real SendInput click (cursor moves, window is raised). Use for
      apps that ignore posted messages (games, some DirectX / custom-input
      apps). restore_cursor=True puts the pointer back afterwards.
    - "invoke": UIA pattern only; errors if the element has none.
    Verify clicks whose effect matters (verify=True): a quiet click is queued
    to the app, and a few apps ignore posted messages.

    Closed-loop verification (when verify=True and a token is provided):
    - If verify_text is given AND verify_text_disappears=True, wait for that
      text to vanish (e.g. click 'Stop Recording' then wait for 'Stop
      Recording' to disappear).
    - Else if verify_text is given, wait for that text to appear (default for
      verify_text path).
    - Else, wait for the window's UIA fingerprint to change. This catches
      ANY structural effect of the click (button disappeared, label changed,
      modal closed, etc.) without the caller needing to name a specific
      post-state.

    The result includes `verified: {...}` with the poll outcome. Verification
    failure does NOT mark the click as failed (we still clicked), it just
    surfaces the timeout so the caller can decide.
    """
    if button not in ("left", "right", "middle"):
        raise ValueError("button must be left/right/middle")
    if click_method not in ("auto", "quiet", "mouse", "invoke"):
        raise ValueError("click_method must be auto/quiet/mouse/invoke")

    target_x, target_y = None, None
    backend = "direct"
    tk: FocusToken | None = None
    invoke_control: object | None = None  # populated when UIA invoke is viable
    resolved: dict | None = None  # what find() picked, echoed back for transparency
    marks_stale = False

    if x is not None and y is not None:
        target_x, target_y = int(x), int(y)
        if token is not None:
            tk = _resolve_token(token)  # keep token for verify, but coords are raw
    elif token is not None and mark is not None:
        tk = _resolve_token(token)
        if not token_revalidate(tk):
            return {"ok": False, "error": "token_dead"}
        ms = marks_mod.get_marks(tk.id)
        if ms is None:
            return {
                "ok": False,
                "error": "no_marks",
                "hint": "call see(token, marks=True) or list_marks(token) first, then click(token, mark=N)",
            }
        m = ms.get(int(mark))
        if m is None:
            return {
                "ok": False,
                "error": "unknown_mark",
                "mark": int(mark),
                "marks_available": len(ms.marks),
                "hint": "ids restart at 1 on every see(marks=True)/list_marks; re-run it after the window changed",
            }
        marks_stale = marks_mod.is_stale(ms, tk)
        target_x, target_y = m.center
        backend = f"mark:{m.source}"
        target = f"mark:{m.id}"  # so the window clamp below applies
        resolved = {"mark": m.id, "text": m.text, "role": m.role, "bbox": m.bbox.to_dict()}
        if prefer_invoke and button == "left" and clicks == 1 and m.control is not None:
            try:
                if semantic_mod.is_invoke_trusted(m.control):
                    invoke_control = m.control
            except Exception:
                invoke_control = None
    elif token is not None:
        tk = _resolve_token(token)
        if not token_revalidate(tk):
            return {"ok": False, "error": "token_dead"}
        if target is not None:
            r = resolver_mod.find(tk, target, prefer_roles=prefer_roles)
            if not r.found:
                return {"ok": False, "error": "target_not_found", "find_result": r.to_dict()}
            top = r.hits[0]
            b = top.get("bbox") or top.get("bounds")
            target_x = b["x"] + b["width"] // 2
            target_y = b["y"] + b["height"] // 2
            backend = r.backend
            resolved = {
                "text": top.get("text") or top.get("name"),
                "role": top.get("role") or top.get("widget_role"),
                "bbox": b,
                "candidates": len(r.hits),
            }
            # Only meaningful for single-click left-clicks; right-click and
            # double-click semantics aren't captured by UIA patterns.
            if prefer_invoke and button == "left" and clicks == 1 and r.controls:
                ctrl = r.controls[0]
                if ctrl is not None and semantic_mod.is_invoke_trusted(ctrl):
                    invoke_control = ctrl
        else:
            live = tk.current_bounds() or tk.bounds_at_creation
            cx, cy = live.center
            target_x, target_y = cx, cy
    else:
        raise ValueError("must provide x+y, or token (with optional target)")

    # Pre-flight clamp / sanity check. A click computed from a stale token or
    # a multi-monitor coord bug will land somewhere unexpected. Refuse rather
    # than fire blind. When the caller passed token + target, the click MUST
    # land inside the token's current window; that's the whole contract.
    if tk is not None and target is not None:
        live = tk.current_bounds()
        if live is None:
            return {
                "ok": False,
                "error": "window_disappeared",
                "x": target_x,
                "y": target_y,
            }
        if not live.contains_point(target_x, target_y):
            wc = live.center
            return {
                "ok": False,
                "error": "click_outside_window",
                "x": target_x,
                "y": target_y,
                "window_bounds": live.to_dict(),
                "window_center": [wc[0], wc[1]],
                "backend": backend,
                "resolved": resolved,
                "hint": "target resolved to coords outside the token's current "
                "window. Window may have moved or resized since focus. "
                f"Try focus() then click() again, or see(token) to re-check. "
                f"Window center is ({wc[0]}, {wc[1]}).",
            }
    # For raw click(x, y) and click(token) (no target), don't refuse but warn
    # if obviously off-screen (outside virtual desktop).
    if invoke_control is None and not _point_on_any_monitor(target_x, target_y):
        return {
            "ok": False,
            "error": "click_off_screen",
            "x": target_x,
            "y": target_y,
            "hint": "coords are outside the virtual desktop (no monitor covers this point).",
        }

    # ------------------------------------------------------------------
    # Delivery. Order of preference, all without touching the shared cursor:
    #   1. UIA pattern (Invoke/Toggle/Select/Expand) on the resolved control
    #   2. window messages posted to the target window ("quiet" click)
    #   3. the real mouse (SendInput), only for click(x, y) without a token
    #      or when the caller asked for click_method="mouse"
    # ------------------------------------------------------------------
    out: dict[str, Any] = {}
    invoke_result: dict | None = None
    delivered = False
    base = {"x": target_x, "y": target_y, "backend": backend}

    if invoke_control is not None and click_method in ("auto", "quiet", "invoke"):
        invoke_result = semantic_mod.try_pattern_click(invoke_control)
        if invoke_result.get("ok"):
            out = {
                "ok": True,
                **base,
                "backend": f"{backend}+invoke",
                "click_method": "uia_pattern",
                "pattern": invoke_result["pattern"],
            }
            delivered = True
        else:
            log.info("uia_invoke_failed: %s", invoke_result)
    if not delivered and click_method == "invoke":
        return {
            "ok": False,
            "error": "invoke_failed" if invoke_result else "no_invoke_pattern",
            **base,
            "invoke_attempt": invoke_result,
            "hint": "the element has no usable UIA pattern; use click_method='auto'",
        }

    quiet_result: dict | None = None
    if not delivered and tk is not None and click_method in ("auto", "quiet"):
        mr = quiet_mod.message_click_checked(
            tk.hwnd,
            target_x,
            target_y,
            button=button,
            clicks=int(clicks),
            force=(click_method == "quiet"),
        )
        quiet_result = mr
        if mr.get("ok"):
            out = {
                "ok": True,
                **base,
                "click_method": "message",
                "target_hwnd": mr.get("target_hwnd"),
                "target_class": mr.get("class"),
                "client": mr.get("client"),
                "reaction": mr.get("reaction"),
            }
            if invoke_result:
                out["invoke_attempt"] = invoke_result
            delivered = True
        elif click_method == "quiet":
            return {
                "ok": False,
                "error": "quiet_click_failed",
                **base,
                "detail": mr,
                "hint": "use click_method='mouse' if this app needs real input",
            }
    if not delivered and click_method == "quiet":
        return {
            "ok": False,
            "error": "quiet_needs_token",
            **base,
            "hint": "quiet clicks post messages to a window: pass a token (focus() first)",
        }

    raised_before_click = False
    if not delivered:
        # A real mouse click lands on whatever is on TOP at that point. find()
        # sees through occlusion (PrintWindow), so raise our window first.
        if tk is not None and target is not None:
            try:
                if spatial_mod.is_occluded(tk.hwnd):
                    spatial_mod.bring_to_front(tk.hwnd)
                    time.sleep(0.05)
                    raised_before_click = True
            except Exception:
                pass
        mr = input_mod.click(
            x=target_x,
            y=target_y,
            button=button,
            clicks=int(clicks),
            restore_cursor=bool(restore_cursor),
        )
        method = "mouse"
        if quiet_result is not None:
            method = f"mouse_after_quiet_{quiet_result.get('reason', 'failed')}"
        elif invoke_result:
            method = "mouse_after_invoke_failed"
        out = {"ok": True, **base, "click_method": method}
        if quiet_result is not None:
            out["quiet_attempt"] = {
                k: quiet_result.get(k) for k in ("reason", "class", "top_class", "verdict")
            }
        if invoke_result:
            out["invoke_attempt"] = invoke_result
        if mr.get("cursor_restored_to"):
            out["cursor_restored_to"] = mr["cursor_restored_to"]
    if resolved is not None:
        out["resolved"] = resolved
        if resolved.get("candidates", 1) > 1:
            out["ambiguous"] = True
    if raised_before_click:
        out["raised_before_click"] = True
    if marks_stale:
        out["marks_stale"] = True
        out["hint"] = (
            "the window changed since these marks were taken; the click went to the "
            "remembered position. Re-run see(token, marks=True) to renumber."
        )
    if verify and token is not None:
        tk = _resolve_token(token)
        if verify_text:
            if verify_text_disappears:
                out["verified"] = verify_mod.wait_for_no_text(
                    tk,
                    verify_text,
                    timeout_ms=verify_timeout_ms,
                )
                out["verify_mode"] = "text_disappears"
            else:
                out["verified"] = verify_mod.wait_for_text(
                    tk,
                    verify_text,
                    timeout_ms=verify_timeout_ms,
                )
                out["verify_mode"] = "text_appears"
        else:
            out["verified"] = verify_mod.wait_for_drift(
                tk,
                timeout_ms=verify_timeout_ms,
            )
            out["verify_mode"] = "fingerprint_drift"
    return out


@mcp.tool()
def launch(app: str, wait_seconds: float = 5.0) -> dict[str, Any]:
    """Launch an app from apps.yaml registry. Returns its window info if it opens."""
    return launcher_mod.launch(app, wait_seconds=wait_seconds)


@mcp.tool()
def list_apps() -> dict[str, Any]:
    """List apps registered in apps.yaml."""
    return launcher_mod.list_apps()


@mcp.tool()
def discover(token: str) -> dict[str, Any]:
    """Full ground-truth dump of a window: UIA tree + OCR text + screenshot."""
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"error": "token_dead"}
    out: dict[str, Any] = {"window": token_inspect(tk)}
    # UIA tree
    if semantic_mod.HAS_UIA:
        try:
            tree = semantic_mod.walk_tree(tk.hwnd, max_depth=6, max_nodes=300)
            out["uia_tree"] = tree
            out["fingerprint"] = fingerprint_mod.compute_fingerprint(tree)
        except Exception as e:
            out["uia_error"] = str(e)
    # OCR (PrintWindow capture so occluded windows still produce real text)
    img = _capture_token_or_bounds(tk)
    if vision_mod._TESSERACT_OK:
        try:
            words = vision_mod.cached_ocr(tk.id, img)
            out["ocr_text"] = [w.to_dict() for w in words]
        except Exception as e:
            out["ocr_error"] = str(e)
    # Screenshot
    try:
        data, w, h = vision_mod.encode_jpeg(img, quality=70, optimize_tokens=True)
        out["screenshot_bytes_len"] = len(data)
        out["screenshot_dims"] = [w, h]
    except Exception as e:
        out["screenshot_error"] = str(e)
    return out


@mcp.tool()
def suggest_alternatives(token: str, target: str, top_n: int = 10) -> dict[str, Any]:
    """When a target wasn't found, get ranked candidates that might be what you meant."""
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"error": "token_dead"}
    return resolver_mod.suggest_alternatives(tk, target, top_n=top_n)


@mcp.tool()
def discover_panels(token: str) -> dict[str, Any]:
    """List dock/panel containers in the focused window with bounds + confidence.

    Generic across Qt (QDockWidget, OBSDock), Win32, and Electron. Each panel
    reports id, name, automation_id, bounds, confidence, hidden, tabbed.
    """
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"error": "token_dead"}
    tree: list[dict] = []
    if semantic_mod.HAS_UIA:
        try:
            tree = semantic_mod.walk_tree(tk.hwnd, max_depth=6, max_nodes=300)
        except Exception as e:
            return {"error": f"uia_walk_failed:{e}"}
    ocr_words: list[dict] = []
    if vision_mod._TESSERACT_OK:
        try:
            img = _capture_token_or_bounds(tk)
            words = vision_mod.cached_ocr(tk.id, img)
            ocr_words = [w.to_dict() for w in words]
        except Exception:
            ocr_words = []
    win_bounds = tk.bounds_at_creation.to_dict()
    panels = panels_mod.discover_panels(tree, ocr_words, win_bounds)
    return {"panels": panels, "count": len(panels)}


@mcp.tool()
def discover_panel_items(token: str, panel: str) -> dict[str, Any]:
    """List actionable items inside a named panel (audio sources, scenes, controls...).

    panel: id, automation_id, or display name (substring match).
    """
    tk = _resolve_token(token)
    if not token_revalidate(tk):
        return {"error": "token_dead"}
    if not semantic_mod.HAS_UIA:
        return {"found": False, "reason": "uia_unavailable"}
    try:
        tree = semantic_mod.walk_tree(tk.hwnd, max_depth=8, max_nodes=600)
    except Exception as e:
        return {"error": f"uia_walk_failed:{e}"}
    return panels_mod.discover_panel_items(tree, panel)


@mcp.tool()
def wait_for(token: str, target: str, timeout_ms: int = 3000) -> dict[str, Any]:
    """Wait until target appears in focused window. Polls with backoff."""
    tk = _resolve_token(token)
    return verify_mod.wait_for_text(tk, target, timeout_ms=timeout_ms)


@mcp.tool()
def iris_status() -> dict[str, Any]:
    """Diagnostics: backend availability, OCR, UIA, cache stats, version."""
    from iris._version import __version__
    from iris.tesseract_bootstrap import locate_tesseract

    tess = locate_tesseract()
    return {
        "version": __version__,
        "win32": spatial_mod.HAS_WIN32,
        "uia": semantic_mod.HAS_UIA,
        "tesseract_ok": vision_mod._TESSERACT_OK,
        "tesseract_path": str(tess) if tess else None,
        "active_tokens": len(registry.all()),
        "ocr_cache": vision_mod.ocr_cache_stats(),
        "ocr_psm": vision_mod.OCR_PSM_DEFAULT,
        "pixel_backend": {
            "numpy": __import__("iris.pixel", fromlist=["HAS_NUMPY"]).HAS_NUMPY,
            "cv2": __import__("iris.pixel", fromlist=["HAS_CV2"]).HAS_CV2,
        },
        "dpi_mode": _DPI_MODE,
        "monitors": len(spatial_mod.list_monitor_info()),
        "virtual_desktop": spatial_mod.virtual_desktop().to_dict(),
        "mark_sets": len(marks_mod._SETS),
        "click_delivery": "uia_pattern > message (quiet) > mouse",
        "log_dir": str(LOG_DIR),
        "events_log": str(telemetry.events_path()),
    }


@mcp.tool()
def events_tail(n: int = 30) -> dict[str, Any]:
    """Last n find/click telemetry events (what was asked, which backend
    answered, where the click landed, refusals). Use it to diagnose a miss
    without re-running anything."""
    return {"path": str(telemetry.events_path()), "events": telemetry.tail(int(n))}


@mcp.tool()
def self_test() -> dict[str, Any]:
    """Spawn the test harness, run a battery of checks, return a structured report."""
    return run_self_test_impl()


# ===========================================================================
#  SYSTEM TOOLS (clipboard, processes, notifications, window state, registry)
# ===========================================================================
@mcp.tool()
def clipboard_get() -> dict[str, Any]:
    """Read text from the Windows clipboard. Returns {ok, text}."""
    return system_mod.clipboard_get()


@mcp.tool()
def clipboard_set(text: str) -> dict[str, Any]:
    """Write text to the Windows clipboard. Replaces existing contents."""
    return system_mod.clipboard_set(text)


@mcp.tool()
def list_processes(name_contains: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List running processes. Filter by case-insensitive name substring."""
    return system_mod.list_processes(name_contains=name_contains, limit=int(limit))


@mcp.tool()
def find_process(name: str) -> dict[str, Any]:
    """Find processes by exact (case-insensitive) name. Useful before kill."""
    return system_mod.find_process(name)


@mcp.tool()
def kill_process(pid: int, force: bool = False) -> dict[str, Any]:
    """Terminate a process by PID. Requires force=True for safety. Refuses
    to touch kernel/system PIDs (0, 4) even with force=True."""
    return system_mod.kill_process(int(pid), force=bool(force))


@mcp.tool()
def notify(title: str, body: str = "", duration_seconds: float = 5.0) -> dict[str, Any]:
    """Show a Windows toast notification. Requires the `winsdk` package
    for the modern Toast backend."""
    return system_mod.notify(title, body, duration_seconds=float(duration_seconds))


@mcp.tool()
def window_minimize(hwnd: int) -> dict[str, Any]:
    """Minimize a window by hwnd."""
    return system_mod.window_minimize(int(hwnd))


@mcp.tool()
def window_maximize(hwnd: int) -> dict[str, Any]:
    """Maximize a window by hwnd."""
    return system_mod.window_maximize(int(hwnd))


@mcp.tool()
def window_restore(hwnd: int) -> dict[str, Any]:
    """Restore a window to normal size."""
    return system_mod.window_restore(int(hwnd))


@mcp.tool()
def window_close(hwnd: int) -> dict[str, Any]:
    """Politely close a window via WM_CLOSE. App may prompt for save."""
    return system_mod.window_close(int(hwnd))


@mcp.tool()
def registry_read(hive: str, key_path: str, value_name: str = "") -> dict[str, Any]:
    """Read a Windows registry value. hive: HKLM/HKCU/HKCR/HKU/HKCC."""
    return system_mod.registry_read(hive, key_path, value_name)


@mcp.tool()
def registry_list_values(hive: str, key_path: str) -> dict[str, Any]:
    """Enumerate all values under a registry key."""
    return system_mod.registry_list_values(hive, key_path)


@mcp.tool()
def registry_write(
    hive: str,
    key_path: str,
    value_name: str,
    value: Any,
    value_type: str = "REG_SZ",
    confirm: bool = False,
) -> dict[str, Any]:
    """Write a Windows registry value. Requires confirm=True for safety."""
    return system_mod.registry_write(
        hive,
        key_path,
        value_name,
        value,
        value_type=value_type,
        confirm=bool(confirm),
    )


@mcp.tool()
def registry_delete_value(
    hive: str, key_path: str, value_name: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a Windows registry value. Requires confirm=True."""
    return system_mod.registry_delete_value(
        hive,
        key_path,
        value_name,
        confirm=bool(confirm),
    )


# ---------------------------------------------------------------------------
# Recipes: named workflows that chain primitives. See iris/recipes.py.
# Every @mcp.tool() above is auto-registered as a recipe action below so any
# YAML in iris/recipes/ can call them by name.
# ---------------------------------------------------------------------------
def _register_actions_for_recipes() -> None:
    """Expose every @mcp.tool function in this module to the recipe engine."""
    for _name, _obj in list(globals().items()):
        if _name.startswith("_") or _name in ("main", "mcp", "log", "registry"):
            continue
        _fn = getattr(_obj, "fn", None)
        if callable(_fn):
            recipes_mod.register_action(_name, _fn)


_register_actions_for_recipes()


@mcp.tool()
def list_recipes() -> dict[str, Any]:
    """List all available recipes in iris/recipes/."""
    return {"recipes": recipes_mod.list_recipes()}


@mcp.tool()
def run_recipe(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a named recipe (e.g. 'obs.start_recording', 'chrome.open_url').

    Args:
        name: recipe name. Resolves to recipes/{name}.yaml or by the recipe's
              declared `name:` field.
        args: input args the recipe accepts (see each recipe's `inputs:` list).

    Returns a structured trace of each step's result, plus a top-level `ok`
    indicator. On failure, includes the failing step index, action, and error.
    """
    return recipes_mod.run_recipe(name, args=args or {})


# ===========================================================================
#  DRAG / KNOB / HOLD: press-hold-move gestures
#
#  These need the REAL mouse: apps track a drag through live pointer input
#  and mouse capture, and OS drag-and-drop runs its own modal loop, so posted
#  messages do not work. The cursor is restored afterwards and the button and
#  modifier keys are always released, even on error.
# ===========================================================================
def _resolve_point(
    tk: FocusToken | None,
    target: str | None = None,
    mark: int | None = None,
    x: int | None = None,
    y: int | None = None,
    prefer_roles: list[str] | None = None,
) -> tuple[tuple[int, int] | None, dict]:
    """Screen point for a gesture endpoint: explicit (x, y), a numbered mark,
    or a find() target. Returns ((x, y) or None, info)."""
    if x is not None and y is not None:
        return (int(x), int(y)), {"source": "xy"}
    if tk is None:
        return None, {"error": "needs_token", "hint": "target/mark endpoints need a token"}
    if mark is not None:
        ms = marks_mod.get_marks(tk.id)
        m = ms.get(int(mark)) if ms else None
        if m is None:
            return None, {"error": "unknown_mark", "mark": int(mark)}
        info = {"source": "mark", "mark": m.id, "text": m.text, "bbox": m.bbox.to_dict()}
        if marks_mod.is_stale(ms, tk):
            info["marks_stale"] = True
        return m.center, info
    if target is not None:
        r = resolver_mod.find(tk, target, prefer_roles=prefer_roles)
        if not r.found:
            return None, {"error": "target_not_found", "target": target, "notes": r.notes[:2]}
        top = r.hits[0]
        b = top.get("bbox") or top.get("bounds")
        info = {"source": r.backend, "text": top.get("text") or top.get("name"), "bbox": b}
        if len(r.hits) > 1:
            info["ambiguous"] = True
        return (b["x"] + b["width"] // 2, b["y"] + b["height"] // 2), info
    return None, {"error": "no_endpoint", "hint": "give target, mark, or x and y"}


def _gesture_prepare(tk: FocusToken | None, start: tuple[int, int]) -> dict | None:
    """Refuse gestures that start outside the token's window or off-screen, and
    raise the window if covered (a real press lands on whatever is on top)."""
    if tk is not None:
        live = tk.current_bounds()
        if live is None:
            return {"ok": False, "error": "window_disappeared"}
        if not live.contains_point(*start):
            return {"ok": False, "error": "start_outside_window", "start": list(start)}
        try:
            if spatial_mod.is_occluded(tk.hwnd):
                spatial_mod.bring_to_front(tk.hwnd)
                time.sleep(0.05)
        except Exception:
            pass
    if not _point_on_any_monitor(*start):
        return {"ok": False, "error": "start_off_screen", "start": list(start)}
    return None


@mcp.tool()
@_record_tool("drag")
def drag(
    token: str | None = None,
    from_target: str | None = None,
    from_mark: int | None = None,
    from_x: int | None = None,
    from_y: int | None = None,
    to_target: str | None = None,
    to_mark: int | None = None,
    to_x: int | None = None,
    to_y: int | None = None,
    dx: int | None = None,
    dy: int | None = None,
    hold_ms: int = 80,
    speed_px_per_s: int = 900,
    button: str = "left",
    modifiers: list[str] | None = None,
    verify: bool = True,
    prefer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Press, hold, move, release. Drag and drop, sliders, faders, scrubbing,
    selecting a region, moving a clip between windows.

    Start: from_target / from_mark / from_x+from_y. End: to_target / to_mark /
    to_x+to_y, or a relative dx/dy from the start. Targets use the find()
    grammar (text, color:#hex, image:path); marks come from see(marks=True).
    The end may be in another window (drop onto another app).

    hold_ms: pause after pressing before moving (some apps need it to start a
    drag). speed_px_per_s: glide speed. modifiers: e.g. ["shift"] held for the
    whole gesture (fine control in many apps), ["ctrl"] to copy on drop.
    verify=True compares the pixels around the start and end before and after
    and reports `changed`.

    Uses the real mouse (drags cannot be delivered quietly); the cursor goes
    back where it was.
    """
    tk = _resolve_token(token) if token else None
    if tk is not None and not token_revalidate(tk):
        return {"ok": False, "error": "token_dead"}
    start, sinfo = _resolve_point(tk, from_target, from_mark, from_x, from_y, prefer_roles)
    if start is None:
        return {"ok": False, "stage": "from", **sinfo}
    if dx is not None or dy is not None:
        end, einfo = (start[0] + int(dx or 0), start[1] + int(dy or 0)), {"source": "relative"}
    else:
        end, einfo = _resolve_point(tk, to_target, to_mark, to_x, to_y, prefer_roles)
    if end is None:
        return {"ok": False, "stage": "to", **einfo}
    refused = _gesture_prepare(tk, start)
    if refused:
        return refused
    if not _point_on_any_monitor(*end):
        return {"ok": False, "error": "end_off_screen", "end": list(end)}
    before = None
    if verify and tk is not None:
        before = (quiet_mod._roi_digest(tk.hwnd, *start), quiet_mod._roi_digest(tk.hwnd, *end))
    dist = max(abs(end[0] - start[0]), abs(end[1] - start[1]), 1)
    max_step = 6.0
    step_ms = max(1, int(1000 * max_step / max(50, int(speed_px_per_s))))
    res = input_mod.drag_path(
        [start, end],
        button=button,
        hold_ms=int(hold_ms),
        step_ms=step_ms,
        modifiers=modifiers,
        restore_cursor=True,
        max_step_px=max_step,
    )
    out: dict[str, Any] = {
        "ok": bool(res.get("ok")),
        "from": list(start),
        "to": list(end),
        "distance_px": dist,
        "ms": res.get("ms"),
        "from_resolved": sinfo,
        "to_resolved": einfo,
        "modifiers": res.get("modifiers"),
        "click_method": "mouse",
    }
    if before is not None:
        time.sleep(0.12)
        after = (quiet_mod._roi_digest(tk.hwnd, *start), quiet_mod._roi_digest(tk.hwnd, *end))
        out["changed"] = any(a is not None and a != b for a, b in zip(after, before, strict=True))
    return out


@mcp.tool()
@_record_tool("knob")
def knob(
    token: str | None = None,
    target: str | None = None,
    mark: int | None = None,
    x: int | None = None,
    y: int | None = None,
    amount: int = 20,
    mode: str = "vertical",
    fine: bool = False,
    fine_modifier: str = "shift",
    hold_ms: int = 60,
    verify: bool = True,
    prefer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Turn a knob / move a fader or slider on the control at target / mark / x+y.

    mode="vertical" (most knobs and faders): drag up by `amount` px (negative
    = down). mode="horizontal": drag right by `amount` px (negative = left).
    mode="wheel": `amount` wheel notches over the control (positive = up).
    fine=True holds `fine_modifier` (Shift by default) for fine adjustment in
    apps that support it. How many pixels equal one step is app-specific:
    turn a little, look (see/zoom or verify), then adjust.

    Uses the real mouse; the cursor goes back where it was. For apps with a
    real control surface (MIDI/OSC/API, e.g. Traktor), prefer that: it is
    exact and does not touch the desktop.
    """
    if mode not in ("vertical", "horizontal", "wheel"):
        raise ValueError("mode must be vertical/horizontal/wheel")
    tk = _resolve_token(token) if token else None
    if tk is not None and not token_revalidate(tk):
        return {"ok": False, "error": "token_dead"}
    pt, info = _resolve_point(tk, target, mark, x, y, prefer_roles)
    if pt is None:
        return {"ok": False, **info}
    refused = _gesture_prepare(tk, pt)
    if refused:
        return refused
    before = quiet_mod._roi_digest(tk.hwnd, *pt) if (verify and tk is not None) else None
    if mode == "wheel":
        res = input_mod.scroll_at(pt[0], pt[1], int(amount))
        out: dict[str, Any] = {"ok": bool(res.get("ok")), "at": list(pt), "notches": int(amount)}
    else:
        end = (pt[0], pt[1] - int(amount)) if mode == "vertical" else (pt[0] + int(amount), pt[1])
        if not _point_on_any_monitor(*end):
            return {"ok": False, "error": "end_off_screen", "end": list(end)}
        res = input_mod.drag_path(
            [pt, end],
            hold_ms=int(hold_ms),
            step_ms=10,
            modifiers=[fine_modifier] if fine else None,
            restore_cursor=True,
            max_step_px=3.0,
        )
        out = {"ok": bool(res.get("ok")), "at": list(pt), "moved_px": int(amount), "mode": mode}
    out.update({"resolved": info, "fine": bool(fine), "click_method": "mouse"})
    if before is not None:
        time.sleep(0.12)
        after = quiet_mod._roi_digest(tk.hwnd, *pt)
        out["changed"] = after is not None and after != before
    return out


@mcp.tool()
@_record_tool("hold")
def hold(
    token: str | None = None,
    target: str | None = None,
    mark: int | None = None,
    x: int | None = None,
    y: int | None = None,
    ms: int = 600,
    button: str = "left",
    prefer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Press and hold without moving (long-press, press-to-talk, hold-to-repeat
    buttons). Real mouse; cursor restored; always released."""
    tk = _resolve_token(token) if token else None
    pt, info = _resolve_point(tk, target, mark, x, y, prefer_roles)
    if pt is None:
        return {"ok": False, **info}
    refused = _gesture_prepare(tk, pt)
    if refused:
        return refused
    res = input_mod.drag_path(
        [pt, pt], button=button, hold_ms=max(0, min(int(ms), 30000)), step_ms=0, release_ms=0
    )
    return {"ok": bool(res.get("ok")), "at": list(pt), "held_ms": int(ms), "resolved": info}


# ===========================================================================
#  BATCH: many steps, one call
# ===========================================================================
_KEYBOARD_ACTIONS = {"type", "key", "hotkey"}
_TOKEN_ACTIONS = {"click", "find", "wait_for", "marks", "set"}
# Keys dropped from per-step results so a batch reply stays short to read.
_DO_DROP = {"find_result", "hits", "nearest_matches", "window_state", "backends_tried"}


def _step_failed(res: Any) -> str | None:
    if not isinstance(res, dict):
        return None
    if res.get("ok") is False:
        return str(res.get("error") or res.get("reason") or "failed")
    if res.get("found") is False:
        return "not_found"
    if res.get("vanished") is False:
        return "still_present"
    if res.get("error"):
        return str(res["error"])
    return None


def _compact(res: Any) -> Any:
    if not isinstance(res, dict):
        return res
    out = {k: v for k, v in res.items() if k not in _DO_DROP}
    hits = res.get("hits")
    if hits:
        top = hits[0]
        b = top.get("bbox") or top.get("bounds")
        out["top"] = {
            "text": top.get("text") or top.get("name"),
            "role": top.get("role") or top.get("widget_role"),
            "bbox": b,
        }
        out["hits"] = len(hits)
    return out


def _set_value(token: str, target: str, value: str, prefer_roles: list[str] | None) -> dict:
    """Quiet text entry: UIA ValuePattern.SetValue on the matched control.
    No focus change, no keystrokes. Fails (so the caller can fall back to
    type) when the control has no ValuePattern."""
    tk = _resolve_token(token)
    r = resolver_mod.find(tk, target, prefer_roles=prefer_roles or ["Edit", "ComboBox"])
    if not r.found:
        return {"ok": False, "error": "target_not_found", "target": target}
    ctrl = next((c for c in r.controls if c is not None), None)
    if ctrl is None:
        return {
            "ok": False,
            "error": "no_uia_control",
            "hint": "OCR-only target: click it, then type",
        }
    res = semantic_mod.invoke(ctrl, action="set", value=str(value))
    res.setdefault("target", target)
    return res


def _ensure_keyboard_focus(token: str | None) -> dict | None:
    """Keyboard input always goes to the focused window. Quiet clicks do not
    focus, so a keyboard step in a token batch activates the token's window
    first (reported), instead of typing into whatever the user is using."""
    if token is None:
        return None
    tk = _resolve_token(token)
    fg = spatial_mod.get_foreground_window_info()
    if fg is not None and semantic_mod._root_hwnd(fg.hwnd) == semantic_mod._root_hwnd(tk.hwnd):
        return None
    spatial_mod.bring_to_front(tk.hwnd)
    time.sleep(0.05)
    fg = spatial_mod.get_foreground_window_info()
    ok = fg is not None and semantic_mod._root_hwnd(fg.hwnd) == semantic_mod._root_hwnd(tk.hwnd)
    return {"activated": True, "ok": ok}


@mcp.tool()
@_record_tool("do")
def do(
    steps: list[dict],
    token: str | None = None,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    """Run several actions in ONE call. Use it for any known sequence instead
    of one tool call per action: each round trip costs far more than Iris does.

    steps: list of {"action": ..., ...args}. `token` applies to every step
    that takes one unless the step sets its own; a "focus" step replaces it.
      {"action": "focus", "match": {"process": "notepad.exe"}}
      {"action": "click", "target": "Save"} | {"action": "click", "mark": 12}
          (all click() args: x, y, button, clicks, click_method, prefer_roles, verify...)
      {"action": "set", "target": "Search", "value": "hello"}   quiet text entry via UIA
      {"action": "type", "text": "hello"}            keyboard; activates the token window first
      {"action": "key", "key": "enter", "modifiers": ["ctrl"]}
      {"action": "hotkey", "keys": ["ctrl", "s"]}
      {"action": "scroll", "amount": -3, "x": 100, "y": 200}
      {"action": "drag", "from_mark": 4, "to_target": "Deck B"}   (all drag() args)
      {"action": "knob", "target": "color:#ff9500", "amount": 30}  (all knob() args)
      {"action": "hold", "target": "Record", "ms": 800}
      {"action": "wait", "ms": 300}
      {"action": "wait_for", "target": "Saved", "timeout_ms": 3000, "gone": false}
      {"action": "find", "target": "OK"}
      {"action": "marks"}                             refresh Set-of-Mark numbers (text legend)

    Returns {ok, steps: [{i, action, ok, ms, ...compact result}], failed_at,
    token}. Stops at the first failure unless stop_on_error=False.
    """
    t_all = time.perf_counter()
    cur = token
    results: list[dict] = []
    failed_at = None
    for i, raw in enumerate(steps or []):
        step = dict(raw or {})
        action = str(step.pop("action", "")).lower()
        tok = step.pop("token", None) or cur
        t0 = time.perf_counter()
        entry: dict[str, Any] = {"i": i, "action": action}
        try:
            if (
                action in _TOKEN_ACTIONS
                and tok is None
                and not (action == "click" and "x" in step and "y" in step)
            ):
                res = {
                    "ok": False,
                    "error": "needs_token",
                    "hint": "add a focus step or pass token=",
                }
            elif action == "focus":
                res = focus(
                    step.get("match") or {}, raise_window=bool(step.get("raise_window", False))
                )
                if res.get("ok"):
                    cur = res["token"]
            elif action == "click":
                res = click(token=tok, **step)
            elif action == "set":
                res = _set_value(
                    tok, step["target"], step.get("value", ""), step.get("prefer_roles")
                )
            elif action in _KEYBOARD_ACTIONS:
                act = _ensure_keyboard_focus(tok)
                if act is not None:
                    entry["activated_window"] = True
                    if not act["ok"]:
                        res = {"ok": False, "error": "could_not_focus_window_for_keyboard"}
                        raise StopIteration
                if action == "type":
                    res = type_text(step["text"], float(step.get("interval", 0.0)))
                elif action == "key":
                    res = press_key(step["key"], step.get("modifiers"))
                else:
                    res = hotkey(step["keys"])
            elif action == "scroll":
                res = mouse_scroll(int(step["amount"]), step.get("x"), step.get("y"))
            elif action == "drag":
                res = drag(token=tok, **step)
            elif action == "knob":
                res = knob(token=tok, **step)
            elif action == "hold":
                res = hold(token=tok, **step)
            elif action == "wait":
                time.sleep(max(0, min(int(step.get("ms", 250)), 30000)) / 1000.0)
                res = {"ok": True}
            elif action == "wait_for":
                tk = _resolve_token(tok)
                timeout = int(step.get("timeout_ms", 3000))
                if step.get("gone"):
                    res = verify_mod.wait_for_no_text(tk, step["target"], timeout_ms=timeout)
                else:
                    res = verify_mod.wait_for_text(tk, step["target"], timeout_ms=timeout)
            elif action == "find":
                res = find(
                    tok,
                    step["target"],
                    fuzzy=bool(step.get("fuzzy", True)),
                    threshold=float(step.get("threshold", 0.6)),
                    prefer_roles=step.get("prefer_roles"),
                )
            elif action == "marks":
                res = list_marks(tok)
            else:
                res = {"ok": False, "error": f"unknown_action:{action}"}
        except StopIteration:
            pass
        except KeyError as e:
            res = {"ok": False, "error": f"missing_arg:{e.args[0]}"}
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}:{e}"}
        err = _step_failed(res)
        entry.update(_compact(res) if isinstance(res, dict) else {"result": res})
        entry["ok"] = err is None
        if err is not None:
            entry["error"] = err
        entry["ms"] = round((time.perf_counter() - t0) * 1000)
        results.append(entry)
        if err is not None:
            if failed_at is None:
                failed_at = i
            if stop_on_error:
                break
    return {
        "ok": failed_at is None,
        "steps": results,
        "failed_at": failed_at,
        "token": cur,
        "ms": round((time.perf_counter() - t_all) * 1000),
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Iris v2 MCP server starting (pid=%d)", os.getpid())
    log.info("status: %s", iris_status.fn() if hasattr(iris_status, "fn") else "available")
    try:
        mcp.run()
    except Exception:
        log.exception("Iris crashed")
        raise
    finally:
        log.info("Iris stopped")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import json

        print(json.dumps(run_self_test_impl(), indent=2))
        sys.exit(0)
    main()
