"""Instrumented Tk harness for measuring Iris click accuracy.

A grid of buttons of varied sizes, label styles, and edge positions. Each
button records click receipts (button id, expected center, actual hit, miss
distance, timestamp, source) to a JSONL file the bench runner tails.

We use Tkinter because:
  1. It's stdlib, no extra deps.
  2. Tk widgets are NOT exposed via UIA on Windows 11, so finds land on the
     OCR + widget-upgrade path. That exercises the trickiest part of the
     resolver. UIA-backed apps (notepad, calculator) get a separate smoke.
  3. We can spawn it as a subprocess and the GUI thread stays clean.

Usage (standalone):
    python -m iris.bench.harness --geometry 900x650+200+200 \
        --events %TEMP%/bench.jsonl --title BENCH

The runner reads --events to learn what was clicked.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# DPI awareness MUST be set before tkinter imports anything graphical, so the
# harness window's coord space matches what the runner sees via GetWindowRect.
# Without this, the harness draws in virtualized pixels and our SendInput
# clicks land at physical pixels: the two coord spaces disagree and every
# click misses by the DPI scale factor.
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

import tkinter as tk
from tkinter import ttk


# ---------------------------------------------------------------------------
# Target definitions
# ---------------------------------------------------------------------------
@dataclass
class TargetSpec:
    """A button the bench will drive Iris to find and click."""

    id: str  # short stable id, e.g. "small_btn_topleft"
    label: str  # the text Iris searches for (must be unique-ish)
    description: str  # human description for the report
    width_px: int  # button width in pixels (Tk's width=N is chars)
    height_px: int  # button height in pixels (Tk's height=N is text rows)
    anchor: str = "center"  # "center" / "w" / "e" so we can test offset labels
    pad_x: int = 0  # extra padding (creates offset-label scenarios)
    pad_y: int = 0
    style: str = "default"  # "default" / "tiny" / "huge" / "icon_label" / "row"
    # Optional grid placement override. None = auto-place in grid.
    row: int | None = None
    column: int | None = None


# A varied set of targets covering the realistic difficulty space.
# Labels are intentionally distinct so OCR fuzzy matching doesn't get them
# confused. Sizes range from button-sized (small) to row-sized (wide).
TARGETS: list[TargetSpec] = [
    TargetSpec(
        id="medium_center",
        label="Quartz Block",
        description="Medium button, centered label, baseline target",
        width_px=180,
        height_px=44,
        style="default",
    ),
    TargetSpec(
        id="tiny_btn",
        label="Push",
        description="Tiny button (text-snug). Tests precision on small targets.",
        width_px=78,
        height_px=28,
        style="tiny",
    ),
    TargetSpec(
        id="short_label",
        label="Go",
        description="Two-letter label. Stress-tests Tesseract on short words.",
        width_px=68,
        height_px=28,
        style="tiny",
    ),
    TargetSpec(
        id="wide_row",
        label="Activate Subscription Renewal",
        description="Wide button with left-aligned label. Tests widget-not-text upgrade.",
        width_px=380,
        height_px=36,
        anchor="w",
        pad_x=14,
        style="row",
    ),
    TargetSpec(
        id="icon_label",
        label="Beacon Glyph",
        description="Icon+label button (icon is the leading character). Tests label-offset handling.",
        width_px=200,
        height_px=44,
        anchor="w",
        pad_x=10,
        style="icon_label",
    ),
    TargetSpec(
        id="huge_btn",
        label="Vermillion Gate",
        description="Large button, centered. Should be easy.",
        width_px=280,
        height_px=72,
        style="huge",
    ),
    TargetSpec(
        id="edge_right",
        label="Margin Sentinel",
        description="Button hugging the right edge of the window.",
        width_px=160,
        height_px=36,
        style="default",
    ),
    TargetSpec(
        id="ambiguous_a",
        label="Crimson Falcon",
        description="One of two semi-similar labels (Crimson Falcon vs Crimson Falchion). Tests fuzzy disambiguation.",
        width_px=200,
        height_px=36,
        style="default",
    ),
    TargetSpec(
        id="ambiguous_b",
        label="Crimson Falchion",
        description="See ambiguous_a. Different word, similar prefix.",
        width_px=200,
        height_px=36,
        style="default",
    ),
    TargetSpec(
        id="lowercase_only",
        label="please click me here",
        description="All lowercase with spaces. Tests OCR + case-insensitive match.",
        width_px=240,
        height_px=36,
        style="default",
    ),
]


# ---------------------------------------------------------------------------
# Harness app
# ---------------------------------------------------------------------------
class HarnessApp:
    def __init__(
        self,
        root: tk.Tk,
        title: str,
        events_path: Path,
        targets: list[TargetSpec],
    ):
        self.root = root
        self.title = title
        self.events_path = events_path
        self.targets = targets
        self.click_counts: dict[str, int] = {t.id: 0 for t in targets}
        self._buttons: dict[str, tk.Button] = {}
        # Open the events file in append mode and keep it open. We flush after
        # every record so the bench runner sees clicks immediately.
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_fp = self.events_path.open("a", encoding="utf-8", buffering=1)
        root.title(self.title)
        self._build()
        # Heartbeat so the runner knows the GUI is alive (caller polls for it).
        self._log_event(
            {
                "kind": "harness_ready",
                "title": self.title,
                "pid": os.getpid(),
                "ts": time.time(),
                "targets": [asdict(t) for t in targets],
            }
        )
        # Periodically log each button's actual screen rect so the runner can
        # cross-check OCR-found bbox vs ground truth. Also serves as a 100ms
        # heartbeat so the runner sees the mainloop is alive.
        self._schedule_layout_snapshot()

    def _schedule_layout_snapshot(self) -> None:
        try:
            self.root.update_idletasks()
            snapshot = []
            for tid, btn in self._buttons.items():
                if not btn.winfo_ismapped():
                    continue
                snapshot.append(
                    {
                        "id": tid,
                        "origin": [btn.winfo_rootx(), btn.winfo_rooty()],
                        "size": [btn.winfo_width(), btn.winfo_height()],
                        "center": [
                            btn.winfo_rootx() + btn.winfo_width() // 2,
                            btn.winfo_rooty() + btn.winfo_height() // 2,
                        ],
                    }
                )
            self._log_event(
                {
                    "kind": "layout_snapshot",
                    "ts": time.time(),
                    "buttons": snapshot,
                }
            )
        except Exception as e:
            self._log_event(
                {
                    "kind": "layout_snapshot_error",
                    "ts": time.time(),
                    "error": repr(e),
                }
            )
        # Re-arm every 500ms.
        self.root.after(500, self._schedule_layout_snapshot)

    def _build(self) -> None:
        # Status bar at top showing the last click receipt.
        self.status_var = tk.StringVar(value="ready, waiting for clicks")
        ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(8, 4),
            background="#1f2933",
            foreground="#e6e8eb",
        ).pack(fill="x", side="top")

        # Two-column grid for buttons (left wide, right narrow with edge targets).
        body = ttk.Frame(self.root, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # Catch-all: any click anywhere on the harness emits an event. This
        # lets the runner diagnose "click registered, just on the wrong widget"
        # vs "no input event arrived at all".
        self.root.bind_all(
            "<ButtonRelease-1>",
            self._on_root_click,
            add="+",
        )

        for t in self.targets:
            container = right if t.id in ("edge_right", "tiny_btn") else left
            # We make a Frame of fixed pixel size and pack a Button into it
            # using propagate(False), because tk Button width is in chars not
            # pixels.
            frame = tk.Frame(
                container,
                width=t.width_px,
                height=t.height_px,
                highlightthickness=0,
            )
            frame.pack_propagate(False)
            frame.pack(pady=4, padx=4, anchor="w")
            text = t.label
            if t.style == "icon_label":
                text = "❖ " + t.label  # leading glyph to offset label
            btn = tk.Button(
                frame,
                text=text,
                anchor=t.anchor,
                padx=t.pad_x,
                pady=t.pad_y,
                command=lambda tid=t.id: self._on_button(tid, source="command"),
                relief="raised",
                bd=1,
            )
            btn.pack(fill="both", expand=True)
            self._buttons[t.id] = btn
            # Bind <Button-1> so we ALSO capture raw click coords (anywhere on
            # the button surface, not just the command callback which only
            # fires on a complete press-release).
            btn.bind(
                "<ButtonRelease-1>",
                lambda e, tid=t.id, b=btn: self._on_button_event(tid, b, e),
            )

    def _on_button(self, tid: str, source: str) -> None:
        """Tk command-callback receipt. Fires only on a clean click."""
        self.click_counts[tid] += 1
        self._log_event(
            {
                "kind": "command",
                "button_id": tid,
                "count": self.click_counts[tid],
                "ts": time.time(),
                "source": source,
            }
        )

    def _on_button_event(self, tid: str, btn: tk.Button, e: tk.Event) -> None:
        """Raw event receipt with precise screen-pixel hit location."""
        try:
            screen_x = btn.winfo_rootx() + int(e.x)
            screen_y = btn.winfo_rooty() + int(e.y)
            origin_x = btn.winfo_rootx()
            origin_y = btn.winfo_rooty()
            width = btn.winfo_width()
            height = btn.winfo_height()
            center_x = origin_x + width // 2
            center_y = origin_y + height // 2
            dx = screen_x - center_x
            dy = screen_y - center_y
            distance = (dx * dx + dy * dy) ** 0.5
            self.status_var.set(
                f"hit {tid}: ({screen_x}, {screen_y}), "
                f"center=({center_x}, {center_y}), miss={distance:.1f}px"
            )
            self._log_event(
                {
                    "kind": "click_receipt",
                    "button_id": tid,
                    "ts": time.time(),
                    "hit_screen": [screen_x, screen_y],
                    "button_origin": [origin_x, origin_y],
                    "button_size": [width, height],
                    "button_center": [center_x, center_y],
                    "miss_dx": dx,
                    "miss_dy": dy,
                    "miss_distance_px": round(distance, 2),
                }
            )
        except Exception as exc:
            self._log_event(
                {
                    "kind": "click_receipt_error",
                    "button_id": tid,
                    "ts": time.time(),
                    "error": repr(exc),
                }
            )

    def _on_root_click(self, e: tk.Event) -> None:
        """Catch-all sensor. Fires on every click anywhere in the harness."""
        try:
            widget_name = type(e.widget).__name__ if e.widget else "?"
            widget_text = ""
            try:
                widget_text = str(e.widget.cget("text"))
            except Exception:
                widget_text = ""
            self._log_event(
                {
                    "kind": "any_click",
                    "ts": time.time(),
                    "screen": [int(e.x_root), int(e.y_root)],
                    "widget": widget_name,
                    "widget_text": widget_text[:80],
                }
            )
        except Exception as exc:
            self._log_event(
                {
                    "kind": "any_click_error",
                    "ts": time.time(),
                    "error": repr(exc),
                }
            )

    def _log_event(self, payload: dict) -> None:
        try:
            self.events_fp.write(json.dumps(payload) + "\n")
            self.events_fp.flush()
        except Exception:
            # Last-ditch: write to stderr so the runner can still see something.
            sys.stderr.write(f"BENCH_EVENT_WRITE_FAIL: {payload}\n")

    def close(self) -> None:
        try:
            self.events_fp.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", default="900x650+200+200")
    parser.add_argument("--title", default="IRIS_BENCH_HARNESS")
    parser.add_argument("--events", required=True, help="JSONL events log path (runner reads this)")
    args = parser.parse_args()

    root = tk.Tk()
    root.geometry(args.geometry)
    app = HarnessApp(
        root,
        title=args.title,
        events_path=Path(args.events),
        targets=TARGETS,
    )
    try:
        root.mainloop()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
