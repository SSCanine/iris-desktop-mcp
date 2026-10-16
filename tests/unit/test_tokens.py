from iris.geometry import Rect
from iris.tokens import FocusToken, TokenRegistry, default_registry


def _make():
    return FocusToken.create(
        hwnd=1234,
        pid=99,
        exe_name="obs64.exe",
        title="OBS Studio",
        monitor_index=1,
        bounds=Rect(0, 0, 1920, 1080),
    )


def test_token_creation():
    tk = _make()
    assert tk.hwnd == 1234
    assert tk.pid == 99
    assert tk.id is not None
    assert len(tk.id) == 8
    assert tk.parent_hwnd is None
    assert tk.fingerprint is None


def test_token_with_fingerprint():
    tk = FocusToken.create(1, 2, "x.exe", "X", 1, Rect(0, 0, 100, 100), fingerprint="abc123")
    assert tk.fingerprint == "abc123"


def test_registry_store_and_get():
    reg = TokenRegistry()
    tk = _make()
    reg.store(tk)
    assert reg.get(tk.id) is tk


def test_registry_remove():
    reg = TokenRegistry()
    tk = _make()
    reg.store(tk)
    reg.remove(tk.id)
    assert reg.get(tk.id) is None


def test_registry_all_and_clear():
    reg = TokenRegistry()
    a = _make()
    b = _make()
    reg.store(a)
    reg.store(b)
    assert len(reg.all()) == 2
    reg.clear()
    assert len(reg.all()) == 0


def test_token_age():
    tk = _make()
    assert tk.age_seconds() < 1.0


def test_token_to_dict():
    tk = _make()
    d = tk.to_dict()
    assert d["hwnd"] == 1234
    assert d["title"] == "OBS Studio"
    assert d["bounds"] == {"x": 0, "y": 0, "width": 1920, "height": 1080}
    assert d["parent_hwnd"] is None


def test_default_registry_is_singleton():
    assert default_registry() is default_registry()
