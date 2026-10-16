"""click() delivery policy: UIA pattern > quiet window message > real mouse,
plus the mark= call mode. Everything below the policy is monkeypatched so
this runs without a desktop."""

from __future__ import annotations

import pytest

from iris import __main__ as srv
from iris import marks as marks_mod
from iris import resolver as resolver_mod
from iris.geometry import Rect
from iris.tokens import FocusToken

WIN = Rect(0, 0, 800, 600)


@pytest.fixture
def env(monkeypatch):
    calls = {"message": [], "mouse": [], "invoke": []}
    tok = FocusToken.create(hwnd=7, pid=1, exe_name="x.exe", title="T", monitor_index=0, bounds=WIN)
    monkeypatch.setattr(FocusToken, "current_bounds", lambda self: WIN)
    monkeypatch.setattr(srv, "_resolve_token", lambda tid: tok)
    monkeypatch.setattr(srv, "token_revalidate", lambda t: True)
    monkeypatch.setattr(srv, "_point_on_any_monitor", lambda x, y: True)
    monkeypatch.setattr(srv.spatial_mod, "is_occluded", lambda hwnd: False)
    monkeypatch.setattr(srv.telemetry, "record", lambda *a, **k: None)

    def fake_message(hwnd, x, y, button="left", clicks=1):
        calls["message"].append((hwnd, x, y, button, clicks))
        return {"ok": True, "method": "message", "target_hwnd": hwnd, "client": [x, y]}

    def fake_mouse(x=None, y=None, button="left", clicks=1, restore_cursor=False, **kw):
        calls["mouse"].append((x, y, button, clicks, restore_cursor))
        return {
            "ok": True,
            "x": x,
            "y": y,
            "cursor_restored_to": [1, 1] if restore_cursor else None,
        }

    monkeypatch.setattr(srv.input_mod, "message_click", fake_message)

    def fake_checked(hwnd, x, y, button="left", clicks=1, force=False):
        if calls.get("deny"):
            calls["message"].append(("denied", x, y))
            return {"ok": False, "reason": "class_denied", "class": "TkChild", "verdict": "denied"}
        r = fake_message(hwnd, x, y, button, clicks)
        r.update({"class": "Button", "verdict": "trusted", "reaction": None})
        return r

    monkeypatch.setattr(srv.quiet_mod, "message_click_checked", fake_checked)
    monkeypatch.setattr(srv.input_mod, "click", fake_mouse)
    monkeypatch.setattr(srv.semantic_mod, "is_invoke_trusted", lambda c: c == "INVOKABLE")

    def fake_pattern(control):
        calls["invoke"].append(control)
        return {"ok": True, "pattern": "invoke"}

    monkeypatch.setattr(srv.semantic_mod, "try_pattern_click", fake_pattern)

    def fake_find(t, target, **kw):
        ctrl = "INVOKABLE" if target == "Invokable" else None
        return resolver_mod.FindResult(
            found=True,
            backend="ocr",
            hits=[{"text": target, "bbox": {"x": 100, "y": 100, "width": 40, "height": 20}}],
            controls=[ctrl],
        )

    monkeypatch.setattr(srv.resolver_mod, "find", fake_find)
    return calls, tok


def test_default_auto_uses_quiet_message_not_mouse(env):
    calls, tok = env
    out = srv.click(token=tok.id, target="Save")
    assert out["ok"] and out["click_method"] == "message"
    assert calls["message"] == [(7, 120, 110, "left", 1)]
    assert calls["mouse"] == []


def test_auto_falls_back_to_mouse_when_class_denied(env):
    calls, tok = env
    calls["deny"] = True
    out = srv.click(token=tok.id, target="Save")
    assert out["ok"] and out["click_method"] == "mouse_after_quiet_class_denied"
    assert out["quiet_attempt"]["class"] == "TkChild"
    assert calls["mouse"] == [(120, 110, "left", 1, True)]


def test_quiet_mode_reports_denied_class_instead_of_mouse(env):
    calls, tok = env
    calls["deny"] = True
    out = srv.click(token=tok.id, target="Save", click_method="quiet")
    assert out["ok"] is False and out["error"] == "quiet_click_failed"
    assert calls["mouse"] == []


def test_auto_prefers_uia_invoke_when_trusted(env):
    calls, tok = env
    out = srv.click(token=tok.id, target="Invokable")
    assert out["click_method"] == "uia_pattern" and calls["invoke"] == ["INVOKABLE"]
    assert calls["message"] == [] and calls["mouse"] == []


def test_mouse_method_uses_sendinput_and_restores_cursor(env):
    calls, tok = env
    out = srv.click(token=tok.id, target="Save", click_method="mouse")
    assert out["click_method"] == "mouse" and out["cursor_restored_to"] == [1, 1]
    assert calls["mouse"] == [(120, 110, "left", 1, True)] and calls["message"] == []


def test_raw_xy_without_token_falls_back_to_mouse(env):
    calls, _ = env
    out = srv.click(x=50, y=60)
    assert out["click_method"] == "mouse" and calls["mouse"][0][:2] == (50, 60)


def test_quiet_without_token_is_refused(env):
    calls, _ = env
    out = srv.click(x=50, y=60, click_method="quiet")
    assert out["ok"] is False and out["error"] == "quiet_needs_token"
    assert calls["mouse"] == [] and calls["message"] == []


def test_invoke_only_errors_without_pattern(env):
    calls, tok = env
    out = srv.click(token=tok.id, target="Save", click_method="invoke")
    assert out["ok"] is False and out["error"] == "no_invoke_pattern"
    assert calls["message"] == [] and calls["mouse"] == []


def test_click_by_mark_resolves_center_and_flags_stale(env, monkeypatch):
    calls, tok = env
    ms = marks_mod.MarkSet(
        token_id=tok.id,
        hwnd=7,
        marks=[marks_mod.Mark(3, Rect(200, 300, 50, 20), "OK", "button", "ocr")],
        window_bounds=WIN,
        capture_digest="abc",
        created_at=0.0,
    )
    monkeypatch.setattr(srv.marks_mod, "get_marks", lambda tid: ms)
    monkeypatch.setattr(srv.marks_mod, "is_stale", lambda m, t: True)
    out = srv.click(token=tok.id, mark=3)
    assert out["ok"] and out["click_method"] == "message"
    assert calls["message"][0][1:3] == (225, 310)
    assert out["resolved"]["mark"] == 3 and out["marks_stale"] is True
    bad = srv.click(token=tok.id, mark=9)
    assert bad["ok"] is False and bad["error"] == "unknown_mark" and bad["marks_available"] == 1


def test_click_by_mark_without_marks_is_explained(env, monkeypatch):
    _, tok = env
    monkeypatch.setattr(srv.marks_mod, "get_marks", lambda tid: None)
    out = srv.click(token=tok.id, mark=1)
    assert (
        out["ok"] is False
        and out["error"] == "no_marks"
        and "see(token, marks=True)" in out["hint"]
    )


def test_click_by_mark_uses_invoke_when_control_trusted(env, monkeypatch):
    calls, tok = env
    ms = marks_mod.MarkSet(
        token_id=tok.id,
        hwnd=7,
        marks=[marks_mod.Mark(1, Rect(10, 10, 50, 20), "Go", "button", "uia", control="INVOKABLE")],
        window_bounds=WIN,
        capture_digest=None,
        created_at=0.0,
    )
    monkeypatch.setattr(srv.marks_mod, "get_marks", lambda tid: ms)
    monkeypatch.setattr(srv.marks_mod, "is_stale", lambda m, t: False)
    out = srv.click(token=tok.id, mark=1)
    assert out["click_method"] == "uia_pattern" and calls["invoke"] == ["INVOKABLE"]


def test_mark_outside_window_is_clamped(env, monkeypatch):
    calls, tok = env
    ms = marks_mod.MarkSet(
        token_id=tok.id,
        hwnd=7,
        marks=[marks_mod.Mark(1, Rect(2000, 2000, 50, 20), "Far", "text", "ocr")],
        window_bounds=WIN,
        capture_digest=None,
        created_at=0.0,
    )
    monkeypatch.setattr(srv.marks_mod, "get_marks", lambda tid: ms)
    monkeypatch.setattr(srv.marks_mod, "is_stale", lambda m, t: False)
    out = srv.click(token=tok.id, mark=1)
    assert out["ok"] is False and out["error"] == "click_outside_window"
    assert calls["message"] == [] and calls["mouse"] == []
