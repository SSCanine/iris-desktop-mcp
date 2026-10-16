"""Set-of-Mark: enumeration, merging, numbering, registry, drawing, and the
quiet-click LPARAM math."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from iris import input as input_mod
from iris import marks, vision
from iris.geometry import Rect
from iris.tokens import FocusToken
from iris.vision import OCRWord

WIN = Rect(100, 100, 800, 600)


@dataclass
class FakeControl:
    name: str
    role: str
    bounds: Rect
    is_offscreen: bool = False
    enabled: bool = True
    raw: object = None


# --- UIA candidates -----------------------------------------------------------
def test_uia_candidates_filters_containers_offscreen_outside_and_nameless():
    controls = [
        FakeControl("Save", "ButtonControl", Rect(120, 120, 80, 30)),
        FakeControl("Main", "WindowControl", Rect(100, 100, 800, 600)),  # container
        FakeControl("Panel", "PaneControl", Rect(100, 100, 400, 300)),  # container
        FakeControl("Ghost", "ButtonControl", Rect(120, 200, 80, 30), is_offscreen=True),
        FakeControl("Outside", "ButtonControl", Rect(2000, 2000, 80, 30)),
        FakeControl("", "TextControl", Rect(300, 120, 50, 20)),  # nameless text
        FakeControl("Title", "TextControl", Rect(300, 160, 50, 20)),
        FakeControl("Huge", "ListItemControl", Rect(100, 100, 780, 500)),  # window-sized
        FakeControl("Save", "ButtonControl", Rect(121, 121, 80, 30)),  # duplicate rect
    ]
    out = marks.uia_candidates(controls, WIN)
    assert [m.text for m in out] == ["Save", "Title"]
    assert out[0].role == "button" and out[0].source == "uia" and out[0].control is controls[0]


# --- OCR runs -----------------------------------------------------------------
def test_ocr_runs_merge_adjacent_words_on_a_line():
    words = [
        OCRWord("Start", Rect(10, 10, 40, 12), 0.9, line=1),
        OCRWord("Recording", Rect(54, 10, 70, 12), 0.9, line=1),  # gap 4
        OCRWord("Far", Rect(300, 10, 30, 12), 0.9, line=1),  # gap 176: new run
        OCRWord("Below", Rect(10, 40, 40, 12), 0.9, line=2),
    ]
    runs = marks.ocr_runs(words, (100, 100))
    assert [r.text for r in runs] == ["Start Recording", "Far", "Below"]
    assert runs[0].bbox == Rect(110, 110, 114, 12)  # translated + merged
    assert runs[0].source == "ocr" and runs[0].role == "text"


def test_ocr_runs_without_line_ids_use_vertical_proximity():
    words = [OCRWord("a", Rect(0, 0, 10, 12), 0.9), OCRWord("b", Rect(12, 2, 10, 12), 0.9)]
    assert [r.text for r in marks.ocr_runs(words, (0, 0))] == ["a b"]


# --- merge + numbering --------------------------------------------------------
def test_merge_drops_ocr_covered_by_uia_and_numbers_in_reading_order():
    uia = [
        marks.Mark(0, Rect(500, 120, 80, 30), "Cancel", "button", "uia"),
        marks.Mark(0, Rect(120, 120, 80, 30), "Save", "button", "uia"),
    ]
    ocr = [
        marks.Mark(0, Rect(130, 128, 40, 14), "Save", "text", "ocr"),  # inside Save button
        marks.Mark(0, Rect(120, 300, 200, 14), "Some caption", "text", "ocr"),
        marks.Mark(0, Rect(120, 60, 100, 14), "Header", "text", "ocr"),
    ]
    out = marks.merge(uia, ocr)
    assert [m.text for m in out] == ["Header", "Save", "Cancel", "Some caption"]
    assert [m.id for m in out] == [1, 2, 3, 4]


def test_merge_caps_and_prefers_uia():
    uia = [marks.Mark(0, Rect(0, i * 20, 50, 16), f"u{i}", "button", "uia") for i in range(5)]
    ocr = [marks.Mark(0, Rect(100, i * 20, 50, 16), f"o{i}", "text", "ocr") for i in range(10)]
    out = marks.merge(uia, ocr, cap=8)
    assert len(out) == 8
    assert sum(1 for m in out if m.source == "uia") == 5
    assert [m.id for m in out] == list(range(1, 9))


# --- registry + collect -------------------------------------------------------
def _token():
    return FocusToken.create(
        hwnd=42, pid=1, exe_name="x.exe", title="T", monitor_index=0, bounds=WIN
    )


def test_collect_marks_registers_and_get_resolves(monkeypatch):
    tok = _token()
    monkeypatch.setattr(FocusToken, "current_bounds", lambda self: WIN)
    monkeypatch.setattr(marks.semantic_mod, "HAS_UIA", True)
    monkeypatch.setattr(marks.semantic_mod, "supports_uia", lambda hwnd, pid: True)
    monkeypatch.setattr(
        marks.semantic_mod,
        "query",
        lambda hwnd, **kw: [FakeControl("OK", "ButtonControl", Rect(150, 400, 60, 24))],
    )
    img = Image.new("RGB", (800, 600), (255, 255, 255))
    monkeypatch.setattr(vision, "capture_window", lambda hwnd: img)
    monkeypatch.setattr(
        vision,
        "cached_ocr",
        lambda tid, im, **kw: [OCRWord("Hello", Rect(20, 20, 40, 12), 0.9, line=1)],
    )
    ms = marks.collect_marks(tok)
    assert [m.text for m in ms.marks] == ["Hello", "OK"]
    assert ms.get(2).text == "OK" and ms.get(2).control is not None
    assert ms.get(9) is None
    assert marks.get_marks(tok.id) is ms
    assert ms.capture_digest == vision.exact_digest(img)
    assert ms.legend()[0]["center"] == [140, 126]
    # Stale detection: same bounds + same pixels -> fresh; a changed pixel -> stale.
    assert marks.is_stale(ms, tok) is False
    img2 = img.copy()
    img2.putpixel((5, 5), (0, 0, 0))
    monkeypatch.setattr(vision, "capture_window", lambda hwnd: img2)
    assert marks.is_stale(ms, tok) is True
    marks.clear_marks(tok.id)
    assert marks.get_marks(tok.id) is None


# --- drawing ------------------------------------------------------------------
def test_draw_marks_changes_pixels_and_keeps_size():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    ms = [
        marks.Mark(1, Rect(1010, 1010, 80, 30), "Save", "button", "uia"),
        marks.Mark(2, Rect(1200, 1100, 120, 20), "caption", "text", "ocr"),
    ]
    out = vision.draw_marks(img, ms, (1000, 1000), 1.0)
    assert out.size == img.size and out.tobytes() != img.tobytes()
    # Shrunk image: still draws without error.
    out2 = vision.draw_marks(img.resize((200, 150)), ms, (1000, 1000), 0.5)
    assert out2.size == (200, 150)


# --- quiet click lparam -------------------------------------------------------
def test_lparam_packs_client_coords_including_negatives():
    assert input_mod.lparam_xy(10, 20) == (20 << 16) | 10
    assert input_mod.lparam_xy(-1, 5) == (5 << 16) | 0xFFFF
    assert input_mod.lparam_xy(3, -2) == (0xFFFE << 16) | 3
