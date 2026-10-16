"""Unit tests for the recipe engine.

Tests substitution, registry, error paths. Does NOT touch real Windows.
For end-to-end recipe execution against real apps, see
tests/smoke_recipes.py.
"""

from __future__ import annotations

import pytest

from iris import recipes as recipes_mod


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test starts with a fresh action registry so we don't leak."""
    saved = dict(recipes_mod._ACTION_REGISTRY)
    recipes_mod._ACTION_REGISTRY.clear()
    yield
    recipes_mod._ACTION_REGISTRY.clear()
    recipes_mod._ACTION_REGISTRY.update(saved)


def test_register_and_list():
    def noop(**kwargs):
        return {"ok": True}

    recipes_mod.register_action("noop", noop)
    assert "noop" in recipes_mod.registered_actions()


def test_substitute_simple_string():
    out = recipes_mod._substitute("hello ${input.name}", {"input": {"name": "Iris"}})
    assert out == "hello Iris"


def test_substitute_fullmatch_preserves_type():
    """A string that is ONLY a variable returns the raw resolved value."""
    out = recipes_mod._substitute("${ctx.bounds}", {"ctx": {"bounds": {"x": 1, "y": 2}}})
    assert out == {"x": 1, "y": 2}


def test_substitute_nested_dict():
    raw = {"match": {"process": "obs64.exe"}, "token": "${f.token}"}
    out = recipes_mod._substitute(raw, {"f": {"token": "abc123"}})
    assert out == {"match": {"process": "obs64.exe"}, "token": "abc123"}


def test_substitute_unresolved_keeps_placeholder():
    out = recipes_mod._substitute("${missing.path}", {})
    assert out == "${missing.path}"


def test_run_recipe_unknown_action(tmp_path, monkeypatch):
    recipe = tmp_path / "x.yaml"
    recipe.write_text(
        "name: x\nsteps:\n  - action: nope\n    args: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    result = recipes_mod.run_recipe("x")
    assert result["ok"] is False
    assert result["action"] == "nope"
    assert "unknown action" in result["error"]


def test_run_recipe_step_returns_ok_false_fails_recipe(tmp_path, monkeypatch):
    recipes_mod.register_action("failer", lambda **kw: {"ok": False, "error": "boom"})
    recipe = tmp_path / "f.yaml"
    recipe.write_text(
        "name: f\nsteps:\n  - action: failer\n    args: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    result = recipes_mod.run_recipe("f")
    assert result["ok"] is False
    assert result["step"] == 0


def test_run_recipe_passes_context_between_steps(tmp_path, monkeypatch):
    captured: dict = {}

    def step_a(**kwargs):
        return {"ok": True, "token": "tok-xyz", "extra": 42}

    def step_b(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    recipes_mod.register_action("a", step_a)
    recipes_mod.register_action("b", step_b)

    recipe = tmp_path / "chain.yaml"
    recipe.write_text(
        "name: chain\n"
        "steps:\n"
        "  - id: first\n"
        "    action: a\n"
        "    args: {}\n"
        "  - action: b\n"
        "    args:\n"
        "      token: ${first.token}\n"
        "      number: ${first.extra}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    result = recipes_mod.run_recipe("chain")
    assert result["ok"] is True
    assert captured["token"] == "tok-xyz"
    # ${first.extra} substitutes as string since it's inline (not fullmatch w/ dict)
    # actually 42 alone in a string still becomes string "42"
    assert str(captured["number"]) == "42"


def test_run_recipe_passes_input_args(tmp_path, monkeypatch):
    captured: dict = {}

    def action(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    recipes_mod.register_action("act", action)
    recipe = tmp_path / "in.yaml"
    recipe.write_text(
        "name: in\ninputs: [url]\nsteps:\n  - action: act\n    args:\n      url: ${input.url}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    result = recipes_mod.run_recipe("in", args={"url": "https://example.com"})
    assert result["ok"] is True
    assert captured["url"] == "https://example.com"


def test_run_recipe_step_raises_is_captured(tmp_path, monkeypatch):
    def raiser(**kwargs):
        raise RuntimeError("disk full")

    recipes_mod.register_action("raiser", raiser)
    recipe = tmp_path / "r.yaml"
    recipe.write_text(
        "name: r\nsteps:\n  - action: raiser\n    args: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    result = recipes_mod.run_recipe("r")
    assert result["ok"] is False
    assert "disk full" in result["error"]


def test_load_recipe_by_name_field(tmp_path, monkeypatch):
    recipe = tmp_path / "different-filename.yaml"
    recipe.write_text("name: my.recipe\nsteps: []\n", encoding="utf-8")
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    data = recipes_mod.load_recipe("my.recipe")
    assert data["name"] == "my.recipe"


def test_list_recipes_returns_metadata(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(
        "name: a\ndescription: First\nsteps:\n  - action: x\n    args: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "name: b\ndescription: Second\nsteps:\n  - action: y\n    args: {}\n  - action: z\n    args: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes_mod, "RECIPES_DIR", tmp_path)
    out = recipes_mod.list_recipes()
    by_name = {r["name"]: r for r in out}
    assert by_name["a"]["step_count"] == 1
    assert by_name["b"]["step_count"] == 2


def test_builtin_recipes_load_cleanly():
    """All shipped recipes parse without errors."""
    recipes = recipes_mod.list_recipes()
    # Every shipped recipe should have NO 'error' key
    errors = [r for r in recipes if "error" in r]
    assert errors == [], f"recipes failed to parse: {errors}"
    names = {r["name"] for r in recipes}
    assert "obs.start_recording" in names
    assert "obs.stop_recording" in names
    assert "chrome.open_url" in names
    assert "vscode.command_palette" in names
    assert "alt_tab_to" in names
