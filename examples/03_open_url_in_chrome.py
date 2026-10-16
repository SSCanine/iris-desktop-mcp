"""03: Open a URL in Chrome via the recipe engine.

Demonstrates how Iris recipes chain primitives. The same workflow can be
hand-coded with focus + hotkey + type_text + press_key, but the recipe form
is what makes Iris feel scriptable.

If Chrome isn't running, the recipe will spawn it. Otherwise it focuses the
existing window.

    python examples/03_open_url_in_chrome.py
    python examples/03_open_url_in_chrome.py --url https://anthropic.com
"""

from __future__ import annotations

import argparse
import ctypes

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

from iris import recipes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://github.com/SSCanine/iris-desktop-mcp")
    args = parser.parse_args()

    print(f"Running recipe: chrome.open_url ({args.url!r})")
    result = recipes.run_recipe("chrome.open_url", {"url": args.url})

    print()
    print(f"ok      : {result.get('ok')}")
    print(f"steps   : {len(result.get('steps', []))}")
    for i, step in enumerate(result.get("steps", [])):
        ok = step.get("ok", True)
        action = step.get("action", "?")
        marker = "PASS" if ok else "FAIL"
        print(f"  step {i + 1}  [{marker}]  {action}")

    if not result.get("ok"):
        print()
        print(f"failed at: {result.get('failed_step_index')}")
        print(f"reason   : {result.get('reason')}")


if __name__ == "__main__":
    main()
