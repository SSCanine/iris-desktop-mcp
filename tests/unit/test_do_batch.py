"""do(): one call, many steps. Everything below the dispatcher is stubbed."""

from __future__ import annotations

import pytest

from iris import __main__ as srv


@pytest.fixture
def env(monkeypatch):
    calls: list = []
    monkeypatch.setattr(srv.telemetry, "record", lambda *a, **k: None)

    def fake_focus(match, raise_window=False):
        calls.append(("focus", match))
        return {"ok": True, "token": "tok1", "title": "T"}

    def fake_click(token=None, **kw):
        calls.append(("click", token, kw))
        if kw.get("target") == "Missing":
            return {"ok": False, "error": "target_not_found", "find_result": {"big": "x" * 999}}
        return {"ok": True, "click_method": "message", "x": 1, "y": 2}

    def fake_find(token, target, **kw):
        calls.append(("find", token, target))
        return {
            "found": True,
            "backend": "uia",
            "hits": [{"name": "OK", "role": "ButtonControl", "bounds": {"x": 1}}, {"name": "OK2"}],
            "window_state": {"minimized": False},
        }

    monkeypatch.setattr(srv, "focus", fake_focus)
    monkeypatch.setattr(srv, "click", fake_click)
    monkeypatch.setattr(srv, "find", fake_find)
    monkeypatch.setattr(
        srv, "type_text", lambda text, interval=0.0: calls.append(("type", text)) or {"ok": True}
    )
    monkeypatch.setattr(srv, "hotkey", lambda keys: calls.append(("hotkey", keys)) or {"ok": True})
    monkeypatch.setattr(
        srv, "_ensure_keyboard_focus", lambda tok: calls.append(("kbfocus", tok)) or None
    )
    monkeypatch.setattr(srv.time, "sleep", lambda s: calls.append(("sleep", s)))
    return calls


def test_focus_token_flows_to_later_steps(env):
    out = srv.do(
        [
            {"action": "focus", "match": {"process": "notepad.exe"}},
            {"action": "click", "target": "File"},
            {"action": "find", "target": "OK"},
        ]
    )
    assert out["ok"] and out["token"] == "tok1" and out["failed_at"] is None
    assert env[1] == ("click", "tok1", {"target": "File"})
    assert env[2] == ("find", "tok1", "OK")
    find_step = out["steps"][2]
    assert find_step["hits"] == 2 and find_step["top"]["text"] == "OK"
    assert "window_state" not in find_step


def test_stops_at_first_failure_and_compacts(env):
    out = srv.do(
        [{"action": "click", "target": "Missing"}, {"action": "type", "text": "never"}],
        token="t0",
    )
    assert out["ok"] is False and out["failed_at"] == 0 and len(out["steps"]) == 1
    assert out["steps"][0]["error"] == "target_not_found"
    assert "find_result" not in out["steps"][0]
    assert not any(c[0] == "type" for c in env)


def test_continue_on_error(env):
    out = srv.do(
        [{"action": "click", "target": "Missing"}, {"action": "hotkey", "keys": ["ctrl", "s"]}],
        token="t0",
        stop_on_error=False,
    )
    assert out["failed_at"] == 0 and len(out["steps"]) == 2 and out["steps"][1]["ok"]


def test_keyboard_steps_check_focus_first(env):
    srv.do([{"action": "type", "text": "hi"}], token="t0")
    assert env[0] == ("kbfocus", "t0") and env[1] == ("type", "hi")


def test_token_required_and_unknown_and_missing_args(env):
    assert srv.do([{"action": "find", "target": "x"}])["steps"][0]["error"] == "needs_token"
    assert srv.do([{"action": "click", "x": 5, "y": 6}])["ok"]  # raw coords need no token
    assert srv.do([{"action": "fly"}], token="t")["steps"][0]["error"].startswith("unknown_action")
    assert srv.do([{"action": "type"}], token="t")["steps"][0]["error"] == "missing_arg:text"


def test_wait_is_clamped(env):
    srv.do([{"action": "wait", "ms": 999999}])
    assert ("sleep", 30.0) in env
