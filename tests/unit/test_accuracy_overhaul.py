"""Unit coverage for the 2026-09 accuracy overhaul: UIA ranking, same-line
OCR joins, exact cache invalidation, screenshot metadata/grid, the pixel path
through the resolver, and telemetry."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from PIL import Image, ImageDraw

from iris import pixel, resolver, telemetry, vision
from iris.geometry import Rect
from iris.tokens import FocusToken
from iris.vision import OCRWord, find_text_in_image


# ---------------------------------------------------------------------------
# UIA ranking
# ---------------------------------------------------------------------------
@dataclass
class FakeControl:
    name: str
    role: str
    bounds: Rect = Rect(10, 10, 80, 24)
    enabled: bool = True
    is_offscreen: bool = False
    raw: object = None

    def to_dict(self):
        return {"name": self.name, "role": self.role, "bounds": self.bounds.to_dict()}


def test_rank_prefers_exact_button_over_substring_list_rows():
    rows = [FakeControl(f"please create {i}", "ListItemControl") for i in range(12)]
    button = FakeControl("Create", "ButtonControl")
    window = FakeControl("Create channel - Telegram", "WindowControl", Rect(0, 0, 900, 700))
    ranked = resolver.rank_uia_candidates(rows + [window, button], "Create")
    assert ranked[0][1] is button
    # The window's own title node must never be the top hit.
    assert ranked[-1][1] is window


def test_rank_prefer_roles_overrides_role_defaults():
    tab = FakeControl("Settings", "TabItemControl")
    text = FakeControl("Settings", "TextControl")
    ranked = resolver.rank_uia_candidates([text, tab], "Settings", prefer_roles=["Text"])
    assert ranked[0][1] is text
    ranked = resolver.rank_uia_candidates([text, tab], "Settings")
    assert ranked[0][1] is tab


def test_rank_sinks_zero_area_and_offscreen():
    ghost = FakeControl("Save", "ButtonControl", Rect(0, 0, 0, 0))
    hidden = FakeControl("Save", "ButtonControl", is_offscreen=True)
    real = FakeControl("Save", "ButtonControl")
    ranked = resolver.rank_uia_candidates([ghost, hidden, real], "Save")
    assert ranked[0][1] is real


def test_normalize_role_strips_control_suffix():
    assert resolver.normalize_role("ButtonControl") == "button"
    assert resolver.normalize_role("Button") == "button"
    assert resolver.normalize_role(None) == ""


# ---------------------------------------------------------------------------
# OCR: same-line n-grams, ordering
# ---------------------------------------------------------------------------
def test_ngram_does_not_join_words_on_different_lines():
    words = [
        OCRWord("Start", Rect(10, 10, 40, 12), 0.9, line=1),
        OCRWord("Recording", Rect(10, 60, 70, 12), 0.9, line=2),  # next row
    ]
    assert find_text_in_image(words, "Start Recording", threshold=0.9) == []
    same = [
        OCRWord("Start", Rect(10, 10, 40, 12), 0.9, line=1),
        OCRWord("Recording", Rect(55, 10, 70, 12), 0.9, line=1),
    ]
    m = find_text_in_image(same, "Start Recording", threshold=0.9)
    assert m and m[0].text == "Start Recording"
    assert m[0].bbox.to_dict() == {"x": 10, "y": 10, "width": 115, "height": 12}


def test_ngram_unknown_line_identity_joins_freely():
    words = [
        OCRWord("Start", Rect(0, 0, 40, 12), 0.9),
        OCRWord("Recording", Rect(0, 40, 70, 12), 0.9),
    ]
    assert find_text_in_image(words, "Start Recording", threshold=0.9)


def test_exact_match_outranks_fuzzy_on_tie():
    words = [
        OCRWord("Mix", Rect(0, 0, 20, 10), 0.5, line=1),
        OCRWord("Mid", Rect(50, 0, 20, 10), 0.99, line=1),
    ]
    m = find_text_in_image(words, "Mid", threshold=0.6)
    assert m[0].text == "Mid"


# ---------------------------------------------------------------------------
# Cache: exact digest catches single-pixel changes
# ---------------------------------------------------------------------------
def test_exact_digest_changes_on_one_pixel():
    a = Image.new("RGB", (300, 200), (255, 255, 255))
    b = a.copy()
    b.putpixel((150, 100), (255, 0, 0))
    assert vision.phash(a) == vision.phash(b), "phash is blind to this (that was the bug)"
    assert vision.exact_digest(a) != vision.exact_digest(b)


def test_cached_ocr_misses_when_pixels_change(monkeypatch):
    calls = []
    monkeypatch.setattr(vision, "ocr_text", lambda img, **kw: calls.append(1) or [])
    vision.clear_ocr_cache()
    a = Image.new("RGB", (300, 200), (255, 255, 255))
    b = a.copy()
    b.putpixel((10, 10), (0, 0, 0))
    vision.cached_ocr("tok", a)
    vision.cached_ocr("tok", a)
    vision.cached_ocr("tok", b)
    assert len(calls) == 2
    # Different scale is a different cache slot.
    vision.cached_ocr("tok", b, scale=2.0)
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Screenshot truth
# ---------------------------------------------------------------------------
def test_capture_meta_formula():
    meta = vision.capture_meta((-3840, 0), (8320, 2160), (1568, 407))
    assert meta["scale"] == pytest.approx(1568 / 8320, abs=1e-4)
    assert meta["origin"] == [-3840, 0]
    assert "-3840" in meta["how_to_click"]


def test_grid_step_scales_with_shrink():
    assert vision._pick_grid_step(1.0) == 100
    assert vision._pick_grid_step(0.19) == 500
    assert vision._pick_grid_step(3.0) == 25


def test_draw_grid_keeps_size_and_changes_pixels():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    out = vision.draw_grid(img, (1000, 500), 1.0)
    assert out.size == img.size
    assert out.tobytes() != img.tobytes()


# ---------------------------------------------------------------------------
# Resolver pixel path (no Win32 needed: capture + inspect are monkeypatched)
# ---------------------------------------------------------------------------
def _fake_token(x=1000, y=500):
    tk = FocusToken.create(
        hwnd=1, pid=1, exe_name="x.exe", title="T", monitor_index=0, bounds=Rect(x, y, 400, 300)
    )
    return tk


def test_resolver_pixel_color_translates_to_screen(monkeypatch):
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([100, 50, 103, 53], fill=(255, 59, 48))
    monkeypatch.setattr(resolver, "_capture_window", lambda tok: img)
    monkeypatch.setattr(resolver, "token_inspect", lambda tok: {"alive": True})
    monkeypatch.setattr(FocusToken, "current_bounds", lambda self: Rect(1000, 500, 400, 300))
    monkeypatch.setattr(telemetry, "record", lambda *a, **k: None)
    r = resolver.find(_fake_token(), "color:#ff3b30")
    assert r.found and r.backend == "pixel"
    assert r.hits[0]["bbox"] == {"x": 1100, "y": 550, "width": 4, "height": 4}
    assert r.controls == [None]


def test_resolver_pixel_no_match_is_handoff_with_screenshot(monkeypatch):
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    monkeypatch.setattr(resolver, "_capture_window", lambda tok: img)
    monkeypatch.setattr(resolver, "token_inspect", lambda tok: {"alive": True})
    monkeypatch.setattr(telemetry, "record", lambda *a, **k: None)
    r = resolver.find(_fake_token(), "color:#00ff00")
    assert not r.found and r.backend == "pixel"
    assert r.screenshot is not None
    assert any("No pixels matched" in n for n in r.notes)


def test_resolver_bad_pixel_grammar_reports_not_ocr(monkeypatch):
    monkeypatch.setattr(resolver, "token_inspect", lambda tok: {"alive": True})
    monkeypatch.setattr(telemetry, "record", lambda *a, **k: None)
    r = resolver.find(_fake_token(), "color:#nope")
    assert not r.found and r.backend == "pixel"
    assert r.backends_tried == ["pixel"]
    assert any(n.startswith("bad_pixel_target") for n in r.notes)


def test_find_result_marks_ambiguity():
    r = resolver.FindResult(found=True, backend="ocr", hits=[{"a": 1}, {"b": 2}])
    assert r.to_dict().get("ambiguous") is True
    r1 = resolver.FindResult(found=True, backend="ocr", hits=[{"a": 1}])
    assert "ambiguous" not in r1.to_dict()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def test_telemetry_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_DISABLED", False)
    telemetry.configure(tmp_path)
    telemetry.record("click", target="Save", ok=True, big="x" * 500, blob=b"1234")
    lines = (tmp_path / telemetry.EVENTS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "click" and rec["target"] == "Save" and rec["ok"] is True
    assert rec["big"].endswith("...") and rec["blob"] == "<4 bytes>"
    assert telemetry.tail(5)[0]["kind"] == "click"


def test_pixel_spec_roundtrip_matches_resolver_grammar():
    assert pixel.is_pixel_target("COLOR:#ffffff")
    assert not pixel.is_pixel_target("Colorful button")


def test_ocr_second_pass_uses_upscale_and_block_psm(monkeypatch):
    """Pass 1: native scale, default psm. Pass 2 (only when pass 1 finds
    nothing): 2x upscale with the block segmentation mode."""
    calls = []

    def fake_cached_ocr(token_id, img, *, scale=None, psm=None):
        calls.append((scale, psm))
        return []

    monkeypatch.setattr(vision, "cached_ocr", fake_cached_ocr)
    img = Image.new("RGB", (800, 600), (255, 255, 255))
    words, matches, note = resolver._ocr_passes(_fake_token(), img, "Save", True, 0.6)
    assert matches == []
    assert calls == [(None, None), (vision.OCR_UPSCALE_FACTOR, vision.OCR_PASS2_PSM)]
    assert note and note.startswith("ocr_pass2")
    # A capture too large for a cheap upscale gets one pass only.
    calls.clear()
    big = Image.new("RGB", (3840, 2160), (255, 255, 255))
    resolver._ocr_passes(_fake_token(), big, "Save", True, 0.6)
    assert calls == [(None, None)]
