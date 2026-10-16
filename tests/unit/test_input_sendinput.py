"""Unit tests for iris.input (SendInput-based mouse primitives).

These don't actually move the cursor in the user's session, they exercise the
internal coordinate normalization and the input-struct construction.
"""

from __future__ import annotations

import pytest

from iris import input as input_mod


@pytest.mark.skipif(not input_mod.HAS_SENDINPUT, reason="Win32 SendInput unavailable")
class TestVirtualDesktopMath:
    def test_virtual_desktop_returns_four_ints(self):
        l, t, w, h = input_mod._virtual_desktop()
        assert isinstance(l, int)
        assert isinstance(t, int)
        assert w > 0
        assert h > 0

    def test_to_absolute_maps_origin_to_origin_relative(self):
        # The virtual screen origin (vl, vt) must map to (0, 0) in normalized space.
        vl, vt, vw, vh = input_mod._virtual_desktop()
        nx, ny = input_mod._to_absolute(vl, vt)
        assert nx == 0
        assert ny == 0

    def test_to_absolute_maps_far_corner_near_max(self):
        vl, vt, vw, vh = input_mod._virtual_desktop()
        far_x = vl + vw - 1
        far_y = vt + vh - 1
        nx, ny = input_mod._to_absolute(far_x, far_y)
        # Should be 65535 within rounding for nontrivial screens (>= 2 pixels).
        # Smaller screens are allowed to land at 65535 exactly.
        assert nx == 65535
        assert ny == 65535


@pytest.mark.skipif(not input_mod.HAS_SENDINPUT, reason="Win32 SendInput unavailable")
class TestButtonFlags:
    @pytest.mark.parametrize("button", ["left", "right", "middle"])
    def test_each_button_has_down_and_up_pair(self, button: str):
        down, up = input_mod._BUTTON_FLAGS[button]
        assert down != 0
        assert up != 0
        assert down != up

    def test_unknown_button_rejected(self):
        with pytest.raises(ValueError):
            input_mod.click(x=0, y=0, button="purple")


@pytest.mark.skipif(not input_mod.HAS_SENDINPUT, reason="Win32 SendInput unavailable")
def test_position_returns_two_ints():
    x, y = input_mod.position()
    assert isinstance(x, int)
    assert isinstance(y, int)
