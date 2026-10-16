"""Recipe engine: named workflows that chain Iris primitives.

A recipe is a YAML file under the repo's `recipes/` folder. It declares
a sequence of steps; each step names an action (which must be a registered
Iris MCP tool function) plus its args. Args support `${path.to.value}`
substitution from earlier step results or the recipe's input args.

Example:
    name: obs.start_recording
    description: Bring OBS to front and start recording
    inputs:
      - scene
    steps:
      - id: f
        action: focus
        args:
          match: {process: obs64.exe}
          raise_window: true
      - action: click
        args:
          token: ${f.token}
          target: "Start Recording"

Server-side wiring:
    The server (server.py) calls recipes.register_action(name, fn) for every
    MCP tool function. The recipe engine looks up actions by name in that
    registry, so any Iris tool is callable from a recipe with no extra glue.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"


# Populated lazily by server.py via register_action()
_ACTION_REGISTRY: dict[str, Callable] = {}


def register_action(name: str, fn: Callable) -> None:
    """Expose an MCP tool function to the recipe engine."""
    _ACTION_REGISTRY[name] = fn


def registered_actions() -> list[str]:
    return sorted(_ACTION_REGISTRY)


_VAR_PATTERN = re.compile(r"\$\{([a-zA-Z0-9_.\[\]]+)\}")


def _lookup(path: str, context: dict) -> Any:
    """Resolve a dotted path against the context, returning the unresolved
    `${path}` string on miss so callers can see what failed."""
    parts = path.split(".")
    cur: Any = context
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return f"${{{path}}}"
        else:
            return f"${{{path}}}"
    return cur


def _substitute(value: Any, context: dict) -> Any:
    """Recursively replace `${path}` in any string leaf of a value tree.

    If a string is EXACTLY one variable (no surrounding text), we replace it
    with the raw resolved value so callers can pass through non-string types
    like dicts. Otherwise we substring-substitute (stringifying the result).
    """
    if isinstance(value, str):
        m = _VAR_PATTERN.fullmatch(value.strip())
        if m:
            return _lookup(m.group(1), context)

        def repl(m: re.Match) -> str:
            return str(_lookup(m.group(1), context))

        return _VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, context) for v in value]
    return value


def list_recipes() -> list[dict]:
    """Enumerate every YAML file in RECIPES_DIR and return its name/description."""
    if not HAS_YAML or not RECIPES_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(RECIPES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            out.append(
                {
                    "name": data.get("name") or p.stem,
                    "description": data.get("description", ""),
                    "inputs": data.get("inputs", []),
                    "step_count": len(data.get("steps", [])),
                    "path": str(p),
                }
            )
        except Exception as e:
            out.append({"name": p.stem, "error": str(e), "path": str(p)})
    return out


def load_recipe(name: str) -> dict:
    """Resolve a recipe by filename or by its declared `name:` field."""
    if not HAS_YAML:
        raise RuntimeError("pyyaml not installed; cannot run recipes")
    if not RECIPES_DIR.exists():
        raise FileNotFoundError(f"recipes dir missing: {RECIPES_DIR}")
    direct = RECIPES_DIR / f"{name}.yaml"
    if direct.exists():
        return yaml.safe_load(direct.read_text(encoding="utf-8")) or {}
    for p in RECIPES_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if data.get("name") == name:
            return data
    raise FileNotFoundError(f"recipe not found: {name}")


def run_recipe(name: str, args: dict | None = None) -> dict:
    """Execute a recipe end-to-end, returning a structured trace.

    Each step is dispatched to its registered action function with kwargs
    expanded from substituted YAML args. If a step raises or returns a dict
    with `ok: False`, the recipe fails at that step and earlier results
    are still returned for debugging.
    """
    args = args or {}
    recipe = load_recipe(name)
    steps = recipe.get("steps") or []
    if not steps:
        return {"ok": False, "recipe": name, "error": "no steps in recipe"}

    context: dict = {"input": args}
    trace: list[dict] = []

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return {"ok": False, "recipe": name, "step": i, "error": f"step {i} is not a mapping"}
        action = step.get("action")
        if not action:
            return {"ok": False, "recipe": name, "step": i, "error": f"step {i} missing 'action'"}
        if action not in _ACTION_REGISTRY:
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "action": action,
                "error": f"unknown action '{action}'. Available: {registered_actions()[:20]}",
                "trace": trace,
            }
        step_id = step.get("id") or f"step{i}"
        raw_args = step.get("args") or {}
        try:
            resolved = _substitute(raw_args, context)
        except Exception as e:
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "step_id": step_id,
                "action": action,
                "error": f"arg substitution failed: {e}",
                "trace": trace,
            }
        if not isinstance(resolved, dict):
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "step_id": step_id,
                "action": action,
                "error": f"args must be a mapping, got {type(resolved).__name__}",
                "trace": trace,
            }

        fn = _ACTION_REGISTRY[action]
        try:
            result = fn(**resolved)
        except TypeError as e:
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "step_id": step_id,
                "action": action,
                "args": resolved,
                "error": f"action call failed: {e}",
                "trace": trace,
            }
        except Exception as e:
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "step_id": step_id,
                "action": action,
                "args": resolved,
                "error": f"{type(e).__name__}: {e}",
                "trace": trace,
            }

        # If the action returned a dict with ok=False, recipe fails here.
        if isinstance(result, dict) and result.get("ok") is False:
            return {
                "ok": False,
                "recipe": name,
                "step": i,
                "step_id": step_id,
                "action": action,
                "result": result,
                "trace": trace,
            }

        # Store result in context. Wrap non-dict results so step.field works.
        context[step_id] = result if isinstance(result, dict) else {"result": result}
        trace.append({"step": i, "step_id": step_id, "action": action, "result": result})

    return {
        "ok": True,
        "recipe": name,
        "step_count": len(trace),
        "trace": trace,
        "final": trace[-1]["result"] if trace else None,
    }
