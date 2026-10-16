"""Unit tests for the OCR -> UIA widget upgrade helpers in semantic.py."""

from __future__ import annotations

import pytest

from iris import semantic as semantic_mod


@pytest.mark.skipif(not semantic_mod.HAS_UIA, reason="UIA unavailable")
def test_control_from_point_does_not_raise_for_off_screen_coords():
    # UIA clamps off-screen points to the desktop root rather than returning
    # None, so we cannot assert None. The contract is: this must not raise,
    # and if it returns something it must be a UIAControl.
    result = semantic_mod.control_from_point(-99999, -99999)
    assert result is None or hasattr(result, "bounds")


@pytest.mark.skipif(not semantic_mod.HAS_UIA, reason="UIA unavailable")
def test_is_invokable_handles_none():
    assert semantic_mod.is_invokable(None) is False


@pytest.mark.skipif(not semantic_mod.HAS_UIA, reason="UIA unavailable")
def test_find_clickable_ancestor_handles_none():
    assert semantic_mod.find_clickable_ancestor(None) is None


@pytest.mark.skipif(not semantic_mod.HAS_UIA, reason="UIA unavailable")
def test_try_pattern_click_handles_none():
    r = semantic_mod.try_pattern_click(None)
    assert r["ok"] is False
    assert "no_control" in r["reason"]
