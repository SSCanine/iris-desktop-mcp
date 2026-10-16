"""Tests for FindResult.window_state propagation through resolver.find()."""

from iris import resolver as resolver_mod
from iris.geometry import Rect
from iris.resolver import FindResult, find
from iris.tokens import FocusToken


def _token():
    return FocusToken.create(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        monitor_index=0,
        bounds=Rect(0, 0, 100, 100),
    )


def test_findresult_window_state_round_trips_through_to_dict():
    r = FindResult(found=True, backend="uia")
    r.window_state = {"minimized": True, "occluded": False, "off_screen": True}
    d = r.to_dict()
    assert d["window_state"] == {
        "minimized": True,
        "occluded": False,
        "off_screen": True,
    }


def test_findresult_omits_window_state_when_none():
    """A None window_state is left out of the serialized dict so we don't
    pollute the response with unhelpful nulls."""
    r = FindResult(found=True, backend="uia")
    d = r.to_dict()
    assert "window_state" not in d


def test_find_populates_window_state_from_inspect(monkeypatch):
    """find() snapshots window state once at the top so every return path
    surfaces minimized/occluded/off_screen to the caller."""
    fake_state = {
        "alive": True,
        "minimized": True,
        "occluded": False,
        "off_screen": True,
    }
    monkeypatch.setattr(resolver_mod, "token_inspect", lambda tk: fake_state)
    # Force vision_handoff path: HAS_UIA off, no image
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: None)

    result = find(_token(), "anything")
    assert result.window_state == {
        "minimized": True,
        "occluded": False,
        "off_screen": True,
    }


def test_find_window_state_falsy_when_window_normal(monkeypatch):
    fake_state = {
        "alive": True,
        "minimized": False,
        "occluded": False,
        "off_screen": False,
    }
    monkeypatch.setattr(resolver_mod, "token_inspect", lambda tk: fake_state)
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: None)

    result = find(_token(), "anything")
    assert result.window_state == {
        "minimized": False,
        "occluded": False,
        "off_screen": False,
    }


def test_find_handles_dead_token_inspect_gracefully(monkeypatch):
    """If inspect reports the token isn't alive, window_state stays None and
    find still completes without crashing."""
    monkeypatch.setattr(
        resolver_mod,
        "token_inspect",
        lambda tk: {"alive": False, "reason": "hwnd_dead_no_repair"},
    )
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: None)

    result = find(_token(), "anything")
    assert result.window_state is None
    assert "window_state" not in result.to_dict()


def test_find_recovers_when_inspect_raises(monkeypatch):
    """Defensive: if inspect throws, find still produces a result."""

    def boom(tk):
        raise RuntimeError("inspect blew up")

    monkeypatch.setattr(resolver_mod, "token_inspect", boom)
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: None)

    result = find(_token(), "anything")
    assert result.window_state is None
