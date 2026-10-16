"""Panels: classify dock/panel containers from a UIA tree, with OCR cross-check.

Generic across Qt (QDockWidget, OBSDock), Win32, and most Electron layouts.
Pure-data: takes the output of semantic.walk_tree + vision.cached_ocr and returns
a ranked panel manifest. No live Win32 or UIA calls; safe to unit test.
"""

from __future__ import annotations

import re

# Class names that strongly indicate a dock/panel container.
# Case-sensitive on 'Dock' to avoid false positives from words like 'document'.
# Catches OBSDock, QDockWidget, MyDock, DockArea, etc.
_DOCK_CLASS_RE = re.compile(r"Dock")
# automation_id patterns produced by Qt/OBS: ends with "Dock" or "-dock"
_DOCK_ID_RE = re.compile(r"(?:Dock$|[-_.]dock$|[-_.]dock[-_]widget)", re.IGNORECASE)
# Roles that can host docks even without obvious class names
_PANEL_ROLES = {"PaneControl", "GroupControl", "WindowControl", "CustomControl"}
# Tab-bar siblings used to detect tabbed-stacked docks
_TABBAR_CLASS_RE = re.compile(r"(?:Q?MainWindowTabBar|QTabBar)", re.IGNORECASE)


def _bounds_dict(node: dict) -> dict:
    """Extract bounds dict with defaults so downstream math is total."""
    b = node.get("bounds") or {}
    return {
        "x": int(b.get("x", 0)),
        "y": int(b.get("y", 0)),
        "width": int(b.get("width", 0)),
        "height": int(b.get("height", 0)),
    }


def _area(b: dict) -> int:
    return max(b["width"], 0) * max(b["height"], 0)


def _is_hidden(b: dict) -> bool:
    """A dock with negative coords or near-zero size is hidden/minimized/tabbed-away."""
    return b["x"] < -1000 or b["y"] < -1000 or b["width"] < 50 or b["height"] < 20


def _is_dock_node(node: dict) -> bool:
    """Strong signal: class_name or automation_id matches a known dock pattern."""
    cn = node.get("class_name") or ""
    aid = node.get("automation_id") or ""
    if _DOCK_CLASS_RE.search(cn):
        return True
    if _DOCK_ID_RE.search(aid):
        return True
    return False


def _ocr_label_in_band(
    panel_bounds: dict, panel_name: str, ocr_words: list[dict], band_height: int = 40
) -> dict | None:
    """Find an OCR word inside the panel's title band that matches its name.

    Returns the matching OCR entry (dict with text/bbox/confidence) or None.
    """
    if not panel_name or not ocr_words:
        return None
    name_l = panel_name.lower().strip()
    if not name_l:
        return None
    px, py = panel_bounds["x"], panel_bounds["y"]
    pw = panel_bounds["width"]
    band_top, band_bot = py, py + band_height
    band_right = px + pw
    name_tokens = {t for t in re.split(r"\s+", name_l) if len(t) > 1}
    if not name_tokens:
        return None
    for w in ocr_words:
        b = w.get("bbox") or w.get("bounds") or {}
        wx = int(b.get("x", 0))
        wy = int(b.get("y", 0))
        if wx < px or wx > band_right:
            continue
        if wy < band_top or wy > band_bot:
            continue
        text_l = (w.get("text") or "").lower()
        if not text_l:
            continue
        text_tokens = set(re.split(r"\s+", text_l))
        if name_tokens & text_tokens:
            return w
    return None


def _detect_tabbed(panel_bounds: dict, all_nodes: list[dict]) -> bool:
    """A dock is 'tabbed' when a tab bar overlaps its bottom edge."""
    px, py = panel_bounds["x"], panel_bounds["y"]
    pw, ph = panel_bounds["width"], panel_bounds["height"]
    if pw <= 0 or ph <= 0:
        return False
    bottom = py + ph
    for n in all_nodes:
        cn = n.get("class_name") or ""
        if not _TABBAR_CLASS_RE.search(cn):
            continue
        b = _bounds_dict(n)
        if (
            abs((b["y"] + b["height"]) - bottom) <= 30
            and b["x"] >= px - 5
            and (b["x"] + b["width"]) <= (px + pw + 5)
        ):
            return True
    return False


def discover_panels(
    uia_tree: list[dict], ocr_words: list[dict], window_bounds: dict, *, min_area_pct: float = 0.05
) -> list[dict]:
    """Return ranked dock/panel containers in the window.

    Args:
        uia_tree: flat list from semantic.walk_tree(). Each node has name, role,
            automation_id, class_name, bounds, depth.
        ocr_words: list from vision.cached_ocr(). Each has text, bbox, confidence.
        window_bounds: bounds dict of the window itself; used for area-percentage
            filtering on weakly-typed candidates.
        min_area_pct: weak-candidate threshold (depth<=2 panes count if their
            area is at least this fraction of the window).
    """
    if not uia_tree:
        return []
    win_area = max(_area(window_bounds), 1)
    seen_keys: set[tuple] = set()
    panels: list[dict] = []

    for node in uia_tree:
        depth = int(node.get("depth", 99))
        role = node.get("role") or ""
        bounds = _bounds_dict(node)
        area = _area(bounds)
        # depth=0 is the window itself, never a panel inside it.
        if depth == 0:
            continue
        is_strong = _is_dock_node(node)
        is_weak = depth <= 2 and role in _PANEL_ROLES and (area / win_area) >= min_area_pct
        if not (is_strong or is_weak):
            continue
        # De-dup by (automation_id, name, bounds) to handle Qt's nested wrappers.
        key = (
            node.get("automation_id") or "",
            node.get("name") or "",
            bounds["x"],
            bounds["y"],
            bounds["width"],
            bounds["height"],
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)

        name = node.get("name") or ""
        ocr_hit = _ocr_label_in_band(bounds, name, ocr_words) if ocr_words else None
        if ocr_hit and is_strong:
            source = "uia+ocr"
            confidence = "very_high"
        elif is_strong:
            source = "uia"
            confidence = "high"
        elif ocr_hit:
            source = "ocr"
            confidence = "medium"
        else:
            source = "uia"
            confidence = "medium"

        panels.append(
            {
                "id": node.get("automation_id") or name or f"panel_{len(panels)}",
                "name": name,
                "automation_id": node.get("automation_id") or "",
                "class_name": node.get("class_name") or "",
                "role": role,
                "bounds": bounds,
                "source": source,
                "confidence": confidence,
                "hidden": _is_hidden(bounds),
                "tabbed": _detect_tabbed(bounds, uia_tree),
                "area": area,
                "depth": depth,
            }
        )

    # Stable sort: visible panels first, then by confidence rank, then area.
    rank = {"very_high": 3, "high": 2, "medium": 1}
    panels.sort(
        key=lambda p: (
            p["hidden"],
            -rank.get(p["confidence"], 0),
            -p["area"],
        )
    )
    return panels


# ---------------------------------------------------------------------------
# Panel item discovery
# ---------------------------------------------------------------------------
_CLICKABLE_ROLES = {
    "ButtonControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ListItemControl",
    "TreeItemControl",
    "TabItemControl",
    "MenuItemControl",
    "HyperlinkControl",
}


def _resolve_parent_panel(uia_tree: list[dict], panel_query: str) -> dict | None:
    """Find the panel node matching panel_query (id, automation_id, or name).

    Match priority: exact automation_id, exact name (ci), then substring.
    """
    if not panel_query or not uia_tree:
        return None
    q = panel_query.strip()
    q_l = q.lower()
    exact_aid = None
    exact_name = None
    substring = None
    for node in uia_tree:
        aid = node.get("automation_id") or ""
        name = node.get("name") or ""
        if aid == q:
            exact_aid = node
            break
        if name.lower() == q_l and exact_name is None:
            exact_name = node
        if (q_l in aid.lower() or q_l in name.lower()) and substring is None:
            substring = node
    return exact_aid or exact_name or substring


def _bbox_inside(child: dict, parent: dict, slack: int = 5) -> bool:
    return (
        child["x"] >= parent["x"] - slack
        and child["y"] >= parent["y"] - slack
        and (child["x"] + child["width"]) <= (parent["x"] + parent["width"] + slack)
        and (child["y"] + child["height"]) <= (parent["y"] + parent["height"] + slack)
    )


def _item_group_key(node: dict) -> str:
    """Group nested controls by their item-name segment in the automation_id.

    OBS mixer pattern: '...vVolumeWidgets.GoXLR Mic.VolumeName' and
    '...vVolumeWidgets.GoXLR Mic.QPushButton' both belong to item 'GoXLR Mic'.
    Falls back to the node name when automation_id offers no useful segment.
    """
    aid = node.get("automation_id") or ""
    name = node.get("name") or ""
    if "." in aid:
        # Take the longest non-generic segment that has a space or capital letter.
        segments = aid.split(".")
        for seg in reversed(segments[:-1]):
            if not seg:
                continue
            # Skip Qt boilerplate segments
            if seg.lower() in {"qpushbutton", "volumename", "qframe", "qwidget", "controlsframe"}:
                continue
            if " " in seg or any(c.isupper() for c in seg[1:]):
                return seg
    return name or aid or "?"


def discover_panel_items(uia_tree: list[dict], panel_query: str) -> dict:
    """List the actionable items inside a named panel.

    Returns dict with the resolved parent and a deduped item list. Each item
    aggregates its sub-controls (e.g., a mixer row's mute toggle + volume label
    collapse into one entry exposing both bounds for downstream targeting).
    """
    parent = _resolve_parent_panel(uia_tree, panel_query)
    if parent is None:
        return {"found": False, "reason": "panel_not_found", "query": panel_query}

    parent_bounds = _bounds_dict(parent)
    parent_depth = int(parent.get("depth", 0))

    # Collect descendants that fall inside the parent's bounds and are actionable.
    raw_items: list[dict] = []
    for node in uia_tree:
        depth = int(node.get("depth", 0))
        if depth <= parent_depth:
            continue
        role = node.get("role") or ""
        if role not in _CLICKABLE_ROLES:
            continue
        b = _bounds_dict(node)
        if _area(b) <= 0:
            continue
        if not _bbox_inside(b, parent_bounds):
            continue
        raw_items.append(node)

    # Group by item key, collapsing nested sub-controls of the same logical item.
    groups: dict[str, dict] = {}
    for node in raw_items:
        key = _item_group_key(node)
        b = _bounds_dict(node)
        entry = groups.get(key)
        sub = {
            "name": node.get("name") or "",
            "role": node.get("role") or "",
            "automation_id": node.get("automation_id") or "",
            "bounds": b,
        }
        if entry is None:
            groups[key] = {
                "key": key,
                "name": node.get("name") or key,
                "primary_role": node.get("role") or "",
                "bounds": dict(b),
                "controls": [sub],
            }
        else:
            entry["controls"].append(sub)
            # Expand the entry's bounding box to enclose all sub-controls.
            ex, ey = entry["bounds"]["x"], entry["bounds"]["y"]
            er = ex + entry["bounds"]["width"]
            eb = ey + entry["bounds"]["height"]
            nx, ny = b["x"], b["y"]
            nr, nb = nx + b["width"], ny + b["height"]
            entry["bounds"] = {
                "x": min(ex, nx),
                "y": min(ey, ny),
                "width": max(er, nr) - min(ex, nx),
                "height": max(eb, nb) - min(ey, ny),
            }

    items = list(groups.values())
    # Stable order: top-to-bottom, left-to-right.
    items.sort(key=lambda it: (it["bounds"]["y"], it["bounds"]["x"]))

    return {
        "found": True,
        "panel": {
            "id": parent.get("automation_id") or parent.get("name") or "",
            "name": parent.get("name") or "",
            "automation_id": parent.get("automation_id") or "",
            "bounds": parent_bounds,
        },
        "items": items,
        "item_count": len(items),
    }
