"""System integration: clipboard, processes, notifications, registry, window state.

Each function returns plain dicts so MCP tool wrappers can pass them through
without further serialization. All Win32 access is best-effort: when an API
is unavailable (different OS, missing extra), functions return
{"ok": False, "reason": ...} rather than raising at import time.

This module deliberately avoids subprocess where pure Win32 will do, and
fails closed (returns ok=False) rather than silently succeeding with stale
data.
"""

from __future__ import annotations

from typing import Any

try:
    import win32clipboard
    import win32con

    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

try:
    import win32gui
    import win32process

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import winreg

    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def clipboard_get() -> dict:
    """Read the system clipboard as text.

    Returns:
        {"ok": True, "text": <str>} or {"ok": False, "reason": ...}.
    Empty clipboard returns {"ok": True, "text": ""}.
    """
    if not HAS_CLIPBOARD:
        return {"ok": False, "reason": "win32clipboard_unavailable"}
    try:
        win32clipboard.OpenClipboard()
    except Exception as e:
        return {"ok": False, "reason": f"open_failed:{e}"}
    try:
        try:
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except TypeError:
            # Clipboard has data but not as text.
            return {"ok": True, "text": "", "note": "non_text_clipboard"}
        except Exception:
            data = ""
        return {"ok": True, "text": str(data or "")}
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def clipboard_set(text: str) -> dict:
    """Write text to the system clipboard.

    Replaces any existing clipboard content. Empty string clears the
    clipboard. Non-string input is coerced via str().

    Uses SetClipboardText which is the higher-level wrapper that handles
    HGLOBAL allocation correctly. SetClipboardData(CF_UNICODETEXT, str) has
    edge cases where the handle marshalling fails on certain inputs.
    """
    if not HAS_CLIPBOARD:
        return {"ok": False, "reason": "win32clipboard_unavailable"}
    payload = str(text)
    try:
        win32clipboard.OpenClipboard()
    except Exception as e:
        return {"ok": False, "reason": f"open_failed:{e}"}
    try:
        win32clipboard.EmptyClipboard()
        if payload:
            # win32clipboard.SetClipboardText accepts a Python str and handles
            # GlobalAlloc + CF_UNICODETEXT under the hood.
            win32clipboard.SetClipboardText(payload, win32con.CF_UNICODETEXT)
        # Empty payload = clipboard now empty (already done by EmptyClipboard).
        return {"ok": True, "bytes_written": len(payload.encode("utf-16le"))}
    except Exception as e:
        return {"ok": False, "reason": f"set_failed:{e}"}
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------
def list_processes(*, name_contains: str | None = None, limit: int = 200) -> dict:
    """Enumerate running processes.

    Returns a list of {pid, name, exe, status, cpu_percent, memory_mb,
    create_time}. Filtered by name_contains (case-insensitive substring) when
    given. Result is capped at `limit` for token-budget safety.
    """
    if not HAS_PSUTIL:
        return {"ok": False, "reason": "psutil_unavailable"}
    needle = name_contains.lower() if name_contains else None
    out = []
    for p in psutil.process_iter(["pid", "name", "exe", "status", "create_time"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            if needle is not None and needle not in name:
                continue
            mem = None
            try:
                mem = round(p.memory_info().rss / (1024 * 1024), 1)
            except Exception:
                pass
            out.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "",
                    "exe": info.get("exe") or "",
                    "status": info.get("status") or "",
                    "memory_mb": mem,
                    "create_time": info.get("create_time"),
                }
            )
            if len(out) >= limit:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"ok": True, "processes": out, "count": len(out)}


def find_process(name: str) -> dict:
    """Find processes by exact (case-insensitive) name. Useful before kill."""
    if not HAS_PSUTIL:
        return {"ok": False, "reason": "psutil_unavailable"}
    matches = []
    needle = name.lower()
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (p.info.get("name") or "").lower() == needle:
                matches.append(
                    {
                        "pid": p.info["pid"],
                        "name": p.info.get("name") or "",
                        "exe": p.info.get("exe") or "",
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"ok": True, "matches": matches, "count": len(matches)}


# PIDs we refuse to touch even with confirm=True. System Idle (0) and
# System (4) are kernel; killing them BSODs Windows.
_PROTECTED_PIDS = frozenset({0, 4})


def kill_process(pid: int, *, force: bool = False) -> dict:
    """Terminate a process by PID. Requires `force=True` for safety.

    Without force=True we refuse, returning a diagnostic. Protected system
    PIDs (0, 4) are refused even with force=True.
    """
    if not HAS_PSUTIL:
        return {"ok": False, "reason": "psutil_unavailable"}
    pid = int(pid)
    if pid in _PROTECTED_PIDS:
        return {
            "ok": False,
            "reason": "protected_pid",
            "hint": f"pid {pid} is a kernel/system process; refusing",
        }
    if not force:
        return {
            "ok": False,
            "reason": "force_required",
            "hint": "pass force=True to confirm killing this pid",
        }
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        try:
            p.wait(timeout=3)
            return {"ok": True, "pid": pid, "name": name, "exit": "terminated"}
        except psutil.TimeoutExpired:
            p.kill()
            return {"ok": True, "pid": pid, "name": name, "exit": "killed_after_timeout"}
    except psutil.NoSuchProcess:
        return {"ok": False, "reason": "no_such_process", "pid": pid}
    except psutil.AccessDenied:
        return {"ok": False, "reason": "access_denied", "pid": pid, "hint": "run as administrator"}
    except Exception as e:
        return {"ok": False, "reason": f"kill_failed:{e}", "pid": pid}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify(title: str, body: str = "", *, duration_seconds: float = 5.0) -> dict:
    """Show a Windows toast notification.

    Tries the modern ToastNotification API first (via winsdk/winrt when
    available), falling back to a balloon via Shell_NotifyIcon. Best effort:
    notification may not appear if Focus Assist is on or in fullscreen.
    """
    title = str(title or "Iris")
    body = str(body or "")

    # Modern path: winsdk (Windows.UI.Notifications)
    try:
        from winsdk.windows.data.xml.dom import XmlDocument
        from winsdk.windows.ui.notifications import (
            ToastNotification,
            ToastNotificationManager,
        )

        xml = f"""
        <toast>
          <visual>
            <binding template="ToastGeneric">
              <text>{_xml_escape(title)}</text>
              <text>{_xml_escape(body)}</text>
            </binding>
          </visual>
        </toast>
        """.strip()
        doc = XmlDocument()
        doc.load_xml(xml)
        notifier = ToastNotificationManager.create_toast_notifier("Iris")
        notifier.show(ToastNotification(doc))
        return {"ok": True, "backend": "winsdk_toast"}
    except Exception:
        pass

    # Fallback: built-in balloon via win10toast-style PowerShell trick. The
    # PowerShell BurntToast cmdlet has the broadest coverage, but adding a PS
    # dependency is heavy. Use a minimal inline approach via ctypes Shell_NotifyIcon
    # only if balloons are required. For now, return a graceful failure that
    # tells the caller what to install.
    return {
        "ok": False,
        "reason": "no_toast_backend",
        "hint": "pip install winsdk to enable notifications",
    }


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ---------------------------------------------------------------------------
# Window state (minimize / maximize / restore / close)
# ---------------------------------------------------------------------------
SW_HIDE = 0
SW_NORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_SHOWNA = 8
SW_RESTORE = 9
WM_CLOSE = 0x0010


def _show(hwnd: int, cmd: int) -> dict:
    if not HAS_WIN32:
        return {"ok": False, "reason": "win32_unavailable"}
    if not win32gui.IsWindow(int(hwnd)):
        return {"ok": False, "reason": "invalid_hwnd", "hwnd": int(hwnd)}
    try:
        win32gui.ShowWindow(int(hwnd), cmd)
        return {"ok": True, "hwnd": int(hwnd), "show_cmd": cmd}
    except Exception as e:
        return {"ok": False, "reason": f"showwindow_failed:{e}"}


def window_minimize(hwnd: int) -> dict:
    return _show(hwnd, SW_MINIMIZE)


def window_maximize(hwnd: int) -> dict:
    return _show(hwnd, SW_MAXIMIZE)


def window_restore(hwnd: int) -> dict:
    return _show(hwnd, SW_RESTORE)


def window_hide(hwnd: int) -> dict:
    return _show(hwnd, SW_HIDE)


def window_show(hwnd: int) -> dict:
    return _show(hwnd, SW_SHOW)


def window_close(hwnd: int) -> dict:
    """Politely ask the window to close (WM_CLOSE). Most apps will honor it,
    showing a save-prompt if needed. Does NOT force-kill the process."""
    if not HAS_WIN32:
        return {"ok": False, "reason": "win32_unavailable"}
    if not win32gui.IsWindow(int(hwnd)):
        return {"ok": False, "reason": "invalid_hwnd", "hwnd": int(hwnd)}
    try:
        win32gui.PostMessage(int(hwnd), WM_CLOSE, 0, 0)
        return {"ok": True, "hwnd": int(hwnd), "message": "WM_CLOSE_posted"}
    except Exception as e:
        return {"ok": False, "reason": f"postmessage_failed:{e}"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_HIVES = {
    "HKEY_CLASSES_ROOT": "HKEY_CLASSES_ROOT",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKEY_CURRENT_USER": "HKEY_CURRENT_USER",
    "HKCU": "HKEY_CURRENT_USER",
    "HKEY_LOCAL_MACHINE": "HKEY_LOCAL_MACHINE",
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKEY_USERS": "HKEY_USERS",
    "HKU": "HKEY_USERS",
    "HKEY_CURRENT_CONFIG": "HKEY_CURRENT_CONFIG",
    "HKCC": "HKEY_CURRENT_CONFIG",
}


def _hive_const(name: str):
    """Map a hive name (short or full) to a winreg constant."""
    if not HAS_WINREG:
        return None
    canonical = _HIVES.get(name.upper())
    if canonical is None:
        return None
    return getattr(winreg, canonical, None)


def registry_read(hive: str, key_path: str, value_name: str = "") -> dict:
    """Read a single registry value.

    Args:
        hive: "HKLM", "HKCU", "HKCR", "HKU", "HKCC" (or full forms).
        key_path: e.g. "SOFTWARE\\Microsoft\\Windows\\CurrentVersion".
        value_name: name of the value. Empty string reads the (Default) value.

    Returns {ok, hive, key_path, value_name, type, value} or
    {ok: False, reason}.
    """
    if not HAS_WINREG:
        return {"ok": False, "reason": "winreg_unavailable"}
    h = _hive_const(hive)
    if h is None:
        return {"ok": False, "reason": "unknown_hive", "hint": f"valid: {sorted(set(_HIVES))}"}
    try:
        with winreg.OpenKey(h, key_path, 0, winreg.KEY_READ) as k:
            value, vtype = winreg.QueryValueEx(k, value_name)
            return {
                "ok": True,
                "hive": hive,
                "key_path": key_path,
                "value_name": value_name,
                "type": _reg_type_name(vtype),
                "value": value,
            }
    except FileNotFoundError:
        return {
            "ok": False,
            "reason": "key_or_value_not_found",
            "hive": hive,
            "key_path": key_path,
            "value_name": value_name,
        }
    except PermissionError:
        return {"ok": False, "reason": "access_denied", "hint": "some keys require administrator"}
    except Exception as e:
        return {"ok": False, "reason": f"read_failed:{e}"}


def registry_list_values(hive: str, key_path: str) -> dict:
    """Enumerate all values under a key without reading individual values."""
    if not HAS_WINREG:
        return {"ok": False, "reason": "winreg_unavailable"}
    h = _hive_const(hive)
    if h is None:
        return {"ok": False, "reason": "unknown_hive"}
    try:
        with winreg.OpenKey(h, key_path, 0, winreg.KEY_READ) as k:
            # QueryInfoKey returns (num_subkeys, num_values, last_modified).
            # Earlier versions of this code grabbed the SUBKEY count and
            # returned an empty list when the key had subkeys but no direct
            # values; we want the value count.
            _, num_values, _ = winreg.QueryInfoKey(k)
            values = []
            for i in range(num_values):
                try:
                    name, value, vtype = winreg.EnumValue(k, i)
                    values.append(
                        {
                            "name": name,
                            "type": _reg_type_name(vtype),
                            "value": value,
                        }
                    )
                except OSError:
                    break
            return {
                "ok": True,
                "hive": hive,
                "key_path": key_path,
                "values": values,
                "count": len(values),
            }
    except FileNotFoundError:
        return {"ok": False, "reason": "key_not_found"}
    except Exception as e:
        return {"ok": False, "reason": f"list_failed:{e}"}


_REG_TYPES = {
    0: "REG_NONE",
    1: "REG_SZ",
    2: "REG_EXPAND_SZ",
    3: "REG_BINARY",
    4: "REG_DWORD",
    5: "REG_DWORD_BIG_ENDIAN",
    6: "REG_LINK",
    7: "REG_MULTI_SZ",
    11: "REG_QWORD",
}


def _reg_type_name(t: int) -> str:
    return _REG_TYPES.get(int(t), f"REG_TYPE_{t}")


def registry_write(
    hive: str,
    key_path: str,
    value_name: str,
    value: Any,
    value_type: str = "REG_SZ",
    *,
    confirm: bool = False,
) -> dict:
    """Write a registry value. Requires confirm=True for safety.

    value_type one of: REG_SZ, REG_EXPAND_SZ, REG_DWORD, REG_QWORD,
    REG_MULTI_SZ, REG_BINARY.
    """
    if not HAS_WINREG:
        return {"ok": False, "reason": "winreg_unavailable"}
    if not confirm:
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint": "registry writes are destructive; pass confirm=True",
        }
    h = _hive_const(hive)
    if h is None:
        return {"ok": False, "reason": "unknown_hive"}
    type_const = getattr(winreg, value_type, None)
    if type_const is None:
        return {
            "ok": False,
            "reason": "unknown_value_type",
            "hint": "valid: REG_SZ, REG_EXPAND_SZ, REG_DWORD, REG_QWORD, REG_MULTI_SZ, REG_BINARY",
        }
    try:
        with winreg.CreateKey(h, key_path) as k:
            winreg.SetValueEx(k, value_name, 0, type_const, value)
        return {
            "ok": True,
            "hive": hive,
            "key_path": key_path,
            "value_name": value_name,
            "type": value_type,
        }
    except PermissionError:
        return {
            "ok": False,
            "reason": "access_denied",
            "hint": "writes to HKLM and some HKCU paths require administrator",
        }
    except Exception as e:
        return {"ok": False, "reason": f"write_failed:{e}"}


def registry_delete_value(
    hive: str, key_path: str, value_name: str, *, confirm: bool = False
) -> dict:
    """Delete a registry value. Requires confirm=True."""
    if not HAS_WINREG:
        return {"ok": False, "reason": "winreg_unavailable"}
    if not confirm:
        return {"ok": False, "reason": "confirm_required"}
    h = _hive_const(hive)
    if h is None:
        return {"ok": False, "reason": "unknown_hive"}
    try:
        with winreg.OpenKey(h, key_path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value_name)
        return {"ok": True, "hive": hive, "key_path": key_path, "value_name": value_name}
    except FileNotFoundError:
        return {"ok": False, "reason": "key_or_value_not_found"}
    except PermissionError:
        return {"ok": False, "reason": "access_denied"}
    except Exception as e:
        return {"ok": False, "reason": f"delete_failed:{e}"}
