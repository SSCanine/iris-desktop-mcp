"""Gauntlet harness: a dumb, instrumented canvas window.

The Gauntlet runner spawns several of these, tells each one what to draw via a
command file, and reads exact pixel-level hit receipts back from an events
file. Nothing here decides what a level looks like; the runner does. This
window only:

  1. Draws targets (colored squares, tiny text labels, PNG glyphs) on a Tk
     Canvas at exact pixel positions.
  2. Reports the SCREEN rectangle of every target after each redraw
     (`layout_snapshot`), so the runner has ground truth.
  3. Reports every click with the screen point, which target (if any) it hit,
     and the distance to the nearest target (`click_receipt`).

Tk is used on purpose: its canvas items have no UIA representation, so the
only ways to find a target are OCR (text), pixel search (color/template), or
eyeballing a screenshot. That is exactly the hard case Iris must handle.

Usage (the runner does this for you):
    python -m iris.bench.gauntlet_harness --title GAUNTLET_A \
        --events <path.jsonl> --commands <path.jsonl> --geometry 720x520+100+100

Command file protocol (JSONL, appended by the runner, tailed by the window):
    {"cmd": "layout", "seq": 3, "bg": "#f2f2f2", "targets": [ {...}, ... ]}
    {"cmd": "move",   "seq": 4, "id": "dot_0", "x": 120, "y": 88}
    {"cmd": "raise"}
    {"cmd": "quit"}

Target spec fields:
    id       stable id, echoed in receipts
    kind     "dot" | "text" | "glyph"
    x, y     canvas coords of the top-left corner
    w, h     size in px (dot / glyph); text measures itself
    color    fill / text color as #RRGGBB
    label    text to draw (kind=text)
    size     font point size (kind=text)
    png      PNG path to draw at (x, y) (kind=glyph)
    decoy    true when clicking this counts as a wrong target
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

# DPI awareness MUST be set before tkinter imports anything graphical, so the
# window's coord space matches what the runner sees via GetWindowRect.
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

import tkinter as tk  # noqa: E402

STATUS_HEIGHT = 26
FONT_FAMILY = "Segoe UI"


class GauntletWindow:
    def __init__(self, root: tk.Tk, title: str, events_path: Path, commands_path: Path, bg: str):
        self.root = root
        self.title = title
        self.events_path = events_path
        self.commands_path = commands_path
        self.bg = bg
        self.seq = 0
        self.targets: dict[str, dict] = {}  # id -> spec (+ 'items': canvas ids, 'rect')
        self._images: dict[str, tk.PhotoImage] = {}  # keep PhotoImage refs alive
        self._cmd_pos = 0
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_fp = self.events_path.open("a", encoding="utf-8", buffering=1)
        root.title(title)
        root.configure(bg="#1f2933")
        self.status_var = tk.StringVar(value=f"{title}: waiting for layout")
        tk.Label(
            root,
            textvariable=self.status_var,
            anchor="w",
            bg="#1f2933",
            fg="#e6e8eb",
            font=(FONT_FAMILY, 9),
            height=1,
            padx=6,
        ).pack(fill="x", side="top", ipady=3)
        self.canvas = tk.Canvas(root, bg=bg, highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonRelease-1>", self._on_click)
        self.canvas.bind("<ButtonRelease-3>", self._on_click)
        root.bind_all("<ButtonRelease-1>", self._on_any_click, add="+")
        self._log({"kind": "harness_ready", "title": title, "pid": os.getpid(), "ts": time.time()})
        self.root.after(30, self._poll_commands)
        self.root.after(500, self._heartbeat)

    # ------------------------------------------------------------------ IO
    def _log(self, payload: dict) -> None:
        try:
            self.events_fp.write(json.dumps(payload) + "\n")
            self.events_fp.flush()
        except Exception:
            sys.stderr.write(f"GAUNTLET_EVENT_WRITE_FAIL: {payload}\n")

    def _poll_commands(self) -> None:
        try:
            if self.commands_path.exists():
                size = self.commands_path.stat().st_size
                if size > self._cmd_pos:
                    with self.commands_path.open("r", encoding="utf-8") as f:
                        f.seek(self._cmd_pos)
                        chunk = f.read()
                        self._cmd_pos = f.tell()
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._handle(json.loads(line))
                        except Exception as e:
                            self._log({"kind": "command_error", "error": repr(e), "line": line})
        finally:
            self.root.after(30, self._poll_commands)

    def _heartbeat(self) -> None:
        self._emit_layout(reason="heartbeat")
        self.root.after(500, self._heartbeat)

    # ------------------------------------------------------------ commands
    def _handle(self, cmd: dict) -> None:
        name = cmd.get("cmd")
        if name == "layout":
            self.seq = int(cmd.get("seq", self.seq + 1))
            bg = cmd.get("bg")
            if bg:
                self.bg = bg
                self.canvas.configure(bg=bg)
            self.canvas.delete("all")
            self.targets.clear()
            self._images.clear()
            for spec in cmd.get("targets", []):
                self._draw(spec)
            self.status_var.set(f"{self.title}: layout seq={self.seq} targets={len(self.targets)}")
            self.root.update_idletasks()
            self._emit_layout(reason="layout")
        elif name == "move":
            self.seq = int(cmd.get("seq", self.seq + 1))
            tid = cmd.get("id")
            spec = self.targets.get(tid)
            if spec is None:
                self._log({"kind": "command_error", "error": f"unknown target {tid}"})
                return
            for item in spec.get("items", []):
                self.canvas.delete(item)
            spec["x"], spec["y"] = int(cmd["x"]), int(cmd["y"])
            self._draw(spec, keep=True)
            self.root.update_idletasks()
            self._emit_layout(reason="move")
        elif name == "raise":
            try:
                user32 = ctypes.windll.user32
                top = user32.GetAncestor(self.root.winfo_id(), 2)
                user32.SetWindowPos(
                    top, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010
                )  # NOSIZE|NOMOVE|NOACTIVATE
            except Exception:
                self.root.lift()
        elif name == "status":
            self.status_var.set(str(cmd.get("text", "")))
        elif name == "quit":
            self.root.after(10, self.root.destroy)
        else:
            self._log({"kind": "command_error", "error": f"unknown cmd {name!r}"})

    # ------------------------------------------------------------- drawing
    def _draw(self, spec: dict, keep: bool = False) -> None:
        kind = spec.get("kind", "dot")
        x, y = int(spec["x"]), int(spec["y"])
        color = spec.get("color", "#ff0000")
        items: list[int] = []
        if kind == "dot":
            w, h = int(spec.get("w", 8)), int(spec.get("h", spec.get("w", 8)))
            # Canvas rectangles are inclusive of x2,y2 when width=0 outline
            # is omitted; draw with outline=color, width=0 to get an exact w x h.
            items.append(
                self.canvas.create_rectangle(x, y, x + w, y + h, fill=color, outline=color, width=0)
            )
            rect = (x, y, w, h)
        elif kind == "text":
            size = int(spec.get("size", 9))
            item = self.canvas.create_text(
                x,
                y,
                text=str(spec.get("label", "")),
                anchor="nw",
                fill=color,
                font=(FONT_FAMILY, size),
            )
            items.append(item)
            bx1, by1, bx2, by2 = self.canvas.bbox(item)
            rect = (bx1, by1, bx2 - bx1, by2 - by1)
        elif kind == "glyph":
            png = spec.get("png")
            img = tk.PhotoImage(file=png)
            self._images[spec["id"]] = img
            items.append(self.canvas.create_image(x, y, image=img, anchor="nw"))
            rect = (x, y, img.width(), img.height())
        else:
            raise ValueError(f"unknown target kind {kind!r}")
        spec = dict(spec) if not keep else spec
        spec["items"] = items
        spec["rect"] = rect  # canvas-local (x, y, w, h)
        self.targets[spec["id"]] = spec

    # ------------------------------------------------------------- sensing
    def _screen_rect(self, spec: dict) -> list[int]:
        cx, cy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        x, y, w, h = spec["rect"]
        return [cx + x, cy + y, w, h]

    def _emit_layout(self, reason: str) -> None:
        try:
            snap = []
            for tid, spec in self.targets.items():
                snap.append(
                    {
                        "id": tid,
                        "kind": spec.get("kind"),
                        "decoy": bool(spec.get("decoy", False)),
                        "label": spec.get("label"),
                        "color": spec.get("color"),
                        "rect": self._screen_rect(spec),
                    }
                )
            self._log(
                {
                    "kind": "layout_snapshot",
                    "seq": self.seq,
                    "reason": reason,
                    "ts": time.time(),
                    "window": [
                        self.root.winfo_rootx(),
                        self.root.winfo_rooty(),
                        self.root.winfo_width(),
                        self.root.winfo_height(),
                    ],
                    "canvas_origin": [self.canvas.winfo_rootx(), self.canvas.winfo_rooty()],
                    "targets": snap,
                }
            )
        except Exception as e:
            self._log({"kind": "layout_snapshot_error", "error": repr(e)})

    def _on_click(self, e: tk.Event) -> None:
        try:
            # Screen point from the canvas-local event coords, not e.x_root:
            # for clicks delivered as posted window messages Tk may fill
            # x_root from the physical cursor, which never moved.
            sx = int(self.canvas.winfo_rootx() + e.x)
            sy = int(self.canvas.winfo_rooty() + e.y)
            hit_id = None
            hit_decoy = False
            nearest_id = None
            nearest_d = None
            nearest_center = None
            for tid, spec in self.targets.items():
                x, y, w, h = spec["rect"]
                # 1 px of grace around text; dots/glyphs must be hit exactly.
                pad = 1 if spec.get("kind") == "text" else 0
                inside = (x - pad) <= e.x < (x + w + pad) and (y - pad) <= e.y < (y + h + pad)
                cxm, cym = x + w / 2.0, y + h / 2.0
                d = ((e.x - cxm) ** 2 + (e.y - cym) ** 2) ** 0.5
                if inside and (hit_id is None or d < (nearest_d or 1e9)):
                    hit_id = tid
                    hit_decoy = bool(spec.get("decoy", False))
                if nearest_d is None or d < nearest_d:
                    nearest_d = d
                    nearest_id = tid
                    ox, oy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
                    nearest_center = [round(ox + cxm, 1), round(oy + cym, 1)]
            self.status_var.set(
                f"{self.title}: click ({sx},{sy}) -> {hit_id or 'MISS'}"
                + (f" nearest {nearest_id} {nearest_d:.1f}px" if nearest_d is not None else "")
            )
            self._log(
                {
                    "kind": "click_receipt",
                    "seq": self.seq,
                    "ts": time.time(),
                    "button": int(getattr(e, "num", 1)),
                    "screen": [sx, sy],
                    "hit_id": hit_id,
                    "hit_decoy": hit_decoy,
                    "nearest_id": nearest_id,
                    "nearest_center": nearest_center,
                    "nearest_distance_px": round(nearest_d, 2) if nearest_d is not None else None,
                }
            )
        except Exception as exc:
            self._log({"kind": "click_receipt_error", "error": repr(exc)})

    def _on_any_click(self, e: tk.Event) -> None:
        # Fires for clicks anywhere in the window (status bar included) so the
        # runner can tell "arrived, wrong widget" from "never arrived".
        try:
            self._log(
                {
                    "kind": "any_click",
                    "seq": self.seq,
                    "ts": time.time(),
                    "screen": [int(e.x_root), int(e.y_root)],
                    "widget": type(e.widget).__name__ if e.widget else "?",
                }
            )
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.events_fp.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="IRIS_GAUNTLET")
    ap.add_argument("--events", required=True)
    ap.add_argument("--commands", required=True)
    ap.add_argument("--geometry", default="720x520+100+100")
    ap.add_argument("--bg", default="#f2f2f2")
    args = ap.parse_args()

    root = tk.Tk()
    root.withdraw()  # map it ourselves, without activation
    root.geometry(args.geometry)
    root.resizable(True, True)
    win = GauntletWindow(root, args.title, Path(args.events), Path(args.commands), args.bg)
    root.update_idletasks()
    try:
        user32 = ctypes.windll.user32
        top = user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT: the real frame window
        GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        ex = user32.GetWindowLongPtrW(top, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(top, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        root.deiconify()
        user32.ShowWindow(top, 4)  # SW_SHOWNOACTIVATE
    except Exception:
        root.deiconify()
    try:
        root.mainloop()
    finally:
        win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
