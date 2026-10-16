"""FocusToken: stateful handle for a window Iris is attending to."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from iris.geometry import Rect


@dataclass
class FocusToken:
    id: str
    hwnd: int
    pid: int
    exe_name: str
    title_at_creation: str
    monitor_index: int
    bounds_at_creation: Rect
    fingerprint: str | None = None
    parent_hwnd: int | None = None
    created_at: float = field(default_factory=time.time)
    last_revalidated_at: float = 0.0
    last_revalidation_result: bool = True

    @classmethod
    def create(
        cls,
        hwnd: int,
        pid: int,
        exe_name: str,
        title: str,
        monitor_index: int,
        bounds: Rect,
        fingerprint: str | None = None,
    ) -> FocusToken:
        return cls(
            id=str(uuid.uuid4())[:8],
            hwnd=hwnd,
            pid=pid,
            exe_name=exe_name,
            title_at_creation=title,
            monitor_index=monitor_index,
            bounds_at_creation=bounds,
            fingerprint=fingerprint,
        )

    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def current_bounds(self) -> Rect | None:
        """Live screen-absolute bounds. None if the window is dead/minimized.

        Use this anywhere coords matter (OCR translation, click clamp). The
        token's bounds_at_creation is a snapshot and goes stale the moment the
        user drags the window. Pixel-accurate clicks require asking Windows
        right now.
        """
        from iris import spatial as _spatial

        return _spatial.current_bounds(self.hwnd)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "exe_name": self.exe_name,
            "title": self.title_at_creation,
            "monitor": self.monitor_index,
            "bounds": self.bounds_at_creation.to_dict(),
            "parent_hwnd": self.parent_hwnd,
            "fingerprint": self.fingerprint,
            "age_seconds": round(self.age_seconds(), 3),
        }


class TokenRegistry:
    """Process-local in-memory store. One MCP process per session."""

    def __init__(self) -> None:
        self._tokens: dict[str, FocusToken] = {}

    def store(self, token: FocusToken) -> None:
        self._tokens[token.id] = token

    def get(self, token_id: str) -> FocusToken | None:
        return self._tokens.get(token_id)

    def remove(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)

    def all(self) -> list[FocusToken]:
        return list(self._tokens.values())

    def clear(self) -> None:
        self._tokens.clear()


_DEFAULT_REGISTRY = TokenRegistry()


def default_registry() -> TokenRegistry:
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# Revalidation (cached for 250ms per token)
# ---------------------------------------------------------------------------
_REVALIDATE_TTL = 0.25  # seconds


def revalidate(token: FocusToken) -> bool:
    """Check the token's hwnd is still valid. Try to repair via pid+exe+title if dead.

    Returns True if the token can still be used (possibly with a swapped hwnd).
    Caches result for 250ms to avoid hammering Win32 from tight loops.
    """
    now = time.time()
    if (now - token.last_revalidated_at) < _REVALIDATE_TTL:
        return token.last_revalidation_result

    # Lazy imports to avoid circular dependency at module load
    try:
        import win32gui
    except ImportError:
        token.last_revalidated_at = now
        token.last_revalidation_result = True
        return True
    from iris.spatial import enumerate_windows

    if win32gui.IsWindow(token.hwnd):
        token.last_revalidated_at = now
        token.last_revalidation_result = True
        return True

    # hwnd died, try repair
    candidates = enumerate_windows()
    # First try same pid + exe (the original process is alive but window swapped)
    for w in candidates:
        if w.pid == token.pid and w.exe_name.lower() == token.exe_name.lower():
            token.hwnd = w.hwnd
            token.bounds_at_creation = w.bounds
            token.last_revalidated_at = now
            token.last_revalidation_result = True
            return True
    # Process died too. Look for a new instance of the same exe with EXACT title match.
    # (Substring matching is too greedy and false-positives on other apps with similar titles.)
    title_l = token.title_at_creation.lower()
    for w in candidates:
        if w.exe_name.lower() != token.exe_name.lower():
            continue
        if w.title.lower() == title_l:
            token.hwnd = w.hwnd
            token.pid = w.pid  # new process
            token.bounds_at_creation = w.bounds
            token.last_revalidated_at = now
            token.last_revalidation_result = True
            return True

    token.last_revalidated_at = now
    token.last_revalidation_result = False
    return False


def inspect(token: FocusToken) -> dict:
    """Full state report for a token: alive, current bounds, occlusion, popups."""
    valid = revalidate(token)
    if not valid:
        return {"id": token.id, "alive": False, "reason": "hwnd_dead_no_repair"}
    try:
        import win32gui

        from iris.spatial import (
            _make_window_info,
            find_popups_for,
            get_monitor_for_window,
            is_occluded,
            list_monitors,
        )
    except ImportError:
        return {"id": token.id, "alive": valid}
    info = _make_window_info(token.hwnd)
    if info is None:
        return {"id": token.id, "alive": False}
    monitor = get_monitor_for_window(info.bounds)
    bounds_changed = info.bounds != token.bounds_at_creation
    popups = find_popups_for(info.pid, exclude_hwnd=token.hwnd)
    occluded = False
    if not info.minimized:
        try:
            occluded = is_occluded(token.hwnd)
        except Exception:
            pass
    # off_screen: bounds don't intersect any physical monitor rect.
    # A minimized window (-32000,-32000) is also off_screen; flags are
    # independent so callers see the full truth.
    off_screen = True
    try:
        for m in list_monitors():
            if m.intersects(info.bounds):
                off_screen = False
                break
    except Exception:
        off_screen = info.bounds.x <= -30000
    return {
        "id": token.id,
        "alive": True,
        "hwnd": token.hwnd,
        "pid": token.pid,
        "title": info.title,
        "bounds": info.bounds.to_dict(),
        "monitor": monitor,
        "minimized": info.minimized,
        "occluded": occluded,
        "off_screen": off_screen,
        "bounds_changed": bounds_changed,
        "popups": [p.to_dict() for p in popups],
    }
