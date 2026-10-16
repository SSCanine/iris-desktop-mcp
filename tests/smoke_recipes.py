"""End-to-end recipe smoke test against real Windows.

Runs `alt_tab_to` against an arbitrary current window to prove the engine
chains primitives correctly, returns context to caller, and the YAML
substitution actually works in production.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iris import fingerprint as fingerprint_mod
from iris import recipes as recipes_mod
from iris import semantic as semantic_mod
from iris import spatial as spatial_mod
from iris.tokens import FocusToken, default_registry

registry = default_registry()


def focus(match: dict, raise_window: bool = False) -> dict:
    """Standalone copy of server.focus that doesn't require an MCP runtime."""
    wins = spatial_mod.match_window(match)
    if not wins:
        return {"ok": False, "error": "no_window_matched", "match": match}
    w = wins[0]
    if raise_window:
        spatial_mod.bring_to_front(w.hwnd)
        fresh = spatial_mod.wait_for_window_visible(w.hwnd, timeout_ms=500)
        if fresh is not None:
            w = fresh
    monitor = max(spatial_mod.get_monitor_for_window(w.bounds), 0)
    fp = None
    if semantic_mod.HAS_UIA and semantic_mod.supports_uia(w.hwnd, w.pid):
        try:
            dump = semantic_mod.walk_tree(w.hwnd, max_depth=4, max_nodes=80)
            fp = fingerprint_mod.compute_fingerprint(dump)
        except Exception:
            fp = None
    tk = FocusToken.create(
        hwnd=w.hwnd,
        pid=w.pid,
        exe_name=w.exe_name,
        title=w.title,
        monitor_index=monitor,
        bounds=w.bounds,
        fingerprint=fp,
    )
    registry.store(tk)
    return {
        "ok": True,
        "token": tk.id,
        "title": tk.title_at_creation,
        "exe": tk.exe_name,
        "hwnd": tk.hwnd,
    }


def main() -> int:
    recipes_mod.register_action("focus", focus)

    # Pick a real window to switch to
    wins = spatial_mod.enumerate_windows()
    if not wins:
        print(json.dumps({"ok": False, "error": "no windows"}))
        return 1
    initial = spatial_mod.get_foreground_window_info()
    target = None
    for w in wins:
        if initial and w.hwnd == initial.hwnd:
            continue
        if w.minimized:
            continue
        if not w.title or len(w.title) < 3:
            continue
        target = w
        break
    if target is None:
        print(json.dumps({"ok": False, "error": "no target candidate"}))
        return 1

    print(f"Running alt_tab_to with title_contains={target.title[:40]!r}")
    result = recipes_mod.run_recipe("alt_tab_to", args={"title_contains": target.title[:20]})
    print(json.dumps(result, indent=2, default=str))

    # Verify result and that the foreground actually changed
    time.sleep(0.2)
    after = spatial_mod.get_foreground_window_info()
    is_target = after is not None and after.hwnd == target.hwnd

    # Politely restore
    if initial is not None and initial.hwnd != target.hwnd:
        spatial_mod.bring_to_front(initial.hwnd)

    print(
        json.dumps(
            {
                "recipe_ok": result.get("ok"),
                "foreground_changed_to_target": is_target,
                "initial_title": initial.title[:40] if initial else None,
                "final_title": after.title[:40] if after else None,
            },
            indent=2,
        )
    )

    return 0 if (result.get("ok") and is_target) else 1


if __name__ == "__main__":
    sys.exit(main())
