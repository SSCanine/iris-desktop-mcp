"""Set-of-Mark: number every element in a window so the model can say
"click 17" instead of estimating coordinates off a shrunk screenshot.

A Mark is one clickable or readable element with a screen rect, an id, a
short text, a role and where it came from (UIA control or OCR text run).
`collect_marks(token)` gathers them, dedupes, orders them top-to-bottom and
left-to-right, and stores the set per token so `click(token, mark=N)` can
resolve N later. `vision.draw_marks` renders the numbered tags.

Design notes
- UIA first: named, visible, non-container controls inside the window. The
  raw control is kept so a click can use Invoke (no mouse at all).
- OCR second: words on the same line that sit close together are merged
  into one run ("Start Recording" is one mark, not two). Runs that are
  mostly covered by a UIA mark are dropped as duplicates.
- Reading order: rows of ~1 text height, then x. Ids restart at 1 on every
  collection; a stale id after the window changed is caught by the capture
  digest stored with the set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from iris import semantic as semantic_mod
from iris import vision as vision_mod
from iris.geometry import Rect
from iris.tokens import FocusToken

MAX_MARKS = 150
# UIA roles worth a number. Containers and structure are left out; their
# children carry the marks.
_MARK_ROLES = {
    "button",
    "menuitem",
    "tabitem",
    "checkbox",
    "radiobutton",
    "hyperlink",
    "splitbutton",
    "combobox",
    "edit",
    "listitem",
    "treeitem",
    "dataitem",
    "headeritem",
    "slider",
    "spinner",
    "thumb",
    "scrollbar",
    "image",
    "text",
    "custom",
    "menu",
}
_SKIP_ROLES = {
    "window",
    "pane",
    "group",
    "document",
    "list",
    "tree",
    "table",
    "toolbar",
    "menubar",
    "titlebar",
    "statusbar",
    "separator",
    "header",
    "tab",
    "datagrid",
}
# Text/image nodes only earn a mark when they have a name; a nameless
# Custom/Text node is usually layout noise.
_NEEDS_NAME = {"text", "image", "custom", "menu"}


@dataclass
class Mark:
    id: int
    bbox: Rect  # screen-absolute
    text: str
    role: str  # normalized role: 'button', 'listitem', 'text' (ocr) ...
    source: str  # 'uia' | 'ocr'
    control: object | None = field(default=None, repr=False)  # UIAControl for invoke

    @property
    def center(self) -> tuple[int, int]:
        return self.bbox.center

    def to_dict(self) -> dict:
        cx, cy = self.bbox.center
        return {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "source": self.source,
            "bbox": self.bbox.to_dict(),
            "center": [cx, cy],
        }


@dataclass
class MarkSet:
    token_id: str
    hwnd: int
    marks: list[Mark]
    window_bounds: Rect
    capture_digest: str | None
    created_at: float

    def get(self, mark_id: int) -> Mark | None:
        for m in self.marks:
            if m.id == int(mark_id):
                return m
        return None

    def legend(self) -> list[dict]:
        return [m.to_dict() for m in self.marks]


_SETS: dict[str, MarkSet] = {}


def get_marks(token_id: str) -> MarkSet | None:
    return _SETS.get(token_id)


def clear_marks(token_id: str | None = None) -> None:
    if token_id is None:
        _SETS.clear()
    else:
        _SETS.pop(token_id, None)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _norm_role(role: str | None) -> str:
    r = (role or "").strip().lower()
    if r.endswith("control"):
        r = r[: -len("control")]
    return r


def _overlap_ratio(a: Rect, b: Rect) -> float:
    """Fraction of `a` covered by `b`."""
    if a.area <= 0:
        return 0.0
    return a.intersection_area(b) / a.area


def uia_candidates(controls: list, window: Rect) -> list[Mark]:
    """Turn UIA controls into unnumbered marks (id 0)."""
    out: list[Mark] = []
    seen: list[Rect] = []
    for c in controls:
        role = _norm_role(getattr(c, "role", ""))
        if role in _SKIP_ROLES or role not in _MARK_ROLES:
            continue
        b: Rect | None = getattr(c, "bounds", None)
        if b is None or b.width <= 0 or b.height <= 0:
            continue
        if getattr(c, "is_offscreen", False):
            continue
        if window.intersection_area(b) < b.area * 0.5:
            continue  # mostly outside the window (popup, stray)
        if b.area > window.area * 0.6:
            continue  # a control the size of the window is a container
        name = (getattr(c, "name", "") or "").strip()
        if role in _NEEDS_NAME and not name:
            continue
        # Same rect already marked (label inside its own button etc.).
        if any(
            abs(b.x - s.x) <= 2
            and abs(b.y - s.y) <= 2
            and abs(b.width - s.width) <= 4
            and abs(b.height - s.height) <= 4
            for s in seen
        ):
            continue
        seen.append(b)
        out.append(Mark(0, b, name[:80], role, "uia", control=c))
    return out


def ocr_runs(words: list, origin: tuple[int, int]) -> list[Mark]:
    """Merge adjacent same-line OCR words into runs; screen coords."""
    ox, oy = origin
    runs: list[Mark] = []
    cur: list = []

    def flush():
        if not cur:
            return
        x = min(w.bbox.left for w in cur)
        y = min(w.bbox.top for w in cur)
        r = max(w.bbox.right for w in cur)
        b = max(w.bbox.bottom for w in cur)
        text = " ".join(w.text for w in cur)
        runs.append(Mark(0, Rect(x + ox, y + oy, r - x, b - y), text[:80], "text", "ocr"))

    for w in words:
        if cur:
            prev = cur[-1]
            same_line = (
                (w.line == prev.line)
                if (w.line != -1 and prev.line != -1)
                else (abs(w.bbox.y - prev.bbox.y) <= max(4, prev.bbox.height // 2))
            )
            gap = w.bbox.left - prev.bbox.right
            close = -2 <= gap <= max(8, int(prev.bbox.height * 1.2))
            if same_line and close:
                cur.append(w)
                continue
            flush()
            cur = []
        cur.append(w)
    flush()
    return runs


def merge(uia: list[Mark], ocr: list[Mark], cap: int = MAX_MARKS) -> list[Mark]:
    """Drop OCR runs covered by a UIA mark, order in reading order, number."""
    kept: list[Mark] = list(uia)
    for run in ocr:
        covered = any(_overlap_ratio(run.bbox, u.bbox) >= 0.6 for u in uia)
        if not covered:
            kept.append(run)
    # Reading order: bucket rows by ~one text height so a row of buttons
    # numbers left-to-right even when their tops differ by a few px.
    row_h = 18
    if kept:
        hs = sorted(m.bbox.height for m in kept)
        row_h = max(12, hs[len(hs) // 2])
    kept.sort(key=lambda m: (m.bbox.y // row_h, m.bbox.x))
    if len(kept) > cap:
        # Keep UIA marks first (they are actionable), then the earliest OCR runs.
        u = [m for m in kept if m.source == "uia"][:cap]
        o = [m for m in kept if m.source == "ocr"][: max(0, cap - len(u))]
        kept = sorted(u + o, key=lambda m: (m.bbox.y // row_h, m.bbox.x))
    for i, m in enumerate(kept, start=1):
        m.id = i
    return kept


def collect_marks(
    token: FocusToken,
    *,
    include_uia: bool = True,
    include_ocr: bool = True,
    img=None,
    cap: int = MAX_MARKS,
) -> MarkSet:
    """Enumerate, number and remember the marks for a token's window.

    `img` may be passed when the caller already captured the window (see()),
    so the OCR pass reuses the exact pixels the user will look at.
    """
    live = token.current_bounds() or token.bounds_at_creation
    uia: list[Mark] = []
    if include_uia and semantic_mod.HAS_UIA:
        try:
            if semantic_mod.supports_uia(token.hwnd, token.pid):
                controls = semantic_mod.query(token.hwnd, max_depth=10, max_results=600)
                uia = uia_candidates(controls, live)
        except Exception:
            uia = []
    ocr: list[Mark] = []
    digest = None
    if include_ocr:
        try:
            if img is None:
                img = vision_mod.capture_window(token.hwnd)
            digest = vision_mod.exact_digest(img)
            words = vision_mod.cached_ocr(token.id, img)
            ocr = ocr_runs(words, (live.x, live.y))
        except Exception:
            ocr = []
    marks = merge(uia, ocr, cap=cap)
    ms = MarkSet(
        token_id=token.id,
        hwnd=token.hwnd,
        marks=marks,
        window_bounds=live,
        capture_digest=digest,
        created_at=time.time(),
    )
    _SETS[token.id] = ms
    return ms


def is_stale(ms: MarkSet, token: FocusToken) -> bool:
    """True when the window moved/resized or its pixels changed since the
    marks were taken. Cheap: one PrintWindow + digest."""
    live = token.current_bounds()
    if live is None or live != ms.window_bounds:
        return True
    if ms.capture_digest is None:
        return False
    try:
        img = vision_mod.capture_window(token.hwnd)
        return vision_mod.exact_digest(img) != ms.capture_digest
    except Exception:
        return False
