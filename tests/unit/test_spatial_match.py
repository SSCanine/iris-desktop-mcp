from iris.geometry import Rect
from iris.spatial import WindowInfo, match_window


def _w(hwnd, pid, exe, title):
    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        exe_name=exe,
        title=title,
        bounds=Rect(0, 0, 100, 100),
        visible=True,
        minimized=False,
    )


CANDIDATES = [
    _w(1, 100, "obs64.exe", "OBS Studio"),
    _w(2, 101, "chrome.exe", "Gmail - Chrome"),
    _w(3, 102, "chrome.exe", "GitHub - Chrome"),
    _w(4, 103, "explorer.exe", "Documents"),
]


def test_match_by_process():
    result = match_window({"process": "chrome.exe"}, candidates=CANDIDATES)
    assert len(result) == 2


def test_match_by_process_case_insensitive():
    result = match_window({"process": "CHROME.EXE"}, candidates=CANDIDATES)
    assert len(result) == 2


def test_match_by_title_contains():
    result = match_window({"title_contains": "obs"}, candidates=CANDIDATES)
    assert len(result) == 1
    assert result[0].hwnd == 1


def test_match_by_title_regex():
    result = match_window({"title_regex": r"Chrome$"}, candidates=CANDIDATES)
    assert len(result) == 2


def test_match_by_pid():
    result = match_window({"pid": 100}, candidates=CANDIDATES)
    assert len(result) == 1


def test_match_by_hwnd():
    result = match_window({"hwnd": 3}, candidates=CANDIDATES)
    assert len(result) == 1
    assert result[0].title == "GitHub - Chrome"


def test_match_combined_filters():
    result = match_window(
        {"process": "chrome.exe", "title_contains": "github"},
        candidates=CANDIDATES,
    )
    assert len(result) == 1
    assert result[0].hwnd == 3


def test_match_no_results():
    result = match_window({"process": "notepad.exe"}, candidates=CANDIDATES)
    assert result == []


def test_match_empty_spec_returns_all():
    result = match_window({}, candidates=CANDIDATES)
    assert len(result) == 4
