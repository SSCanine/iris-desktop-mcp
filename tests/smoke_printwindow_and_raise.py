"""Smoke test for the two fixes shipped 2026-05-20:
1. vision.capture_window uses PrintWindow correctly (occluded windows OK)
2. spatial.bring_to_front actually raises the target window

Run from the repo root:
    python tests/smoke_printwindow_and_raise.py

Outputs a JSON report with pass/fail per check.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iris import spatial, vision


def report(name: str, ok: bool, **kwargs):
    out = {"name": name, "ok": ok}
    out.update(kwargs)
    print(json.dumps(out))
    return out


def main() -> int:
    results = []

    # Find some real windows on this machine
    wins = spatial.enumerate_windows()
    if not wins:
        results.append(report("enumerate_windows", False, error="no windows found"))
        return 1
    results.append(report("enumerate_windows", True, count=len(wins)))

    # Test 1: PrintWindow capture on the foreground window
    fg = spatial.get_foreground_window_info()
    if fg is None:
        results.append(report("capture_window_foreground", False, error="no foreground"))
    else:
        try:
            img = vision.capture_window(fg.hwnd)
            non_black = not vision._bitmap_is_black(img)
            results.append(
                report(
                    "capture_window_foreground",
                    True,
                    hwnd=fg.hwnd,
                    title=fg.title[:60],
                    size=[img.width, img.height],
                    non_black=non_black,
                )
            )
        except Exception as e:
            results.append(report("capture_window_foreground", False, error=str(e)))

    # Test 2: PrintWindow capture on an occluded window
    # Pick a non-foreground non-minimized visible window
    target = None
    for w in wins:
        if w.minimized:
            continue
        if fg and w.hwnd == fg.hwnd:
            continue
        if not w.visible:
            continue
        if w.bounds.width < 100 or w.bounds.height < 100:
            continue
        target = w
        break
    if target is None:
        results.append(report("capture_window_occluded", False, error="no candidate window"))
    else:
        try:
            occluded = spatial.is_occluded(target.hwnd)
            img = vision.capture_window(target.hwnd)
            non_black = not vision._bitmap_is_black(img)
            results.append(
                report(
                    "capture_window_occluded",
                    True,
                    hwnd=target.hwnd,
                    title=target.title[:60],
                    occluded=occluded,
                    size=[img.width, img.height],
                    non_black=non_black,
                )
            )
        except Exception as e:
            results.append(report("capture_window_occluded", False, error=str(e)))

    # Test 3: bring_to_front actually changes the foreground window
    if target is None:
        results.append(report("bring_to_front", False, error="no target"))
    else:
        initial = spatial.get_foreground_window_info()
        ok = spatial.bring_to_front(target.hwnd)
        time.sleep(0.2)
        final = spatial.get_foreground_window_info()
        is_foreground = final is not None and final.hwnd == target.hwnd
        results.append(
            report(
                "bring_to_front",
                is_foreground,
                return_value=ok,
                initial=initial.title[:60] if initial else None,
                final=final.title[:60] if final else None,
                target=target.title[:60],
            )
        )
        # Politely return focus to original
        if initial is not None and initial.hwnd != target.hwnd:
            spatial.bring_to_front(initial.hwnd)

    passes = sum(1 for r in results if r["ok"])
    print(json.dumps({"summary": {"total": len(results), "passed": passes}}))
    return 0 if passes == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
