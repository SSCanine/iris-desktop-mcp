"""Semantic layer: UIA queries and pattern invocation.

Asks Windows directly what controls a window has, instead of looking at pixels.
Falls back gracefully (returns empty) for windows with no usable UIA tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from iris.geometry import Rect

try:
    import uiautomation as auto

    HAS_UIA = True
except ImportError:
    HAS_UIA = False


@dataclass(frozen=True)
class UIAControl:
    name: str
    role: str  # ControlTypeName, e.g. "ButtonControl"
    automation_id: str
    class_name: str
    bounds: Rect  # screen-absolute
    enabled: bool
    is_offscreen: bool
    raw: object = None  # the underlying uiautomation.Control (not serialized)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "bounds": self.bounds.to_dict(),
            "enabled": self.enabled,
            "is_offscreen": self.is_offscreen,
        }


def _control_to_uia(control) -> UIAControl | None:
    if control is None:
        return None
    try:
        rect = control.BoundingRectangle
        bounds = Rect.from_ltrb(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        return UIAControl(
            name=str(control.Name or ""),
            role=str(control.ControlTypeName or ""),
            automation_id=str(control.AutomationId or ""),
            class_name=str(control.ClassName or ""),
            bounds=bounds,
            enabled=bool(control.IsEnabled),
            is_offscreen=bool(control.IsOffscreen),
            raw=control,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# UIA-support cache (per-pid)
# ---------------------------------------------------------------------------
_UIA_SUPPORT_CACHE: dict[int, bool] = {}


def reset_uia_support_cache() -> None:
    _UIA_SUPPORT_CACHE.clear()


def control_for_hwnd(hwnd: int):
    if not HAS_UIA:
        return None
    try:
        return auto.ControlFromHandle(hwnd)
    except Exception:
        return None


def control_from_point(x: int, y: int) -> UIAControl | None:
    """Return the UIA control under screen-absolute (x, y), or None.

    Used to upgrade OCR text hits to the enclosing clickable widget. The text
    bbox returned by Tesseract is the glyph bounds, not the button surface, so
    clicking the text center can miss buttons with offset labels (icon+text,
    list rows, etc.). Asking UIA "what's under this point" lets us click the
    widget center instead, and lets us call Invoke() to bypass the mouse.
    """
    if not HAS_UIA:
        return None
    try:
        ctrl = auto.ControlFromPoint(int(x), int(y))
    except Exception:
        return None
    return _control_to_uia(ctrl)


def _root_hwnd(hwnd: int) -> int:
    """Top-level ancestor of a window handle (GA_ROOT)."""
    try:
        import ctypes

        root = ctypes.windll.user32.GetAncestor(int(hwnd), 2)  # GA_ROOT
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def owner_hwnd(control: UIAControl | None) -> int | None:
    """Top-level window handle that owns a UIA control, or None.

    Walks up the UIA parent chain until a node exposes a NativeWindowHandle
    (most leaf controls in modern apps do not have their own hwnd), then
    resolves that handle to its top-level root.
    """
    if control is None or control.raw is None:
        return None
    cur = control.raw
    for _ in range(40):
        if cur is None:
            return None
        try:
            h = int(getattr(cur, "NativeWindowHandle", 0) or 0)
        except Exception:
            h = 0
        if h:
            return _root_hwnd(h)
        try:
            cur = cur.GetParentControl()
        except Exception:
            return None
    return None


def control_in_window(control: UIAControl | None, hwnd: int) -> bool:
    """True when `control` belongs to the top-level window `hwnd`.

    ControlFromPoint hit-tests the whole desktop; when another window overlaps
    the token's window the control under the point belongs to THAT window.
    Callers use this to refuse the OCR->UIA widget upgrade in that case. If
    ownership cannot be determined we return True (do not lose the upgrade
    over a UIA hiccup); the resolver's click clamp still guards the click.
    """
    owner = owner_hwnd(control)
    if owner is None:
        return True
    return owner == _root_hwnd(hwnd)


# Classes whose UIA Invoke pattern is structurally broken: it returns success
# but the underlying widget never fires its click handler. Currently this is
# Tk (which uses Win32 class "Button" inside "TkChild" panes but draws the
# button itself, so the OS-synthesized invoke goes nowhere). When we see one
# of these in the ancestor chain we refuse to invoke and force a geometric
# mouse click instead. Add other offenders here as the bench finds them.
_INVOKE_DENYLIST_CLASSES = frozenset(
    {
        "TkTopLevel",
        "TkChild",
    }
)


def _has_denylisted_ancestor(raw_control, max_levels: int = 10) -> bool:
    """Walk up the UIA parent chain looking for a class name we know has
    broken Invoke. We start at the control itself so a denylisted control
    that's its own root still gets caught."""
    cur = raw_control
    for _ in range(max_levels):
        if cur is None:
            return False
        try:
            cls = str(getattr(cur, "ClassName", "") or "")
        except Exception:
            cls = ""
        if cls in _INVOKE_DENYLIST_CLASSES:
            return True
        try:
            cur = cur.GetParentControl()
        except Exception:
            return False
    return False


def is_invokable(control: UIAControl) -> bool:
    """True if a UIAControl exposes a click-equivalent pattern.

    Invoke (buttons), Toggle (checkboxes/toggle buttons), SelectionItem (list
    items, tabs), and ExpandCollapse (combo boxes, tree nodes) all let a
    caller activate the control without simulating a mouse click.

    This is the LENIENT check used by find_clickable_ancestor() to decide
    "is this a button-shaped thing?" so the OCR widget-upgrade can still
    swap text bounds for widget bounds. Use is_invoke_trusted() before
    actually calling Invoke (denylist-aware).
    """
    if control is None or control.raw is None:
        return False
    raw = control.raw
    for getter in (
        "GetInvokePattern",
        "GetTogglePattern",
        "GetSelectionItemPattern",
        "GetExpandCollapsePattern",
    ):
        try:
            fn = getattr(raw, getter, None)
            if fn is None:
                continue
            p = fn()
            if p is not None:
                return True
        except Exception:
            continue
    return False


def is_invoke_trusted(control: UIAControl) -> bool:
    """True if is_invokable(control) AND the control's ancestor chain does
    NOT include a class with known-broken Invoke (Tk widgets fake UIA but
    don't fire their command on synthesized invoke). Use this to decide
    whether to call Invoke vs fall back to a geometric mouse click.
    """
    if not is_invokable(control):
        return False
    return not _has_denylisted_ancestor(control.raw)


def find_clickable_ancestor(control: UIAControl, max_levels: int = 6) -> UIAControl | None:
    """Walk up the UIA parent chain looking for the smallest invokable ancestor.

    A label inside a button is not itself invokable; its parent Button is.
    A label inside a list item is not invokable; the ListItem is. Walking up
    finds the actual click target without overshooting (we stop at the first
    invokable, not the root).
    """
    if control is None or control.raw is None:
        return None
    if is_invokable(control):
        return control
    cur = control.raw
    for _ in range(max_levels):
        try:
            parent = cur.GetParentControl()
        except Exception:
            return None
        if parent is None:
            return None
        wrapped = _control_to_uia(parent)
        if wrapped is None:
            return None
        if is_invokable(wrapped):
            return wrapped
        cur = parent
    return None


def supports_uia(hwnd: int, pid: int) -> bool:
    """Probe whether this window/pid has any usable UIA children. Cached per pid."""
    if not HAS_UIA:
        return False
    cached = _UIA_SUPPORT_CACHE.get(pid)
    if cached is not None:
        return cached
    root = control_for_hwnd(hwnd)
    if root is None:
        _UIA_SUPPORT_CACHE[pid] = False
        return False
    try:
        children = root.GetChildren()
        ok = bool(children)
    except Exception:
        ok = False
    _UIA_SUPPORT_CACHE[pid] = ok
    return ok


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
# UIA property ids fetched in one round trip by the cached query.
_P_NAME, _P_CONTROLTYPE, _P_RECT = 30005, 30003, 30001
_P_AUTOMATIONID, _P_CLASSNAME, _P_ENABLED, _P_OFFSCREEN = 30011, 30012, 30010, 30022
_TREESCOPE_SUBTREE = 7  # element + descendants (the old walk included the root too)
_PCF_IGNORECASE_SUBSTRING = 1 | 2
_ROLE_TO_TYPE: dict[str, int] = {}


def _uia_client():
    return auto.uiautomation._AutomationClient.instance().IUIAutomation


def _control_type_id(role: str) -> int | None:
    if not _ROLE_TO_TYPE:
        for tid, tname in getattr(auto, "ControlTypeNames", {}).items():
            _ROLE_TO_TYPE[str(tname)] = int(tid)
    return _ROLE_TO_TYPE.get(role)


def _query_cached(
    hwnd: int,
    *,
    name: str | None,
    role: str | None,
    automation_id: str | None,
    class_name: str | None,
    max_results: int,
) -> list[UIAControl] | None:
    """One FindAllBuildCache call: the provider evaluates the filters and
    returns every property we need in a single cross-process round trip,
    instead of 7 COM calls per node from a Python walk. Measured 2026-09-29:
    File Explorer name search 1.83 s -> 0.74 s, full enumeration 2.2 s ->
    1.3 s; it also reaches deep Electron trees the depth-limited walk missed.
    Returns None when the fast path cannot run (caller falls back)."""
    uia = _uia_client()
    root = uia.ElementFromHandle(int(hwnd))
    if root is None:
        return None
    cr = uia.CreateCacheRequest()
    for pid in (
        _P_NAME,
        _P_CONTROLTYPE,
        _P_RECT,
        _P_AUTOMATIONID,
        _P_CLASSNAME,
        _P_ENABLED,
        _P_OFFSCREEN,
    ):
        cr.AddProperty(pid)
    conds = []
    if name:
        conds.append(uia.CreatePropertyConditionEx(_P_NAME, name, _PCF_IGNORECASE_SUBSTRING))
    if role is not None:
        tid = _control_type_id(role)
        if tid is None:
            return []
        conds.append(uia.CreatePropertyCondition(_P_CONTROLTYPE, tid))
    if automation_id is not None:
        conds.append(uia.CreatePropertyCondition(_P_AUTOMATIONID, automation_id))
    if class_name is not None:
        conds.append(uia.CreatePropertyCondition(_P_CLASSNAME, class_name))
    cond = conds[0] if conds else uia.CreateTrueCondition()
    for c in conds[1:]:
        cond = uia.CreateAndCondition(cond, c)
    arr = root.FindAllBuildCache(_TREESCOPE_SUBTREE, cond, cr)
    names = getattr(auto, "ControlTypeNames", {})
    out: list[UIAControl] = []
    for i in range(min(int(arr.Length), int(max_results))):
        e = arr.GetElement(i)
        try:
            r = e.CachedBoundingRectangle
            out.append(
                UIAControl(
                    name=str(e.CachedName or ""),
                    role=str(names.get(int(e.CachedControlType), "")),
                    automation_id=str(e.CachedAutomationId or ""),
                    class_name=str(e.CachedClassName or ""),
                    bounds=Rect.from_ltrb(int(r.left), int(r.top), int(r.right), int(r.bottom)),
                    enabled=bool(e.CachedIsEnabled),
                    is_offscreen=bool(e.CachedIsOffscreen),
                    raw=auto.Control.CreateControlFromElement(e),
                )
            )
        except Exception:
            continue
    return out


def query(
    hwnd: int,
    *,
    name: str | None = None,
    role: str | None = None,
    automation_id: str | None = None,
    class_name: str | None = None,
    max_depth: int = 8,
    max_results: int = 50,
) -> list[UIAControl]:
    """Return the controls of hwnd's UIA tree that match every given filter
    (case-insensitive substring for name, exact for role/class/automation_id).

    Fast path: one cached FindAll evaluated inside the provider process
    (_query_cached). The Python walk below is the fallback when the COM
    interface is unavailable or the call fails.
    """
    if not HAS_UIA:
        return []
    filtered = any(v is not None for v in (name, role, automation_id, class_name))
    # Filtered searches (the find/click hot path) take the fast path: on every
    # window measured it returned all the walk's matches, plus deep Electron
    # ones the walk missed. Unfiltered enumeration (marks) stays on the walk:
    # the two return different element sets there (tree view + depth limit),
    # stable run to run, and the difference is not yet understood.
    if filtered and os.environ.get("IRIS_UIA_WALK") != "1":
        try:
            fast = _query_cached(
                hwnd,
                name=name,
                role=role,
                automation_id=automation_id,
                class_name=class_name,
                max_results=max_results,
            )
            if fast is not None:
                return fast
        except Exception:
            pass
    root = control_for_hwnd(hwnd)
    if root is None:
        return []

    name_l = name.lower() if name else None
    out: list[UIAControl] = []

    def matches(c: UIAControl) -> bool:
        if name_l is not None and name_l not in c.name.lower():
            return False
        if role is not None and c.role != role:
            return False
        if automation_id is not None and c.automation_id != automation_id:
            return False
        if class_name is not None and c.class_name != class_name:
            return False
        return True

    def walk(node, depth: int):
        if depth > max_depth or len(out) >= max_results:
            return
        c = _control_to_uia(node)
        if c is not None and matches(c):
            out.append(c)
            if len(out) >= max_results:
                return
        try:
            children = node.GetChildren()
        except Exception:
            return
        for child in children:
            if len(out) >= max_results:
                break
            walk(child, depth + 1)

    walk(root, 0)
    return out


def walk_tree(hwnd: int, *, max_depth: int = 6, max_nodes: int = 500) -> list[dict]:
    """Full hierarchical UIA dump for discover(). Returns flattened list with depth."""
    if not HAS_UIA:
        return []
    root = control_for_hwnd(hwnd)
    if root is None:
        return []
    out: list[dict] = []

    def walk(node, depth: int):
        if depth > max_depth or len(out) >= max_nodes:
            return
        c = _control_to_uia(node)
        if c is not None:
            d = c.to_dict()
            d["depth"] = depth
            out.append(d)
        try:
            children = node.GetChildren()
        except Exception:
            return
        for child in children:
            if len(out) >= max_nodes:
                break
            walk(child, depth + 1)

    walk(root, 0)
    return out


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------
def try_pattern_click(control: UIAControl) -> dict:
    """Activate a control via UIA pattern, no mouse. Tries Invoke, Toggle,
    SelectionItem.Select, ExpandCollapse.Expand in priority order. Returns
    {ok: bool, pattern: str} or {ok: False, reason: str}.

    Unlike invoke(action='click'), this does NOT fall back to a positional
    click. Callers that want a guaranteed-fire click should handle the
    fallback themselves (so they control which mouse API runs).
    """
    if control is None or control.raw is None:
        return {"ok": False, "reason": "no_control"}
    raw = control.raw
    attempts = [
        ("GetInvokePattern", "Invoke", "invoke"),
        ("GetTogglePattern", "Toggle", "toggle"),
        ("GetSelectionItemPattern", "Select", "selection_item"),
        ("GetExpandCollapsePattern", "Expand", "expand_collapse"),
    ]
    failures: list[str] = []
    for getter, action_name, label in attempts:
        try:
            fn = getattr(raw, getter, None)
            if fn is None:
                continue
            p = fn()
            if p is None:
                continue
            getattr(p, action_name)()
            return {"ok": True, "pattern": label}
        except Exception as e:
            # Keep trying the remaining patterns: a control whose Invoke
            # raises may still Toggle or Select fine.
            failures.append(f"{label}_failed:{type(e).__name__}:{e}")
            continue
    if failures:
        return {"ok": False, "reason": ";".join(failures)}
    return {"ok": False, "reason": "no_clickable_pattern"}


def invoke(control: UIAControl, action: str = "click", value: str | None = None) -> dict:
    """Invoke an action on a control using the right UIA pattern.

    actions:
        click   -> InvokePattern, fallback to TogglePattern, fallback to bbox click via pyautogui
        toggle  -> TogglePattern
        set     -> ValuePattern.SetValue(value)
        expand  -> ExpandCollapsePattern.Expand()
        select  -> SelectionItemPattern.Select()
    """
    if control is None or control.raw is None:
        return {"ok": False, "reason": "no_control"}
    raw = control.raw
    try:
        if action == "click":
            # Try Invoke first
            if hasattr(raw, "GetInvokePattern"):
                p = raw.GetInvokePattern()
                if p is not None:
                    p.Invoke()
                    return {"ok": True, "pattern": "invoke"}
            if hasattr(raw, "GetTogglePattern"):
                p = raw.GetTogglePattern()
                if p is not None:
                    p.Toggle()
                    return {"ok": True, "pattern": "toggle"}
            # Fallback: positional click via center of bbox
            try:
                import pyautogui

                cx, cy = control.bounds.center
                pyautogui.click(cx, cy)
                return {"ok": True, "pattern": "positional"}
            except Exception:
                return {"ok": False, "reason": "no_invoke_or_toggle_pattern"}
        elif action == "toggle":
            p = raw.GetTogglePattern()
            if p is None:
                return {"ok": False, "reason": "no_toggle_pattern"}
            p.Toggle()
            return {"ok": True, "pattern": "toggle"}
        elif action == "set":
            p = raw.GetValuePattern()
            if p is None:
                return {"ok": False, "reason": "no_value_pattern"}
            p.SetValue(value or "")
            return {"ok": True, "pattern": "value", "value": value}
        elif action == "expand":
            p = raw.GetExpandCollapsePattern()
            if p is None:
                return {"ok": False, "reason": "no_expand_collapse_pattern"}
            p.Expand()
            return {"ok": True, "pattern": "expand_collapse"}
        elif action == "select":
            p = raw.GetSelectionItemPattern()
            if p is None:
                return {"ok": False, "reason": "no_selection_item_pattern"}
            p.Select()
            return {"ok": True, "pattern": "selection_item"}
        else:
            return {"ok": False, "reason": f"unknown_action:{action}"}
    except Exception as e:
        return {"ok": False, "reason": f"exception:{type(e).__name__}:{e}"}
