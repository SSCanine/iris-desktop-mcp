"""Action verification: polling helpers."""

from __future__ import annotations

import time
from collections.abc import Callable

from iris import resolver as resolver_mod
from iris.tokens import FocusToken


def _backoff_iter(start_ms: int, max_ms: int, factor: float):
    cur = start_ms
    while True:
        yield cur
        cur = min(int(cur * factor), max_ms)


def wait_for_text(
    token: FocusToken,
    text: str,
    *,
    timeout_ms: int = 3000,
    start_interval_ms: int = 100,
    max_interval_ms: int = 500,
    factor: float = 1.3,
) -> dict:
    """Poll resolver.find for text. Returns {found, elapsed_ms, polls}."""
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    polls = 0
    backoff = _backoff_iter(start_interval_ms, max_interval_ms, factor)
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        polls += 1
        result = resolver_mod.find(token, text, capture_for_handoff=False)
        if result.found:
            return {
                "found": True,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "polls": polls,
                "backend": result.backend,
            }
        sleep_ms = next(backoff)
        time.sleep(sleep_ms / 1000.0)
    return {
        "found": False,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "polls": polls,
    }


def wait_for_no_text(
    token: FocusToken,
    text: str,
    *,
    timeout_ms: int = 3000,
    start_interval_ms: int = 100,
    max_interval_ms: int = 500,
    factor: float = 1.3,
) -> dict:
    """Poll until text is NO LONGER found (e.g. 'Start Recording' disappears after click)."""
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    polls = 0
    backoff = _backoff_iter(start_interval_ms, max_interval_ms, factor)
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        polls += 1
        result = resolver_mod.find(token, text, capture_for_handoff=False)
        if not result.found:
            return {
                "vanished": True,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "polls": polls,
            }
        sleep_ms = next(backoff)
        time.sleep(sleep_ms / 1000.0)
    return {
        "vanished": False,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "polls": polls,
    }


def wait_for_drift(
    token: FocusToken,
    *,
    timeout_ms: int = 3000,
    start_interval_ms: int = 150,
    max_interval_ms: int = 500,
    factor: float = 1.4,
) -> dict:
    """Poll the window's UIA fingerprint until it differs from the initial
    snapshot. Returns as soon as drift is detected (or on timeout).

    Useful as a default verification for click actions: "did the window
    structure change in any way after I clicked?" If the click did nothing,
    the fingerprint stays stable and this times out.

    Falls back gracefully when UIA is unavailable: returns {drifted: false,
    error: "uia_unavailable"}.
    """
    from iris import fingerprint as fp_mod
    from iris import semantic as semantic_mod

    if not semantic_mod.HAS_UIA:
        return {"drifted": False, "error": "uia_unavailable", "elapsed_ms": 0, "polls": 0}

    try:
        initial_dump = semantic_mod.walk_tree(token.hwnd, max_depth=4, max_nodes=80)
        initial_fp = fp_mod.compute_fingerprint(initial_dump)
    except Exception as e:
        return {"drifted": False, "error": f"initial_dump_failed: {e}", "elapsed_ms": 0, "polls": 0}

    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    polls = 0
    backoff = _backoff_iter(start_interval_ms, max_interval_ms, factor)
    t0 = time.perf_counter()
    final_fp = initial_fp
    while time.perf_counter() < deadline:
        polls += 1
        sleep_ms = next(backoff)
        time.sleep(sleep_ms / 1000.0)
        try:
            cur_dump = semantic_mod.walk_tree(token.hwnd, max_depth=4, max_nodes=80)
            cur_fp = fp_mod.compute_fingerprint(cur_dump)
        except Exception:
            continue
        final_fp = cur_fp
        if cur_fp != initial_fp:
            return {
                "drifted": True,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "polls": polls,
                "initial_fingerprint": initial_fp,
                "final_fingerprint": cur_fp,
            }
    return {
        "drifted": False,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "polls": polls,
        "initial_fingerprint": initial_fp,
        "final_fingerprint": final_fp,
    }


def wait_for(
    predicate: Callable[[], bool], *, timeout_ms: int = 3000, interval_ms: int = 100
) -> dict:
    """Generic predicate poll. Returns {ok, elapsed_ms, polls}."""
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    polls = 0
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        polls += 1
        try:
            if predicate():
                return {
                    "ok": True,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "polls": polls,
                }
        except Exception:
            pass
        time.sleep(interval_ms / 1000.0)
    return {
        "ok": False,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "polls": polls,
    }
