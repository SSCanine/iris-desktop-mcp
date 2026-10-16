"""Resolver: route find/click requests to the right backend.

Order:
    pixel   when the target uses the pixel grammar (color:#..., image:path)
    uia     when the window exposes accessibility and a control name matches
    ocr     PrintWindow capture + Tesseract, 1x then 2x upscale
    vision_handoff  screenshot + nearest matches so the caller can decide
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from iris import pixel as pixel_mod
from iris import semantic as semantic_mod
from iris import spatial as spatial_mod
from iris import telemetry
from iris import vision as vision_mod
from iris.tokens import FocusToken
from iris.tokens import inspect as token_inspect

# Token-overlap ranking helpers. Used to surface candidates that share
# concept words with the target even when raw edit-distance is low. Example:
# target='Mic/Aux' vs candidate='GoXLR Mic' shares 'mic' -> meaningful boost.
_TOKEN_SPLIT_RE = re.compile(r"[\s/\-_.,;:|\\()\[\]]+")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "you",
        "your",
        "are",
        "was",
        "but",
        "not",
        "any",
        "all",
    }
)

# How many UIA candidates to pull before ranking. The old code stopped at the
# first 10 substring matches in tree order, which in text-heavy apps (chat
# lists, feeds) never reached the actual button.
UIA_CANDIDATE_CAP = 100
# Invokability is a COM round-trip per candidate; only probe the best few.
UIA_INVOKE_PROBE_TOP = 15


def _tokenize(s: str) -> set[str]:
    """Lowercase tokens of length>2 with stopwords stripped."""
    if not s:
        return set()
    toks = _TOKEN_SPLIT_RE.split(s.lower())
    return {t for t in toks if len(t) > 2 and t not in _STOPWORDS}


def _token_overlap(target: str, candidate: str) -> float:
    """Fraction of target tokens that appear in candidate. 0.0..1.0."""
    t = _tokenize(target)
    if not t:
        return 0.0
    c = _tokenize(candidate)
    if not c:
        return 0.0
    return len(t & c) / len(t)


@dataclass
class FindResult:
    found: bool
    backend: str  # 'uia' | 'ocr' | 'pixel' | 'vision_handoff'
    hits: list[dict] = field(default_factory=list)
    nearest_matches: list[dict] = field(default_factory=list)
    screenshot: bytes | None = None
    screenshot_dims: tuple[int, int] | None = None
    backends_tried: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    drift_detected: bool = False
    drift_summary: dict | None = None
    # Independent state flags so the caller can self-correct off-screen clicks.
    # See plan 2026-04-27-iris-v2-phase15.
    window_state: dict | None = None
    # Parallel to `hits`: the live UIAControl (for backend='uia') or None.
    # Lets click() call control.Invoke() instead of moving the mouse, which
    # is the most reliable click possible (no pixels, no animation timing).
    # Not serialized via to_dict() because COM objects aren't JSON-safe.
    controls: list[object | None] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        d = {
            "found": self.found,
            "backend": self.backend,
            "hits": self.hits,
            "backends_tried": self.backends_tried,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "notes": self.notes,
        }
        if self.nearest_matches:
            d["nearest_matches"] = self.nearest_matches
        if self.screenshot is not None:
            d["screenshot_bytes_len"] = len(self.screenshot)
            d["screenshot_dims"] = list(self.screenshot_dims or ())
        if self.drift_detected:
            d["drift_detected"] = True
            d["drift_summary"] = self.drift_summary
        if self.window_state is not None:
            d["window_state"] = self.window_state
        if len(self.hits) > 1:
            d["ambiguous"] = True
        return d


def _capture_window(token: FocusToken) -> object | None:
    """Capture only the token's window. Returns PIL Image or None.

    Uses Win32 PrintWindow so the capture is the actual window contents even
    when the window is occluded by other windows. Falls back to bounds-based
    capture (using LIVE bounds, never bounds_at_creation) if PrintWindow fails.
    """
    try:
        return vision_mod.capture_window(token.hwnd)
    except Exception:
        live = token.current_bounds()
        if live is None:
            return None
        try:
            return vision_mod.capture(bounds=live)
        except Exception:
            return None


def _ocr_origin(token: FocusToken) -> tuple[int, int]:
    """Screen-absolute origin to add to OCR window-local coords.

    PrintWindow + GetWindowRect both work in WINDOW coords (top-left includes
    title bar). The captured image's (0, 0) corresponds to GetWindowRect's
    (left, top). We pull that NOW, not from token.bounds_at_creation, so a
    user-dragged window still maps OCR hits to the right screen pixels.

    Falls back to bounds_at_creation if the window is dead (we'll still return
    SOMETHING rather than 0,0, but the caller should already have bailed on a
    dead token via revalidate()).
    """
    live = token.current_bounds()
    if live is not None:
        return live.x, live.y
    return token.bounds_at_creation.x, token.bounds_at_creation.y


# ---------------------------------------------------------------------------
# UIA candidate ranking
# ---------------------------------------------------------------------------
# Positive = "this is the kind of thing people mean when they say click X".
_ROLE_WEIGHTS = {
    "button": 3,
    "menuitem": 3,
    "tabitem": 3,
    "checkbox": 3,
    "radiobutton": 3,
    "hyperlink": 3,
    "splitbutton": 3,
    "combobox": 2,
    "edit": 2,
    "listitem": 1,
    "treeitem": 1,
    "dataitem": 1,
    "headeritem": 1,
    "text": 0,
    "image": 0,
    "custom": 0,
    "pane": -1,
    "group": -1,
    "list": -2,
    "tree": -2,
    "table": -2,
    "toolbar": -2,
    "menubar": -2,
    "document": -2,
    "window": -3,
}


def normalize_role(role: str | None) -> str:
    r = (role or "").strip().lower()
    if r.endswith("control"):
        r = r[: -len("control")]
    return r


def _name_match_score(name: str, target: str) -> int:
    n = (name or "").strip().lower()
    t = (target or "").strip().lower()
    if not n or not t:
        return 0
    if n == t:
        return 4
    if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", n):
        # whole-word occurrence, e.g. 'Create' in 'Create channel'
        return 2 if not n.startswith(t) else 3
    return 1  # bare substring


def rank_uia_candidates(
    controls: list, target: str, prefer_roles: list[str] | None = None
) -> list[tuple[int, object]]:
    """Score and sort UIA matches. Returns [(score, control)] best first.

    Score = name exactness (x2) + role weight + visibility + invokability, plus
    a large bonus when the role is in `prefer_roles`. Zero-area or off-screen
    controls sink; the window's own title node (role Window) sinks hardest.
    """
    prefs = {normalize_role(r) for r in (prefer_roles or [])}
    scored: list[tuple[int, object]] = []
    for c in controls:
        role = normalize_role(getattr(c, "role", ""))
        s = 2 * _name_match_score(getattr(c, "name", ""), target)
        s += _ROLE_WEIGHTS.get(role, 0)
        if prefs and role in prefs:
            s += 6
        b = getattr(c, "bounds", None)
        if b is None or b.width <= 0 or b.height <= 0:
            s -= 5
        if getattr(c, "is_offscreen", False):
            s -= 3
        if getattr(c, "enabled", True) is False:
            s -= 1
        scored.append((s, c))
    scored.sort(key=lambda p: p[0], reverse=True)
    # Invokability probe on the head only (COM calls are slow).
    head = scored[:UIA_INVOKE_PROBE_TOP]
    probed: list[tuple[int, object]] = []
    for s, c in head:
        try:
            if semantic_mod.is_invokable(c):
                s += 2
        except Exception:
            pass
        probed.append((s, c))
    probed.sort(key=lambda p: p[0], reverse=True)
    return probed + scored[UIA_INVOKE_PROBE_TOP:]


_CONTAINER_ROLES = {"window", "pane", "group", "document", "list", "tree", "table", "custom"}


def _is_container_like(widget, token: FocusToken) -> bool:
    """A 'clickable ancestor' that is really the window or a big pane.
    Clicking its center would miss the text the user asked for."""
    role = normalize_role(getattr(widget, "role", ""))
    if role in _CONTAINER_ROLES:
        return True
    live = token.current_bounds() or token.bounds_at_creation
    b = getattr(widget, "bounds", None)
    return b is not None and live.area > 0 and b.area >= 0.5 * live.area


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------
def find(
    token: FocusToken,
    target: str,
    *,
    fuzzy: bool = True,
    threshold: float = 0.6,
    capture_for_handoff: bool = True,
    prefer_roles: list[str] | None = None,
) -> FindResult:
    """Find target in the focused window. Pixel grammar -> UIA -> OCR -> handoff.

    Every call is recorded to the telemetry events log.
    """
    result = _find_impl(
        token,
        target,
        fuzzy=fuzzy,
        threshold=threshold,
        capture_for_handoff=capture_for_handoff,
        prefer_roles=prefer_roles,
    )
    telemetry.record_find(token, target, result, prefer_roles=prefer_roles)
    return result


def _find_impl(
    token: FocusToken,
    target: str,
    *,
    fuzzy: bool,
    threshold: float,
    capture_for_handoff: bool,
    prefer_roles: list[str] | None,
) -> FindResult:
    t0 = time.perf_counter()
    result = FindResult(found=False, backend="vision_handoff")

    # Snapshot window state once so every return path includes it. Lets the
    # caller (Claude) detect off-screen clicks before issuing them.
    try:
        snap = token_inspect(token)
        if snap.get("alive", True):
            result.window_state = {
                "minimized": bool(snap.get("minimized", False)),
                "occluded": bool(snap.get("occluded", False)),
                "off_screen": bool(snap.get("off_screen", False)),
            }
    except Exception:
        result.window_state = None

    # 0. Pixel grammar (color:/image:). No text, no UIA, just pixels.
    try:
        spec = pixel_mod.parse_target(target)
    except ValueError as e:
        result.backend = "pixel"
        result.backends_tried.append("pixel")
        result.notes.append(f"bad_pixel_target:{e}")
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        return result
    if spec is not None:
        result.backends_tried.append("pixel")
        result.backend = "pixel"
        img = _capture_window(token)
        if img is None:
            result.notes.append("capture_failed")
        else:
            try:
                hits = pixel_mod.find(img, spec)
            except Exception as e:
                hits = []
                result.notes.append(f"pixel_error:{type(e).__name__}:{e}")
            if hits:
                ox, oy = _ocr_origin(token)
                out = []
                for h in hits:
                    d = h.to_dict()
                    d["bbox"]["x"] += ox
                    d["bbox"]["y"] += oy
                    out.append(d)
                result.found = True
                result.hits = out
                result.controls = [None] * len(out)
            else:
                result.notes.append(f"No pixels matched {spec.raw!r} in the window capture.")
                if capture_for_handoff:
                    try:
                        data, w, h = vision_mod.encode_jpeg(img, quality=60, optimize_tokens=True)
                        result.screenshot = data
                        result.screenshot_dims = (w, h)
                    except Exception as e:
                        result.notes.append(f"screenshot_encode_failed:{e}")
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        return result

    # 1. Try UIA
    if semantic_mod.HAS_UIA:
        result.backends_tried.append("uia")
        if semantic_mod.supports_uia(token.hwnd, token.pid):
            controls = semantic_mod.query(token.hwnd, name=target, max_results=UIA_CANDIDATE_CAP)
            if controls:
                ranked = rank_uia_candidates(controls, target, prefer_roles)
                # A negative score means "matched the substring but is the
                # window itself / an off-screen container": not a real hit.
                usable = [(s, c) for s, c in ranked if s > 0]
                if usable:
                    result.found = True
                    result.backend = "uia"
                    hits = []
                    ctrls = []
                    for s, c in usable:
                        d = c.to_dict()
                        d["match_score"] = s
                        hits.append(d)
                        ctrls.append(c)
                    result.hits = hits
                    # Keep the live UIAControl alongside each hit so click()
                    # can call Invoke() instead of moving the mouse.
                    result.controls = ctrls
                    if len(usable) > 1 and usable[0][0] == usable[1][0]:
                        result.notes.append("ambiguous_uia: top two candidates tie")
                    result.elapsed_ms = (time.perf_counter() - t0) * 1000
                    return result
                result.notes.append(
                    f"uia_matches_rejected:{len(controls)} (window title / containers only)"
                )

    # 2. Try OCR
    result.backends_tried.append("ocr")
    img = _capture_window(token)
    words: list = []
    matches: list = []
    if img is not None:
        words, matches, pass_note = _ocr_passes(token, img, target, fuzzy, threshold)
        if pass_note:
            result.notes.append(pass_note)
        # If OCR found nothing AND window is occluded, raise and retry once
        if not matches:
            try:
                if spatial_mod.is_occluded(token.hwnd):
                    spatial_mod.bring_to_front(token.hwnd)
                    time.sleep(0.2)
                    vision_mod.clear_ocr_cache(token.id)
                    img = _capture_window(token)
                    if img is not None:
                        words, matches, pass_note = _ocr_passes(
                            token, img, target, fuzzy, threshold
                        )
                        result.notes.append("occlusion_retry: window raised, OCR re-run")
            except Exception as e:
                result.notes.append(f"occlusion_retry_failed:{e}")
        if matches:
            # Translate window-local bbox to screen-absolute using LIVE bounds,
            # not bounds_at_creation. If the user moves the window between focus
            # and find, the creation snapshot is wrong by exactly the drag delta.
            origin_x, origin_y = _ocr_origin(token)
            hits: list[dict] = []
            ctrls: list[object | None] = []
            try_uia_upgrade = semantic_mod.HAS_UIA and semantic_mod.supports_uia(
                token.hwnd, token.pid
            )
            for m in matches:
                d = m.to_dict()
                d["bbox"]["x"] += origin_x
                d["bbox"]["y"] += origin_y
                # Widget-not-text upgrade: ask UIA what control sits under the
                # OCR hit's screen center. If we find an invokable ancestor
                # THAT BELONGS TO THIS WINDOW, swap the OCR text bbox for the
                # widget bbox and remember the control so click() can Invoke()
                # instead of moving the mouse. ControlFromPoint hit-tests the
                # whole desktop, so an overlapping window's widget must be
                # rejected or the click would go to the wrong app.
                widget = None
                if try_uia_upgrade:
                    bbox = d["bbox"]
                    cx = bbox["x"] + bbox["width"] // 2
                    cy = bbox["y"] + bbox["height"] // 2
                    try:
                        leaf = semantic_mod.control_from_point(cx, cy)
                        if leaf is not None:
                            widget = semantic_mod.find_clickable_ancestor(leaf)
                        if widget is not None and not semantic_mod.control_in_window(
                            widget, token.hwnd
                        ):
                            result.notes.append("uia_upgrade_rejected:foreign_window")
                            widget = None
                        if widget is not None and _is_container_like(widget, token):
                            result.notes.append("uia_upgrade_rejected:container")
                            widget = None
                    except Exception:
                        widget = None
                if widget is not None:
                    d["widget_bbox"] = widget.bounds.to_dict()
                    d["bbox"] = widget.bounds.to_dict()
                    d["widget_role"] = widget.role
                    d["widget_name"] = widget.name
                    d["upgraded_to_uia"] = True
                hits.append(d)
                ctrls.append(widget)
            result.found = True
            result.backend = "ocr"
            result.hits = hits
            result.controls = ctrls
            result.elapsed_ms = (time.perf_counter() - t0) * 1000
            return result
        # Capture nearest matches at lower threshold for the handoff. Re-rank
        # so candidates sharing concept words with the target (e.g. 'Mic/Aux'
        # vs 'GoXLR Mic' both contain 'mic') float to the top, instead of
        # ranking purely by edit-distance.
        soft = vision_mod.find_text_in_image(words, target, fuzzy=True, threshold=0.4)
        if soft:
            origin_x, origin_y = _ocr_origin(token)
            scored = []
            for m in soft:
                d = m.to_dict()
                d["bbox"]["x"] += origin_x
                d["bbox"]["y"] += origin_y
                d["backend"] = "ocr"
                d["token_overlap"] = round(_token_overlap(target, m.text), 3)
                scored.append(d)
            scored.sort(
                key=lambda c: (c["token_overlap"], c.get("similarity", 0)),
                reverse=True,
            )
            result.nearest_matches = scored[:5]

    # 3. Vision handoff: include screenshot + nearest matches so Claude can decide
    if capture_for_handoff and img is not None:
        try:
            data, w, h = vision_mod.encode_jpeg(img, quality=60, optimize_tokens=True)
            result.screenshot = data
            result.screenshot_dims = (w, h)
        except Exception as e:
            result.notes.append(f"screenshot_encode_failed:{e}")

    result.notes.append(
        f"No exact match for {target!r}. "
        f"Window may have changed structure since the target was last seen."
    )
    result.elapsed_ms = (time.perf_counter() - t0) * 1000
    return result


def _ocr_passes(
    token: FocusToken, img, target: str, fuzzy: bool, threshold: float
) -> tuple[list, list, str | None]:
    """OCR at native scale, then 2x if nothing matched and the capture is small
    enough that the upscale stays fast. Returns (words, matches, note)."""
    words = vision_mod.cached_ocr(token.id, img)
    matches = vision_mod.find_text_in_image(words, target, fuzzy=fuzzy, threshold=threshold)
    if matches:
        return words, matches, None
    width = getattr(img, "width", None)
    height = getattr(img, "height", None)
    if width is None or height is None:
        return words, [], None
    if (
        vision_mod.auto_ocr_scale(img) == 1.0
        and width * height <= vision_mod.OCR_UPSCALE_MAX_PIXELS
    ):
        words2 = vision_mod.cached_ocr(
            token.id, img, scale=vision_mod.OCR_UPSCALE_FACTOR, psm=vision_mod.OCR_PASS2_PSM
        )
        matches2 = vision_mod.find_text_in_image(words2, target, fuzzy=fuzzy, threshold=threshold)
        if matches2:
            return (
                words2,
                matches2,
                f"ocr_pass2: matched after 2x upscale (psm {vision_mod.OCR_PASS2_PSM})",
            )
        # Keep the richer word list for nearest-match suggestions.
        if len(words2) > len(words):
            words = words2
        return words, [], "ocr_pass2: no match at 1x or 2x"
    return words, [], None


def suggest_alternatives(token: FocusToken, target: str, *, top_n: int = 10) -> dict:
    """Lower-threshold fuzzy search across BOTH UIA and OCR."""
    t0 = time.perf_counter()
    candidates = []
    # UIA: dump all controls, score against target
    if semantic_mod.HAS_UIA and semantic_mod.supports_uia(token.hwnd, token.pid):
        dump = semantic_mod.walk_tree(token.hwnd, max_depth=6, max_nodes=200)
        target_l = target.lower()
        import difflib

        for node in dump:
            name = node.get("name", "")
            if not name:
                continue
            sim = difflib.SequenceMatcher(None, name.lower(), target_l).ratio()
            if sim >= 0.4:
                candidates.append(
                    {
                        "text": name,
                        "role": node.get("role"),
                        "bbox": node.get("bounds"),
                        "similarity": round(sim, 3),
                        "backend": "uia",
                    }
                )
    # OCR
    img = _capture_window(token)
    if img is not None:
        words = vision_mod.cached_ocr(token.id, img)
        soft = vision_mod.find_text_in_image(words, target, fuzzy=True, threshold=0.4)
        ox, oy = _ocr_origin(token)
        for m in soft:
            d = m.to_dict()
            d["bbox"]["x"] += ox
            d["bbox"]["y"] += oy
            d["backend"] = "ocr"
            candidates.append(d)
    # Rank: similarity*0.55 + prominence*0.20 + token_overlap*0.25
    # Token overlap rewards candidates that share concept words with the
    # target ("Mic/Aux" vs "GoXLR Mic" share 'mic'), which pure edit-distance
    # otherwise ranks poorly because the surrounding characters differ.
    live = token.current_bounds()
    win_area = max((live or token.bounds_at_creation).area, 1)
    for c in candidates:
        b = c.get("bbox") or {}
        area = max(int(b.get("width", 0)) * int(b.get("height", 0)), 0)
        prominence = min(area / win_area, 1.0)
        overlap = _token_overlap(target, c.get("text", ""))
        c["token_overlap"] = round(overlap, 3)
        c["score"] = round(
            c["similarity"] * 0.55 + prominence * 0.20 + overlap * 0.25,
            3,
        )
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return {
        "target": target,
        "candidates": candidates[:top_n],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
