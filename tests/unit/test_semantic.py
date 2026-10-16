import pytest

from iris.semantic import HAS_UIA, reset_uia_support_cache


def test_module_imports():
    """Smoke test: semantic module imports cleanly even if UIA missing."""
    assert isinstance(HAS_UIA, bool)


def test_reset_uia_support_cache_runs():
    reset_uia_support_cache()


@pytest.mark.skipif(not HAS_UIA, reason="UIA only")
def test_query_returns_list():
    from iris.semantic import query

    # Don't pass a real hwnd, just verify the API doesn't crash on bogus input
    result = query(99999999, name="nonexistent")
    assert isinstance(result, list)


@pytest.mark.skipif(not HAS_UIA, reason="UIA only")
def test_walk_tree_returns_list():
    from iris.semantic import walk_tree

    result = walk_tree(99999999)
    assert isinstance(result, list)


@pytest.mark.skipif(not HAS_UIA, reason="UIA only")
def test_supports_uia_returns_bool():
    from iris.semantic import supports_uia

    reset_uia_support_cache()
    assert isinstance(supports_uia(99999999, 99999999), bool)
