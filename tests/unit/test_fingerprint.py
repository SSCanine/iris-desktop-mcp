from iris.fingerprint import collect_button_names, compare, compute_fingerprint


def _node(role, name, depth=0):
    return {"role": role, "name": name, "depth": depth}


def test_identical_dumps_same_fingerprint():
    a = [_node("ButtonControl", "Save"), _node("ButtonControl", "Cancel")]
    b = [_node("ButtonControl", "Save"), _node("ButtonControl", "Cancel")]
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_different_dumps_different_fingerprint():
    a = [_node("ButtonControl", "Save")]
    b = [_node("ButtonControl", "Cancel")]
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_collect_button_names():
    dump = [
        _node("ButtonControl", "Save"),
        _node("ButtonControl", "Cancel"),
        _node("PaneControl", ""),
        _node("ButtonControl", ""),  # empty name skipped
    ]
    names = collect_button_names(dump)
    assert names == {"Save", "Cancel"}


def test_compare_no_drift():
    a = [_node("ButtonControl", "Save"), _node("ButtonControl", "Cancel")]
    b = [_node("ButtonControl", "Save"), _node("ButtonControl", "Cancel")]
    result = compare(a, b)
    assert result["drift_detected"] is False
    assert result["buttons_added"] == []
    assert result["buttons_removed"] == []


def test_compare_button_renamed():
    a = [_node("ButtonControl", "Start Recording")]
    b = [_node("ButtonControl", "Start Stream/Record")]
    result = compare(a, b)
    assert result["drift_detected"] is True
    assert "Start Stream/Record" in result["buttons_added"]
    assert "Start Recording" in result["buttons_removed"]


def test_compare_structural_change_threshold():
    a = [_node("ButtonControl", f"Btn{i}") for i in range(10)]
    b = [_node("ButtonControl", f"NewBtn{i}") for i in range(10)]
    result = compare(a, b)
    assert result["structural_change"] is True
    assert result["changed_ratio"] >= 0.5
