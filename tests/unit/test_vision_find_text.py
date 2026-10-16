from iris.geometry import Rect
from iris.vision import OCRWord, find_text_in_image


def _w(text, x=0, y=0, w=50, h=20, conf=0.9):
    return OCRWord(text=text, bbox=Rect(x, y, w, h), confidence=conf)


def test_exact_match():
    words = [_w("Save"), _w("Cancel"), _w("OK")]
    matches = find_text_in_image(words, "Save")
    assert len(matches) >= 1
    assert matches[0].text == "Save"
    assert matches[0].similarity == 1.0


def test_fuzzy_match():
    words = [_w("Recordng"), _w("Cancel")]
    matches = find_text_in_image(words, "Recording", fuzzy=True, threshold=0.7)
    assert len(matches) == 1
    assert "Record" in matches[0].text


def test_ngram_match_phrase():
    words = [_w("Start", x=0), _w("Recording", x=60)]
    matches = find_text_in_image(words, "Start Recording", fuzzy=True, threshold=0.7)
    assert len(matches) >= 1
    top = matches[0]
    assert top.text == "Start Recording"
    assert top.similarity == 1.0
    # Bbox should cover both words
    assert top.bbox.left == 0
    assert top.bbox.right >= 110


def test_no_match_below_threshold():
    words = [_w("Cancel"), _w("OK")]
    matches = find_text_in_image(words, "Save", threshold=0.6)
    assert matches == []


def test_empty_query_returns_empty():
    words = [_w("Save")]
    assert find_text_in_image(words, "") == []


def test_results_sorted_by_similarity_desc():
    words = [_w("Recording"), _w("Recordingx"), _w("Recor")]
    matches = find_text_in_image(words, "Recording", fuzzy=True, threshold=0.5)
    assert matches[0].similarity >= matches[-1].similarity
