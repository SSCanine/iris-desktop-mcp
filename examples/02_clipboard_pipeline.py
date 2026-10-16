"""02: Clipboard pipeline.

Read whatever's on the clipboard, transform it, write the result back. This
is one of the most common ways Iris adds value to an LLM agent: the agent
can ask "what's selected" and "now paste this back" without ever touching
the mouse or screen.

    python examples/02_clipboard_pipeline.py
"""

from __future__ import annotations

import ctypes

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

from iris import system


def upper_with_prefix(text: str) -> str:
    """Trivial transform. Replace this with anything: LLM summarization,
    translation, JSON validation, regex extraction, etc."""
    return f"[transformed] {text.upper()}"


def main() -> None:
    got = system.clipboard_get()
    if not got["ok"]:
        print(f"clipboard_get failed: {got.get('reason')}")
        return

    original = got["text"]
    if not original:
        print("Clipboard is empty. Copy some text first, then re-run.")
        return

    print(f"Original  : {original[:120]}{'...' if len(original) > 120 else ''}")
    transformed = upper_with_prefix(original)
    print(f"Transformed: {transformed[:120]}{'...' if len(transformed) > 120 else ''}")

    put = system.clipboard_set(transformed)
    if not put["ok"]:
        print(f"clipboard_set failed: {put.get('reason')}")
        return
    print()
    print("Clipboard updated. Paste anywhere with Ctrl+V to verify.")


if __name__ == "__main__":
    main()
