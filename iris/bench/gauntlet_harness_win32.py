"""Gauntlet harness, pure Win32 edition (ctypes, no toolkit).

Same command/event protocol as gauntlet_harness.py (Tk), but the window is a
plain Win32 class with GDI drawing. Why it exists: Tk derives mouse button
state from the physical mouse, so posted click messages (Iris's quiet path)
never register there. A message-driven Win32 window is how most classic
apps behave, so this harness measures the quiet path honestly.

Supported target kinds: dot, text. (glyph needs an image loader; the runner
skips the template level on this harness.)

On a click the harness flashes an outline around the hit target for 200 ms,
like a real button repaints when pressed, so Iris's reaction check for
unknown window classes has something to see.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import POINTER, wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

STATUS_H = 26
CLASS_NAME = "IRIS_GAUNTLET_W32"
FONT_FAMILY = "Segoe UI"

WM_DESTROY, WM_PAINT, WM_TIMER = 0x0002, 0x000F, 0x0113
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP = 0x0201, 0x0202, 0x0205
WM_ERASEBKGND, WM_CLOSE = 0x0014, 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_EX_NOACTIVATE = 0x08000000  # the window never takes focus from the user
SW_SHOWNOACTIVATE = 4
HWND_TOP = 0
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
DT_LEFT, DT_TOP, DT_SINGLELINE, DT_NOCLIP, DT_CALCRECT = 0, 0, 0x20, 0x100, 0x400
TRANSPARENT = 1
FW_NORMAL = 400
CLEARTYPE_QUALITY = 5
NULL_BRUSH = 5
IDC_CROSS = 32515
TIMER_CMDS, TIMER_HEARTBEAT, TIMER_FLASH = 1, 2, 3

LRESULT = ctypes.c_ssize_t
HANDLE = wintypes.HANDLE
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", wintypes.BYTE * 32),
    ]


# Every handle is 64-bit; without explicit signatures ctypes truncates them to
# C int and the calls fail with "int too long to convert" (which is exactly
# what the first version of this file did, painting nothing).
def _sig(fn, restype, argtypes):
    fn.restype = restype
    fn.argtypes = argtypes


_sig(
    user32.DefWindowProcW, LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
)
_sig(user32.RegisterClassW, wintypes.ATOM, [POINTER(WNDCLASSW)])
_sig(
    user32.CreateWindowExW,
    wintypes.HWND,
    [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ],
)
_sig(
    user32.AdjustWindowRectEx,
    wintypes.BOOL,
    [POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
)
_sig(user32.LoadCursorW, HANDLE, [wintypes.HINSTANCE, ctypes.c_void_p])
_sig(user32.GetDpiForWindow, wintypes.UINT, [wintypes.HWND])
_sig(
    user32.SetTimer,
    ctypes.c_size_t,
    [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p],
)
_sig(user32.KillTimer, wintypes.BOOL, [wintypes.HWND, ctypes.c_size_t])
_sig(user32.GetDC, wintypes.HDC, [wintypes.HWND])
_sig(user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC])
_sig(user32.BeginPaint, wintypes.HDC, [wintypes.HWND, POINTER(PAINTSTRUCT)])
_sig(user32.EndPaint, wintypes.BOOL, [wintypes.HWND, POINTER(PAINTSTRUCT)])
_sig(user32.FillRect, ctypes.c_int, [wintypes.HDC, POINTER(wintypes.RECT), wintypes.HBRUSH])
_sig(
    user32.DrawTextW,
    ctypes.c_int,
    [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, POINTER(wintypes.RECT), wintypes.UINT],
)
_sig(user32.InvalidateRect, wintypes.BOOL, [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL])
_sig(user32.UpdateWindow, wintypes.BOOL, [wintypes.HWND])
_sig(user32.GetClientRect, wintypes.BOOL, [wintypes.HWND, POINTER(wintypes.RECT)])
_sig(user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, POINTER(wintypes.RECT)])
_sig(user32.ClientToScreen, wintypes.BOOL, [wintypes.HWND, POINTER(wintypes.POINT)])
_sig(
    user32.PostMessageW,
    wintypes.BOOL,
    [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
)
_sig(user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND])
_sig(user32.ShowWindow, wintypes.BOOL, [wintypes.HWND, ctypes.c_int])
_sig(
    user32.SetWindowPos,
    wintypes.BOOL,
    [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ],
)
_sig(user32.PostQuitMessage, None, [ctypes.c_int])
_sig(user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND])
_sig(
    user32.GetMessageW,
    wintypes.BOOL,
    [POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT],
)
_sig(user32.TranslateMessage, wintypes.BOOL, [POINTER(wintypes.MSG)])
_sig(user32.DispatchMessageW, LRESULT, [POINTER(wintypes.MSG)])
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, [wintypes.LPCWSTR])
_sig(gdi32.CreateSolidBrush, wintypes.HBRUSH, [wintypes.COLORREF])
_sig(
    gdi32.CreateFontW,
    HANDLE,
    [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR],
)
_sig(gdi32.SelectObject, HANDLE, [wintypes.HDC, HANDLE])
_sig(gdi32.DeleteObject, wintypes.BOOL, [HANDLE])
_sig(gdi32.SetBkMode, ctypes.c_int, [wintypes.HDC, ctypes.c_int])
_sig(gdi32.SetTextColor, wintypes.COLORREF, [wintypes.HDC, wintypes.COLORREF])
_sig(gdi32.CreatePen, HANDLE, [ctypes.c_int, ctypes.c_int, wintypes.COLORREF])
_sig(gdi32.GetStockObject, HANDLE, [ctypes.c_int])
_sig(
    gdi32.Rectangle,
    wintypes.BOOL,
    [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int],
)


def colorref(hexcol: str) -> int:
    r, g, b = int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16)
    return (b << 16) | (g << 8) | r


def lo(v: int) -> int:
    x = v & 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


def hi(v: int) -> int:
    x = (v >> 16) & 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


class Harness:
    def __init__(self, title: str, events_path: Path, commands_path: Path, geometry: str, bg: str):
        self.title = title
        self.events_path = events_path
        self.commands_path = commands_path
        self.bg = bg
        self.seq = 0
        self.targets: dict[str, dict] = {}
        self.status = f"{title}: waiting for layout"
        self.flash: tuple[int, int, int, int] | None = None
        self._cmd_pos = 0
        self._fonts: dict[int, int] = {}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_fp = self.events_path.open("a", encoding="utf-8", buffering=1)
        self._wndproc = WNDPROC(self._proc)  # keep a reference alive
        size, pos = geometry.split("+", 1)
        w, h = (int(v) for v in size.split("x"))
        x, y = (int(v) for v in pos.split("+"))
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, IDC_CROSS)
        wc.hbrBackground = None
        wc.lpszClassName = CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = wintypes.RECT(0, 0, w, h)
        user32.AdjustWindowRectEx(ctypes.byref(rect), WS_OVERLAPPEDWINDOW, False, 0)
        ow, oh = rect.right - rect.left, rect.bottom - rect.top
        self.hwnd = user32.CreateWindowExW(
            WS_EX_NOACTIVATE,
            CLASS_NAME,
            title,
            WS_OVERLAPPEDWINDOW,
            x,
            y,
            ow,
            oh,
            None,
            None,
            hinst,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)  # visible, but the user keeps focus
        self.dpi = int(user32.GetDpiForWindow(self.hwnd) or 96)
        user32.SetTimer(self.hwnd, TIMER_CMDS, 30, None)
        user32.SetTimer(self.hwnd, TIMER_HEARTBEAT, 500, None)
        self._log({"kind": "harness_ready", "title": title, "pid": os.getpid(), "ts": time.time()})

    # ---------------------------------------------------------------- IO
    def _log(self, payload: dict) -> None:
        try:
            self.events_fp.write(json.dumps(payload) + "\n")
            self.events_fp.flush()
        except Exception:
            sys.stderr.write(f"GAUNTLET_EVENT_WRITE_FAIL: {payload}\n")

    def _poll_commands(self) -> None:
        if not self.commands_path.exists():
            return
        size = self.commands_path.stat().st_size
        if size <= self._cmd_pos:
            return
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

    def _handle(self, cmd: dict) -> None:
        name = cmd.get("cmd")
        if name == "layout":
            self.seq = int(cmd.get("seq", self.seq + 1))
            if cmd.get("bg"):
                self.bg = cmd["bg"]
            self.targets.clear()
            for spec in cmd.get("targets", []):
                self._add(dict(spec))
            self.status = f"{self.title}: layout seq={self.seq} targets={len(self.targets)}"
            self._repaint()
            self._emit_layout("layout")
        elif name == "move":
            self.seq = int(cmd.get("seq", self.seq + 1))
            spec = self.targets.get(cmd.get("id"))
            if spec is None:
                self._log({"kind": "command_error", "error": f"unknown target {cmd.get('id')}"})
                return
            spec["x"], spec["y"] = int(cmd["x"]), int(cmd["y"])
            self._measure(spec)
            self._repaint()
            self._emit_layout("move")
        elif name == "raise":
            # z-order only; never activate
            user32.SetWindowPos(
                self.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        elif name == "status":
            self.status = str(cmd.get("text", ""))
            self._repaint()
        elif name == "quit":
            user32.DestroyWindow(self.hwnd)
        else:
            self._log({"kind": "command_error", "error": f"unknown cmd {name!r}"})

    # ------------------------------------------------------------ targets
    def _font(self, pt: int) -> int:
        if pt not in self._fonts:
            height = -int(round(pt * self.dpi / 72.0))
            self._fonts[pt] = gdi32.CreateFontW(
                height, 0, 0, 0, FW_NORMAL, 0, 0, 0, 1, 0, 0, CLEARTYPE_QUALITY, 0, FONT_FAMILY
            )
        return self._fonts[pt]

    def _measure(self, spec: dict) -> None:
        kind = spec.get("kind", "dot")
        x, y = int(spec["x"]), int(spec["y"])
        if kind == "dot":
            w = int(spec.get("w", 8))
            h = int(spec.get("h", w))
            spec["rect"] = (x, y, w, h)
        elif kind == "text":
            hdc = user32.GetDC(self.hwnd)
            try:
                old = gdi32.SelectObject(hdc, self._font(int(spec.get("size", 9))))
                rect = wintypes.RECT(0, 0, 0, 0)
                user32.DrawTextW(
                    hdc,
                    str(spec.get("label", "")),
                    -1,
                    ctypes.byref(rect),
                    DT_CALCRECT | DT_SINGLELINE,
                )
                gdi32.SelectObject(hdc, old)
            finally:
                user32.ReleaseDC(self.hwnd, hdc)
            spec["rect"] = (x, y, rect.right - rect.left, rect.bottom - rect.top)
        else:
            raise ValueError(f"unsupported target kind on win32 harness: {kind!r}")

    def _add(self, spec: dict) -> None:
        self._measure(spec)
        self.targets[spec["id"]] = spec

    def _client_origin(self) -> tuple[int, int]:
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def _screen_rect(self, spec: dict) -> list[int]:
        ox, oy = self._client_origin()
        x, y, w, h = spec["rect"]
        return [ox + x, oy + STATUS_H + y, w, h]

    def _emit_layout(self, reason: str) -> None:
        try:
            wr = wintypes.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(wr))
            ox, oy = self._client_origin()
            self._log(
                {
                    "kind": "layout_snapshot",
                    "seq": self.seq,
                    "reason": reason,
                    "ts": time.time(),
                    "window": [wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top],
                    "canvas_origin": [ox, oy + STATUS_H],
                    "targets": [
                        {
                            "id": tid,
                            "kind": s.get("kind"),
                            "decoy": bool(s.get("decoy", False)),
                            "label": s.get("label"),
                            "color": s.get("color"),
                            "rect": self._screen_rect(s),
                        }
                        for tid, s in self.targets.items()
                    ],
                }
            )
        except Exception as e:
            self._log({"kind": "layout_snapshot_error", "error": repr(e)})

    # ------------------------------------------------------------- input
    def _on_click(self, cx: int, cy: int, button: int) -> None:
        ex, ey = cx, cy - STATUS_H  # canvas-local like the Tk harness
        ox, oy = self._client_origin()
        sx, sy = ox + cx, oy + cy
        hit_id = None
        hit_decoy = False
        nearest_id = None
        nearest_d = None
        nearest_center = None
        for tid, spec in self.targets.items():
            x, y, w, h = spec["rect"]
            pad = 1 if spec.get("kind") == "text" else 0
            inside = (x - pad) <= ex < (x + w + pad) and (y - pad) <= ey < (y + h + pad)
            cxm, cym = x + w / 2.0, y + h / 2.0
            d = ((ex - cxm) ** 2 + (ey - cym) ** 2) ** 0.5
            if inside and (hit_id is None or d < (nearest_d or 1e9)):
                hit_id = tid
                hit_decoy = bool(spec.get("decoy", False))
            if nearest_d is None or d < nearest_d:
                nearest_d = d
                nearest_id = tid
                nearest_center = [round(ox + cxm, 1), round(oy + STATUS_H + cym, 1)]
        self.status = f"{self.title}: click ({sx},{sy}) -> {hit_id or 'MISS'}"
        self._log(
            {
                "kind": "click_receipt",
                "seq": self.seq,
                "ts": time.time(),
                "button": button,
                "screen": [sx, sy],
                "hit_id": hit_id,
                "hit_decoy": hit_decoy,
                "nearest_id": nearest_id,
                "nearest_center": nearest_center,
                "nearest_distance_px": round(nearest_d, 2) if nearest_d is not None else None,
            }
        )
        self._log(
            {
                "kind": "any_click",
                "seq": self.seq,
                "ts": time.time(),
                "screen": [sx, sy],
                "widget": "win32",
            }
        )
        if hit_id is not None:
            x, y, w, h = self.targets[hit_id]["rect"]
            self.flash = (x - 3, y + STATUS_H - 3, w + 6, h + 6)
        else:
            self.flash = (ex - 8, cy - 8, 16, 16)
        user32.SetTimer(self.hwnd, TIMER_FLASH, 200, None)
        self._repaint()

    # ------------------------------------------------------------- paint
    def _repaint(self) -> None:
        user32.InvalidateRect(self.hwnd, None, False)
        # Paint NOW: the runner captures the window right after the layout
        # snapshot, and an async WM_PAINT would leave it half drawn.
        user32.UpdateWindow(self.hwnd)

    def _paint(self) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            rc = wintypes.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(rc))
            bg = gdi32.CreateSolidBrush(colorref(self.bg))
            user32.FillRect(hdc, ctypes.byref(rc), bg)
            gdi32.DeleteObject(bg)
            strip = wintypes.RECT(0, 0, rc.right, STATUS_H)
            dark = gdi32.CreateSolidBrush(colorref("#1f2933"))
            user32.FillRect(hdc, ctypes.byref(strip), dark)
            gdi32.DeleteObject(dark)
            gdi32.SetBkMode(hdc, TRANSPARENT)
            old = gdi32.SelectObject(hdc, self._font(9))
            gdi32.SetTextColor(hdc, colorref("#e6e8eb"))
            srect = wintypes.RECT(6, 5, rc.right - 6, STATUS_H)
            user32.DrawTextW(
                hdc,
                self.status,
                -1,
                ctypes.byref(srect),
                DT_LEFT | DT_TOP | DT_SINGLELINE | DT_NOCLIP,
            )
            for spec in self.targets.values():
                x, y, w, h = spec["rect"]
                if spec.get("kind") == "dot":
                    br = gdi32.CreateSolidBrush(colorref(spec.get("color", "#ff0000")))
                    r = wintypes.RECT(x, y + STATUS_H, x + w, y + STATUS_H + h)
                    user32.FillRect(hdc, ctypes.byref(r), br)
                    gdi32.DeleteObject(br)
                else:
                    gdi32.SelectObject(hdc, self._font(int(spec.get("size", 9))))
                    gdi32.SetTextColor(hdc, colorref(spec.get("color", "#202020")))
                    r = wintypes.RECT(x, y + STATUS_H, x + w, y + STATUS_H + h)
                    user32.DrawTextW(
                        hdc,
                        str(spec.get("label", "")),
                        -1,
                        ctypes.byref(r),
                        DT_LEFT | DT_TOP | DT_SINGLELINE | DT_NOCLIP,
                    )
            if self.flash is not None:
                fx, fy, fw, fh = self.flash
                # Neutral gray, deliberately far from every DOT_COLORS entry so a
                # color search for the next target cannot match the flash.
                pen = gdi32.CreatePen(0, 2, colorref("#6b6b6b"))
                oldpen = gdi32.SelectObject(hdc, pen)
                oldbr = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
                gdi32.Rectangle(hdc, fx, fy, fx + fw, fy + fh)
                gdi32.SelectObject(hdc, oldbr)
                gdi32.SelectObject(hdc, oldpen)
                gdi32.DeleteObject(pen)
            gdi32.SelectObject(hdc, old)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    # ------------------------------------------------------------ wndproc
    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                self._paint()
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_TIMER:
                if wparam == TIMER_CMDS:
                    self._poll_commands()
                elif wparam == TIMER_HEARTBEAT:
                    self._emit_layout("heartbeat")
                elif wparam == TIMER_FLASH:
                    user32.KillTimer(hwnd, TIMER_FLASH)
                    self.flash = None
                    self._repaint()
                return 0
            if msg in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._on_click(lo(lparam), hi(lparam), 1 if msg == WM_LBUTTONUP else 3)
                return 0
            if msg == WM_LBUTTONDOWN:
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self._log({"kind": "wndproc_error", "error": repr(e), "msg": int(msg)})
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        try:
            self.events_fp.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="IRIS_GAUNTLET_W32")
    ap.add_argument("--events", required=True)
    ap.add_argument("--commands", required=True)
    ap.add_argument("--geometry", default="720x520+100+100")
    ap.add_argument("--bg", default="#f2f2f2")
    args = ap.parse_args()
    h = Harness(args.title, Path(args.events), Path(args.commands), args.geometry, args.bg)
    try:
        h.run()
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
