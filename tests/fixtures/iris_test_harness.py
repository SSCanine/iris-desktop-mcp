"""Iris test harness: a Tkinter app with known controls at known positions.

Used by integration tests and the self_test() MCP tool. Run standalone:

    python tests/fixtures/iris_test_harness.py [--geometry 600x400+100+100]

Title is always "IRIS_TEST_HARNESS" so spatial.match_window can find it reliably.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from tkinter import ttk

TITLE = "IRIS_TEST_HARNESS"


class HarnessApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(TITLE)
        self.click_count = 0
        self.last_action = tk.StringVar(value="(none)")
        self.update_mode = False
        self._build_widgets()

    def _build_widgets(self):
        # Wipe and rebuild for "Simulate Update"
        for w in self.root.winfo_children():
            w.destroy()
        if not self.update_mode:
            ttk.Button(self.root, text="Click Me", command=self._on_click_me).pack(pady=4)
            ttk.Button(self.root, text="Spawn Dialog", command=self._on_spawn_dialog).pack(pady=4)
            ttk.Button(self.root, text="Move Window", command=self._on_move).pack(pady=4)
            ttk.Button(self.root, text="Minimize Self", command=self._on_minimize).pack(pady=4)
            ttk.Button(self.root, text="Simulate Update", command=self._on_simulate_update).pack(
                pady=4
            )
        else:
            # Different control names after "update"
            ttk.Button(self.root, text="New Action", command=self._on_click_me).pack(pady=4)
            ttk.Button(self.root, text="Different Dialog", command=self._on_spawn_dialog).pack(
                pady=4
            )
            ttk.Button(self.root, text="Reset Layout", command=self._on_reset).pack(pady=4)
        ttk.Label(self.root, text="Type here:").pack(pady=2)
        self.entry = ttk.Entry(self.root)
        self.entry.pack(pady=2, padx=10, fill=tk.X)
        ttk.Label(self.root, textvariable=self.last_action).pack(pady=4)

    def _on_click_me(self):
        self.click_count += 1
        self.last_action.set(f"Clicked {self.click_count} times")

    def _on_spawn_dialog(self):
        d = tk.Toplevel(self.root)
        d.title("IRIS_TEST_HARNESS_DIALOG")
        ttk.Label(d, text="This is a popup").pack(padx=20, pady=20)
        ttk.Button(d, text="Close Dialog", command=d.destroy).pack(pady=10)
        d.geometry("300x150+200+200")

    def _on_move(self):
        x = self.root.winfo_x() + 50
        y = self.root.winfo_y() + 50
        self.root.geometry(f"+{x}+{y}")
        self.last_action.set("Moved")

    def _on_minimize(self):
        self.root.iconify()
        # Auto-restore after 2 seconds so tests can continue
        self.root.after(2000, self.root.deiconify)

    def _on_simulate_update(self):
        self.update_mode = True
        self._build_widgets()
        self.last_action.set("Updated")

    def _on_reset(self):
        self.update_mode = False
        self._build_widgets()
        self.last_action.set("Reset")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--geometry", default="400x350+100+100")
    args = p.parse_args()
    root = tk.Tk()
    root.geometry(args.geometry)
    HarnessApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
