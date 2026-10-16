"""Tests for iris.panels: panel discovery and item enumeration on synthetic UIA trees."""

from __future__ import annotations

from iris.panels import (
    _detect_tabbed,
    _is_dock_node,
    _item_group_key,
    _ocr_label_in_band,
    discover_panel_items,
    discover_panels,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
WIN_BOUNDS = {"x": 0, "y": 0, "width": 2560, "height": 1392}


def _node(**kw) -> dict:
    """Build a UIA tree node dict with sane defaults."""
    base = {
        "name": "",
        "role": "PaneControl",
        "automation_id": "",
        "class_name": "",
        "bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
        "enabled": True,
        "is_offscreen": False,
        "depth": 1,
    }
    base.update(kw)
    return base


def _obs_like_tree() -> list[dict]:
    """Synthetic OBS-shaped UIA tree with the 5 reference panels."""
    return [
        _node(
            role="WindowControl",
            name="OBS",
            depth=0,
            bounds={"x": 0, "y": 0, "width": 2560, "height": 1392},
        ),
        _node(
            name="Multiple output",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.obs-multi-rtmp-dock",
            bounds={"x": 0, "y": 50, "width": 334, "height": 970},
            depth=1,
        ),
        _node(
            name="Scenes",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.scenesDock",
            bounds={"x": 0, "y": 1024, "width": 305, "height": 334},
            depth=1,
        ),
        _node(
            name="Sources",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.sourcesDock",
            bounds={"x": 309, "y": 1024, "width": 306, "height": 300},
            depth=1,
        ),
        _node(
            name="Audio Mixer",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.mixerDock",
            bounds={"x": 619, "y": 1024, "width": 1294, "height": 334},
            depth=1,
        ),
        _node(
            name="Controls",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.controlsDock",
            bounds={"x": 1917, "y": 1024, "width": 643, "height": 300},
            depth=1,
        ),
        _node(
            name="Stats",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.statsDock",
            bounds={"x": -2542, "y": -754, "width": 929, "height": 200},
            depth=1,
        ),
    ]


def _ocr_words_with_titles() -> list[dict]:
    """OCR words sitting in each panel's title band."""
    return [
        {
            "text": "Multiple",
            "bbox": {"x": 17, "y": 56, "width": 80, "height": 20},
            "confidence": 0.93,
        },
        {
            "text": "output",
            "bbox": {"x": 100, "y": 56, "width": 80, "height": 20},
            "confidence": 0.93,
        },
        {
            "text": "Scenes",
            "bbox": {"x": 12, "y": 1030, "width": 70, "height": 20},
            "confidence": 0.90,
        },
        {
            "text": "Sources",
            "bbox": {"x": 325, "y": 1041, "width": 80, "height": 20},
            "confidence": 0.90,
        },
        {
            "text": "Audio",
            "bbox": {"x": 635, "y": 1040, "width": 60, "height": 20},
            "confidence": 0.96,
        },
        {
            "text": "Mixer",
            "bbox": {"x": 700, "y": 1040, "width": 60, "height": 20},
            "confidence": 0.96,
        },
        {
            "text": "Controls",
            "bbox": {"x": 1933, "y": 1040, "width": 90, "height": 20},
            "confidence": 0.87,
        },
    ]


# ---------------------------------------------------------------------------
# discover_panels
# ---------------------------------------------------------------------------
def test_discover_panels_finds_all_obs_docks_with_ocr_confirmation():
    tree = _obs_like_tree()
    ocr = _ocr_words_with_titles()
    panels = discover_panels(tree, ocr, WIN_BOUNDS)
    visible_names = [p["name"] for p in panels if not p["hidden"]]
    assert "Multiple output" in visible_names
    assert "Scenes" in visible_names
    assert "Sources" in visible_names
    assert "Audio Mixer" in visible_names
    assert "Controls" in visible_names
    # All five visible should be very_high since OCR confirmed each.
    very_high_visible = [p for p in panels if not p["hidden"] and p["confidence"] == "very_high"]
    assert len(very_high_visible) == 5


def test_discover_panels_marks_offscreen_dock_hidden():
    tree = _obs_like_tree()
    panels = discover_panels(tree, [], WIN_BOUNDS)
    stats = next(p for p in panels if p["name"] == "Stats")
    assert stats["hidden"] is True


def test_discover_panels_without_ocr_still_finds_uia_docks_at_high_confidence():
    tree = _obs_like_tree()
    panels = discover_panels(tree, [], WIN_BOUNDS)
    visible = [p for p in panels if not p["hidden"]]
    assert all(p["confidence"] == "high" for p in visible), (
        "without OCR, strong UIA matches should be 'high' not 'very_high'"
    )


def test_discover_panels_falls_back_to_weak_pane_heuristic():
    """A generic Pane at depth<=2 with substantial area still surfaces."""
    tree = [
        _node(
            role="WindowControl", depth=0, bounds={"x": 0, "y": 0, "width": 1000, "height": 1000}
        ),
        _node(
            name="Side bar",
            role="PaneControl",
            depth=1,
            bounds={"x": 0, "y": 0, "width": 300, "height": 1000},
        ),
    ]
    panels = discover_panels(tree, [], {"x": 0, "y": 0, "width": 1000, "height": 1000})
    names = [p["name"] for p in panels]
    assert "Side bar" in names


def test_discover_panels_returns_empty_for_empty_tree():
    assert discover_panels([], [], WIN_BOUNDS) == []


def test_discover_panels_dedupes_identical_nodes():
    """Qt sometimes wraps a dock multiple times; we should report it once."""
    n = _node(
        name="Scenes",
        class_name="QDockWidget",
        automation_id="scenesDock",
        bounds={"x": 0, "y": 1024, "width": 305, "height": 334},
    )
    tree = [
        _node(
            role="WindowControl", depth=0, bounds={"x": 0, "y": 0, "width": 2000, "height": 2000}
        ),
        n,
        dict(n),
    ]
    panels = discover_panels(tree, [], {"x": 0, "y": 0, "width": 2000, "height": 2000})
    matches = [p for p in panels if p["name"] == "Scenes"]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def test_is_dock_node_matches_qt_and_obs_classes():
    assert _is_dock_node({"class_name": "OBSDock", "automation_id": ""})
    assert _is_dock_node({"class_name": "QDockWidget", "automation_id": ""})
    assert not _is_dock_node({"class_name": "QPushButton", "automation_id": ""})


def test_is_dock_node_matches_automation_id_pattern():
    assert _is_dock_node({"class_name": "", "automation_id": "scenesDock"})
    assert _is_dock_node({"class_name": "", "automation_id": "obs-multi-rtmp-dock"})
    assert not _is_dock_node({"class_name": "", "automation_id": "regularButton"})


def test_ocr_label_in_band_finds_token_match():
    bounds = {"x": 0, "y": 1024, "width": 305, "height": 334}
    ocr = [
        {
            "text": "Scenes",
            "bbox": {"x": 12, "y": 1030, "width": 70, "height": 20},
            "confidence": 0.9,
        }
    ]
    hit = _ocr_label_in_band(bounds, "Scenes", ocr)
    assert hit is not None
    assert hit["text"] == "Scenes"


def test_ocr_label_in_band_misses_when_text_outside_band():
    bounds = {"x": 0, "y": 1024, "width": 305, "height": 334}
    ocr = [
        {
            "text": "Scenes",
            "bbox": {"x": 12, "y": 1300, "width": 70, "height": 20},
            "confidence": 0.9,
        }
    ]
    assert _ocr_label_in_band(bounds, "Scenes", ocr) is None


def test_detect_tabbed_when_tabbar_overlaps_bottom():
    panel_b = {"x": 0, "y": 1024, "width": 600, "height": 300}
    nodes = [
        _node(
            class_name="QMainWindowTabBar",
            bounds={"x": 5, "y": 1320, "width": 200, "height": 25},
            depth=2,
        ),
    ]
    assert _detect_tabbed(panel_b, nodes) is True


# ---------------------------------------------------------------------------
# discover_panel_items
# ---------------------------------------------------------------------------
def _mixer_subtree() -> list[dict]:
    """Synthetic mixer dock with a GoXLR Mic row containing a label and mute toggle."""
    return [
        _node(
            role="WindowControl",
            name="OBS",
            depth=0,
            bounds={"x": 0, "y": 0, "width": 2560, "height": 1392},
        ),
        _node(
            name="Audio Mixer",
            class_name="OBSDock",
            automation_id="OBSApp.OBSBasic.mixerDock",
            bounds={"x": 619, "y": 1024, "width": 1294, "height": 334},
            depth=1,
        ),
        _node(
            name="GoXLR Mic",
            role="ButtonControl",
            automation_id="OBSApp.OBSBasic.mixerDock.AudioMixer.GoXLR Mic.VolumeName",
            bounds={"x": 714, "y": 1065, "width": 74, "height": 21},
            depth=4,
        ),
        _node(
            name="Mute 'GoXLR Mic'",
            role="CheckBoxControl",
            automation_id="OBSApp.OBSBasic.mixerDock.AudioMixer.GoXLR Mic.QPushButton",
            bounds={"x": 727, "y": 1299, "width": 24, "height": 22},
            depth=4,
        ),
        _node(
            name="Desktop Audio",
            role="ButtonControl",
            automation_id="OBSApp.OBSBasic.mixerDock.AudioMixer.Desktop Audio.VolumeName",
            bounds={"x": 850, "y": 1065, "width": 100, "height": 21},
            depth=4,
        ),
    ]


def test_discover_panel_items_finds_audio_mixer_rows():
    tree = _mixer_subtree()
    out = discover_panel_items(tree, "Audio Mixer")
    assert out["found"] is True
    item_names = {it["key"] for it in out["items"]}
    assert "GoXLR Mic" in item_names
    assert "Desktop Audio" in item_names


def test_discover_panel_items_collapses_label_and_mute_into_one_row():
    tree = _mixer_subtree()
    out = discover_panel_items(tree, "Audio Mixer")
    goxlr = next(it for it in out["items"] if it["key"] == "GoXLR Mic")
    assert len(goxlr["controls"]) == 2
    roles = {c["role"] for c in goxlr["controls"]}
    assert "ButtonControl" in roles
    assert "CheckBoxControl" in roles


def test_discover_panel_items_resolves_panel_by_automation_id():
    tree = _mixer_subtree()
    out = discover_panel_items(tree, "OBSApp.OBSBasic.mixerDock")
    assert out["found"] is True
    assert out["panel"]["name"] == "Audio Mixer"


def test_discover_panel_items_returns_not_found_when_panel_missing():
    out = discover_panel_items(_obs_like_tree(), "NonexistentPanel")
    assert out["found"] is False


def test_discover_panel_items_excludes_descendants_outside_panel_bounds():
    """A button outside the parent's bbox must not appear in its items."""
    tree = [
        _node(
            role="WindowControl", depth=0, bounds={"x": 0, "y": 0, "width": 2000, "height": 2000}
        ),
        _node(
            name="Panel A",
            automation_id="panelA",
            class_name="QDockWidget",
            bounds={"x": 0, "y": 0, "width": 500, "height": 500},
            depth=1,
        ),
        _node(
            name="Inside",
            role="ButtonControl",
            bounds={"x": 50, "y": 50, "width": 80, "height": 30},
            depth=2,
        ),
        _node(
            name="Outside",
            role="ButtonControl",
            bounds={"x": 1000, "y": 1000, "width": 80, "height": 30},
            depth=2,
        ),
    ]
    out = discover_panel_items(tree, "panelA")
    keys = {it["key"] for it in out["items"]}
    assert "Inside" in keys
    assert "Outside" not in keys


def test_item_group_key_strips_qt_boilerplate():
    node = {
        "automation_id": "OBSApp.OBSBasic.mixerDock.AudioMixer.GoXLR Mic.VolumeName",
        "name": "GoXLR Mic",
    }
    assert _item_group_key(node) == "GoXLR Mic"


def test_item_group_key_falls_back_to_name_when_aid_useless():
    node = {"automation_id": "qt_genericwidget", "name": "Plain Button"}
    assert _item_group_key(node) == "Plain Button"
