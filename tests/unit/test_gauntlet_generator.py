"""The Gauntlet's layout generator must never produce an unfair level."""

from __future__ import annotations

import random

import pytest

from iris.bench import gauntlet as g


def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def test_shade_never_collides_with_base_and_stays_in_range():
    for base in g.DOT_COLORS:
        for delta in (20, 40, 80):
            for d in ((delta, 0, 0), (0, -delta, 0), (0, 0, delta)):
                sh = g.shade(base, *d)
                assert sh != base, (base, d)
                r, gg, b = _hex(sh)
                assert all(0 <= v <= 255 for v in (r, gg, b))
                # Exactly one channel moved, by exactly delta.
                diffs = [abs(a - c) for a, c in zip(_hex(base), (r, gg, b), strict=True)]
                assert sorted(diffs) == [0, 0, delta]


def test_shade_flips_direction_at_the_clamp():
    assert g.shade("#ff0000", 40, 0, 0) == "#d70000"
    assert g.shade("#000000", 0, -40, 0) == "#002800"


def test_place_rects_are_disjoint_with_margin():
    rng = random.Random(3)
    sizes = [(24, 24), (16, 16), (12, 12), (8, 8), (6, 6), (4, 4), (3, 3), (2, 2)] * 2
    pos = g.place_rects(rng, sizes, (700, 494), margin=20)
    boxes = [(x, y, x + w, y + h) for (x, y), (w, h) in zip(pos, sizes, strict=True)]
    for i, a in enumerate(boxes):
        assert a[0] >= 20 and a[1] >= 20 and a[2] <= 680 and a[3] <= 474
        for b in boxes[i + 1 :]:
            gap_x = max(b[0] - a[2], a[0] - b[2])
            gap_y = max(b[1] - a[3], a[1] - b[3])
            assert max(gap_x, gap_y) >= 20, (a, b)


def test_dot_colors_are_mutually_distinct_beyond_default_tolerance():
    from iris.pixel import DEFAULT_COLOR_TOLERANCE

    cols = [_hex(c) for c in g.DOT_COLORS]
    for i, a in enumerate(cols):
        for b in cols[i + 1 :]:
            assert max(abs(x - y) for x, y in zip(a, b, strict=True)) > DEFAULT_COLOR_TOLERANCE


@pytest.mark.parametrize("pt", [7, 9, 11])
def test_text_box_estimate_grows_with_size(pt):
    w, h = g.text_box_estimate("Render", pt)
    w2, h2 = g.text_box_estimate("Render", pt + 2)
    assert w2 > w and h2 > h
