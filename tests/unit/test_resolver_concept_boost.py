"""Tests for the token-overlap concept boost in resolver ranking.

Covers both the nearest_matches block in find() and the score formula in
suggest_alternatives(). The boost rewards candidates that share concept words
with the target (e.g. 'Mic/Aux' vs 'GoXLR Mic' both contain 'mic') even when
edit-distance is mediocre.
"""

from __future__ import annotations

from iris import resolver as resolver_mod
from iris.geometry import Rect
from iris.resolver import _token_overlap, _tokenize, find, suggest_alternatives
from iris.tokens import FocusToken


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------
def test_tokenize_splits_on_slash_and_dash():
    assert _tokenize("Mic/Aux") == {"mic", "aux"}
    assert _tokenize("foo-bar_baz") == {"foo", "bar", "baz"}


def test_tokenize_drops_short_tokens_and_stopwords():
    assert _tokenize("a of the streaming app") == {"streaming", "app"}


def test_tokenize_lowercases():
    assert _tokenize("StartStreaming GoXLR") == {"startstreaming", "goxlr"}


def test_tokenize_returns_empty_for_empty_input():
    assert _tokenize("") == set()
    assert _tokenize(None) == set()


# ---------------------------------------------------------------------------
# _token_overlap
# ---------------------------------------------------------------------------
def test_token_overlap_mic_aux_vs_goxlr_mic_is_half():
    assert _token_overlap("Mic/Aux", "GoXLR Mic") == 0.5


def test_token_overlap_full_match_is_one():
    assert _token_overlap("Audio Mixer", "Audio Mixer") == 1.0


def test_token_overlap_no_shared_tokens_is_zero():
    assert _token_overlap("Audio Mixer", "Camera Source") == 0.0


def test_token_overlap_handles_separator_variants():
    assert _token_overlap("audio-mixer", "Audio Mixer") == 1.0
    assert _token_overlap("audio.mixer", "Audio_Mixer") == 1.0


def test_token_overlap_zero_when_either_side_empty():
    assert _token_overlap("", "anything") == 0.0
    assert _token_overlap("anything", "") == 0.0


# ---------------------------------------------------------------------------
# find() nearest_matches re-ranking
# ---------------------------------------------------------------------------
def _token():
    return FocusToken.create(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        monitor_index=0,
        bounds=Rect(0, 0, 100, 100),
    )


class _FakeOCRMatch:
    """Stand-in for vision.TextMatch with the to_dict shape."""

    def __init__(self, text: str, similarity: float):
        self.text = text
        self.similarity = similarity

    def to_dict(self):
        return {
            "text": self.text,
            "bbox": {"x": 0, "y": 0, "width": 50, "height": 20},
            "similarity": self.similarity,
            "confidence": 0.8,
        }


def test_find_nearest_matches_ranks_token_overlap_winner_first(monkeypatch):
    """'GoXLR Mic' shares 'mic' with target 'Mic/Aux'; 'Mixer' shares nothing.
    With the new ranker, GoXLR Mic must appear before Mixer in nearest_matches.
    """
    # Force OCR path: UIA off, real capture stubbed.
    monkeypatch.setattr(
        resolver_mod,
        "token_inspect",
        lambda tk: {"alive": True, "minimized": False, "occluded": False, "off_screen": False},
    )
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: object())
    monkeypatch.setattr(resolver_mod.vision_mod, "cached_ocr", lambda token_id, img: ["fake_words"])
    monkeypatch.setattr(resolver_mod.spatial_mod, "is_occluded", lambda hwnd: False)
    # First call (threshold=0.6) returns no matches (forces nearest path).
    # Second call (threshold=0.4) returns soft hits we can rank.
    soft_hits = [
        _FakeOCRMatch("Mixer", 0.5),  # higher edit-similarity, no token overlap
        _FakeOCRMatch("GoXLR Mic", 0.42),  # lower edit-similarity, but shares 'mic'
    ]

    call_count = {"n": 0}

    def fake_find_text(words, target, fuzzy, threshold):
        call_count["n"] += 1
        if threshold > 0.5:
            return []
        return soft_hits

    monkeypatch.setattr(resolver_mod.vision_mod, "find_text_in_image", fake_find_text)
    monkeypatch.setattr(
        resolver_mod.vision_mod, "encode_jpeg", lambda img, quality, optimize_tokens: (b"", 1, 1)
    )

    r = find(_token(), "Mic/Aux", capture_for_handoff=False)
    assert not r.found
    assert len(r.nearest_matches) >= 2
    # GoXLR Mic should rank ABOVE Mixer thanks to token overlap.
    first = r.nearest_matches[0]
    assert first["text"] == "GoXLR Mic"
    assert first["token_overlap"] == 0.5


def test_find_nearest_matches_includes_token_overlap_field(monkeypatch):
    monkeypatch.setattr(
        resolver_mod,
        "token_inspect",
        lambda tk: {"alive": True, "minimized": False, "occluded": False, "off_screen": False},
    )
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: object())
    monkeypatch.setattr(resolver_mod.vision_mod, "cached_ocr", lambda token_id, img: ["fake"])
    monkeypatch.setattr(resolver_mod.spatial_mod, "is_occluded", lambda hwnd: False)

    def fake_find_text(words, target, fuzzy, threshold):
        if threshold > 0.5:
            return []
        return [_FakeOCRMatch("Audio", 0.45)]

    monkeypatch.setattr(resolver_mod.vision_mod, "find_text_in_image", fake_find_text)
    monkeypatch.setattr(
        resolver_mod.vision_mod, "encode_jpeg", lambda img, quality, optimize_tokens: (b"", 1, 1)
    )

    r = find(_token(), "Audio Mixer", capture_for_handoff=False)
    assert r.nearest_matches
    assert "token_overlap" in r.nearest_matches[0]


# ---------------------------------------------------------------------------
# suggest_alternatives() boost
# ---------------------------------------------------------------------------
def test_suggest_alternatives_token_overlap_promotes_concept_match(monkeypatch):
    """suggest_alternatives() must promote candidates with token overlap."""
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    # Stub OCR pipeline to return two candidates with known similarity scores.
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: object())
    monkeypatch.setattr(resolver_mod.vision_mod, "cached_ocr", lambda token_id, img: ["fake"])

    soft = [
        _FakeOCRMatch("Mixer", 0.55),  # decent similarity, zero overlap
        _FakeOCRMatch("GoXLR Mic", 0.42),  # weaker similarity, but shares 'mic'
    ]
    monkeypatch.setattr(
        resolver_mod.vision_mod, "find_text_in_image", lambda words, target, fuzzy, threshold: soft
    )

    out = suggest_alternatives(_token(), "Mic/Aux")
    cands = out["candidates"]
    # GoXLR Mic should rank first with the new scoring.
    assert cands[0]["text"] == "GoXLR Mic"
    assert cands[0]["token_overlap"] == 0.5
    # Each candidate should now expose the token_overlap field.
    assert all("token_overlap" in c for c in cands)


def test_suggest_alternatives_falls_back_to_similarity_when_no_overlap(monkeypatch):
    """When no candidate shares concept words, ranking still works on similarity+area."""
    monkeypatch.setattr(resolver_mod.semantic_mod, "HAS_UIA", False)
    monkeypatch.setattr(resolver_mod, "_capture_window", lambda tk: object())
    monkeypatch.setattr(resolver_mod.vision_mod, "cached_ocr", lambda token_id, img: ["fake"])

    soft = [
        _FakeOCRMatch("Apple", 0.45),
        _FakeOCRMatch("Banana", 0.55),
    ]
    monkeypatch.setattr(
        resolver_mod.vision_mod, "find_text_in_image", lambda words, target, fuzzy, threshold: soft
    )

    out = suggest_alternatives(_token(), "Cherry")
    cands = out["candidates"]
    # No shared tokens with 'Cherry'; Banana wins on raw similarity.
    assert cands[0]["text"] == "Banana"
