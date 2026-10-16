"""Unit tests for verify.wait_for_drift.

Mocks the semantic.walk_tree call so tests are deterministic and don't
require a real Windows desktop.
"""

from __future__ import annotations

from unittest.mock import patch

from iris import verify as verify_mod
from iris.geometry import Rect
from iris.tokens import FocusToken


def _tok():
    return FocusToken.create(
        hwnd=12345,
        pid=999,
        exe_name="test.exe",
        title="test",
        monitor_index=0,
        bounds=Rect(0, 0, 100, 100),
        fingerprint=None,
    )


def test_drift_detected_when_tree_changes():
    """First poll: stable dump. Second poll: new button appears. Should drift."""
    initial_dump = [
        {"depth": 0, "role": "WindowControl", "name": "Test"},
        {"depth": 1, "role": "ButtonControl", "name": "Start"},
    ]
    changed_dump = [
        {"depth": 0, "role": "WindowControl", "name": "Test"},
        {"depth": 1, "role": "ButtonControl", "name": "Stop"},
    ]
    sequence = [initial_dump, initial_dump, changed_dump]
    call_index = {"i": 0}

    def fake_walk_tree(hwnd, max_depth=4, max_nodes=80):
        idx = min(call_index["i"], len(sequence) - 1)
        call_index["i"] += 1
        return sequence[idx]

    with patch.object(verify_mod, "_backoff_iter", return_value=iter([10] * 100)):
        with (
            patch("iris.semantic.walk_tree", side_effect=fake_walk_tree),
            patch("iris.semantic.HAS_UIA", True),
        ):
            result = verify_mod.wait_for_drift(_tok(), timeout_ms=2000)

    assert result["drifted"] is True
    assert result["polls"] >= 1
    assert result["initial_fingerprint"] != result["final_fingerprint"]


def test_no_drift_when_tree_stable():
    """Same dump every poll. Should time out with drifted=False."""
    stable = [{"depth": 0, "role": "WindowControl", "name": "Test"}]

    def fake_walk_tree(hwnd, max_depth=4, max_nodes=80):
        return stable

    with patch.object(verify_mod, "_backoff_iter", return_value=iter([10] * 100)):
        with (
            patch("iris.semantic.walk_tree", side_effect=fake_walk_tree),
            patch("iris.semantic.HAS_UIA", True),
        ):
            result = verify_mod.wait_for_drift(_tok(), timeout_ms=200)

    assert result["drifted"] is False
    assert result["initial_fingerprint"] == result["final_fingerprint"]


def test_uia_unavailable_returns_error():
    with patch("iris.semantic.HAS_UIA", False):
        result = verify_mod.wait_for_drift(_tok(), timeout_ms=200)
    assert result["drifted"] is False
    assert result.get("error") == "uia_unavailable"


def test_initial_dump_failure_returned():
    with (
        patch("iris.semantic.HAS_UIA", True),
        patch("iris.semantic.walk_tree", side_effect=RuntimeError("hwnd dead")),
    ):
        result = verify_mod.wait_for_drift(_tok(), timeout_ms=200)
    assert result["drifted"] is False
    assert "initial_dump_failed" in result.get("error", "")
