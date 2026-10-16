"""Drag engine and drag/knob/hold tools. SendInput is captured, not sent."""

from __future__ import annotations

import pytest

from iris import __main__ as srv
from iris import input as input_mod
from iris.geometry import Rect
from iris.tokens import FocusToken


@pytest.fixture
def sent(monkeypatch):
    log: list = []
    monkeypatch.setattr(input_mod, "HAS_SENDINPUT", True)
    monkeypatch.setattr(input_mod, "_send", lambda *evs: log.extend(evs) or len(evs))
    monkeypatch.setattr(input_mod, "move", lambda x, y: log.append(("move", x, y)))
    monkeypatch.setattr(input_mod, "position", lambda: (5, 5))
    monkeypatch.setattr(input_mod.time, "sleep", lambda s: None)
    return log


def _kinds(log):
    out = []
    for e in log:
        if isinstance(e, tuple):
            out.append(e)
        elif e.type == input_mod.INPUT_KEYBOARD:
            out.append(("key", e.ki.wVk, bool(e.ki.dwFlags & input_mod.KEYEVENTF_KEYUP)))
        else:
            out.append(("mouse", e.mi.dwFlags))
    return out


def test_interpolate_small_steps_no_duplicates():
    p = input_mod.interpolate_path([(0, 0), (0, -20)], max_step_px=6)
    assert p[0] == (0, 0) and p[-1] == (0, -20)
    assert all(abs(b[1] - a[1]) <= 6 for a, b in zip(p, p[1:], strict=False))
    assert len(p) == len(set(p))
    assert input_mod.interpolate_path([(3, 3), (3, 3)]) == [(3, 3)]


def test_drag_path_order_modifiers_and_restore(sent):
    res = input_mod.drag_path([(100, 100), (100, 80)], modifiers=["shift"], max_step_px=10)
    k = _kinds(sent)
    assert k[0] == ("key", 0x10, False)  # shift down first
    assert k[1] == ("move", 100, 100)
    assert k[2] == ("mouse", input_mod.MOUSEEVENTF_LEFTDOWN)
    assert ("move", 100, 90) in k and ("move", 100, 80) in k
    up = k.index(("mouse", input_mod.MOUSEEVENTF_LEFTUP))
    assert k[up + 1] == ("key", 0x10, True)  # shift released after the button
    assert k[-1] == ("move", 5, 5)  # cursor restored
    assert res["ok"] and res["to"] == [100, 80] and res["cursor_restored_to"] == [5, 5]


def test_drag_path_always_releases_on_error(sent, monkeypatch):
    calls = {"n": 0}
    real_log = sent

    def boom(x, y):
        calls["n"] += 1
        real_log.append(("move", x, y))
        if calls["n"] == 3:
            raise RuntimeError("pointer device vanished")

    monkeypatch.setattr(input_mod, "move", boom)
    with pytest.raises(RuntimeError):
        input_mod.drag_path([(0, 0), (0, 30)], modifiers=["ctrl"], max_step_px=5)
    k = _kinds(sent)
    assert ("mouse", input_mod.MOUSEEVENTF_LEFTUP) in k
    assert ("key", 0x11, True) in k


def test_unknown_modifier_rejected_before_any_input(sent):
    with pytest.raises(ValueError):
        input_mod.drag_path([(0, 0), (1, 1)], modifiers=["hyper"])
    assert sent == []


def test_scroll_at_one_event_per_notch(sent):
    input_mod.scroll_at(10, 20, -3)
    wheels = [e for e in sent if not isinstance(e, tuple)]
    assert len(wheels) == 3
    assert sent[0] == ("move", 10, 20) and sent[-1] == ("move", 5, 5)


# --- tool level -------------------------------------------------------------
WIN = Rect(0, 0, 800, 600)


@pytest.fixture
def tool_env(monkeypatch):
    calls: dict = {"drag_path": [], "scroll_at": []}
    tok = FocusToken.create(hwnd=9, pid=1, exe_name="x.exe", title="T", monitor_index=0, bounds=WIN)
    monkeypatch.setattr(FocusToken, "current_bounds", lambda self: WIN)
    monkeypatch.setattr(srv, "_resolve_token", lambda t: tok)
    monkeypatch.setattr(srv, "token_revalidate", lambda t: True)
    monkeypatch.setattr(srv, "_point_on_any_monitor", lambda x, y: -100 < x < 5000)
    monkeypatch.setattr(srv.spatial_mod, "is_occluded", lambda h: False)
    monkeypatch.setattr(srv.telemetry, "record", lambda *a, **k: None)
    digests = iter(["a", "b", "c", "d", "e", "f"])
    monkeypatch.setattr(srv.quiet_mod, "_roi_digest", lambda h, x, y: next(digests))
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)

    def fake_drag_path(points, **kw):
        calls["drag_path"].append((points, kw))
        return {"ok": True, "ms": 5, "modifiers": kw.get("modifiers") or []}

    monkeypatch.setattr(srv.input_mod, "drag_path", fake_drag_path)
    monkeypatch.setattr(
        srv.input_mod,
        "scroll_at",
        lambda x, y, n: calls["scroll_at"].append((x, y, n)) or {"ok": True},
    )

    def fake_resolve(tk, target=None, mark=None, x=None, y=None, prefer_roles=None):
        if x is not None:
            return (x, y), {"source": "xy"}
        if target == "Knob":
            return (200, 300), {"source": "pixel"}
        if target == "Deck B":
            return (5000 - 1, 10), {"source": "uia"}
        return None, {"error": "target_not_found"}

    monkeypatch.setattr(srv, "_resolve_point", fake_resolve)
    return calls, tok


def test_drag_relative_and_changed(tool_env):
    calls, tok = tool_env
    out = srv.drag(token=tok.id, from_x=100, from_y=100, dy=-40, modifiers=["shift"])
    assert out["ok"] and out["to"] == [100, 60] and out["changed"] is True
    pts, kw = calls["drag_path"][0]
    assert pts == [(100, 100), (100, 60)] and kw["modifiers"] == ["shift"] and kw["restore_cursor"]


def test_drag_refuses_start_outside_window_and_missing_endpoint(tool_env):
    _, tok = tool_env
    assert srv.drag(token=tok.id, from_x=900, from_y=100, dx=10)["error"] == "start_outside_window"
    bad = srv.drag(token=tok.id, from_target="Nope", to_x=1, to_y=1)
    assert bad["ok"] is False and bad["stage"] == "from"


def test_drag_end_may_be_outside_window_but_not_off_screen(tool_env):
    calls, tok = tool_env
    out = srv.drag(token=tok.id, from_target="Knob", to_target="Deck B")
    assert out["ok"] and out["to"] == [4999, 10]


def test_knob_vertical_fine_and_wheel(tool_env):
    calls, tok = tool_env
    out = srv.knob(token=tok.id, target="Knob", amount=30, fine=True)
    pts, kw = calls["drag_path"][0]
    assert pts == [(200, 300), (200, 270)] and kw["modifiers"] == ["shift"]
    assert out["ok"] and out["moved_px"] == 30
    srv.knob(token=tok.id, target="Knob", amount=-15, mode="horizontal")
    assert calls["drag_path"][1][0] == [(200, 300), (185, 300)]
    srv.knob(token=tok.id, target="Knob", amount=-2, mode="wheel")
    assert calls["scroll_at"] == [(200, 300, -2)]


def test_hold_is_a_zero_length_gesture(tool_env):
    calls, tok = tool_env
    out = srv.hold(token=tok.id, target="Knob", ms=700)
    pts, kw = calls["drag_path"][0]
    assert pts == [(200, 300), (200, 300)] and kw["hold_ms"] == 700 and out["ok"]
