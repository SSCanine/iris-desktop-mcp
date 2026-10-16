# Contributing to Iris

Thanks for considering a contribution. Iris is small and the surface area
matters a lot, so the bar for "what gets merged" is shaped accordingly.

## Quick start

```powershell
git clone https://github.com/SSCanine/iris-desktop-mcp.git
cd iris-desktop-mcp
python -m pip install -e ".[dev]"
python -m pytest tests/unit tests/integration -q
python -m iris.bench.runner --scenarios baseline_static
```

## Where to start

Easiest wins first:

- **App recipes.** Drop a `recipes/<your_app>.<verb>.yaml` and add it to the
  recipes gallery in the README. No code review needed beyond "does it run".
- **Bench scenarios.** Add a new scenario to `iris/bench/scenarios.py` that
  exercises a real-world failure mode. The bench is how we catch regressions.
- **`is_invoke_trusted` denylist entries.** Found another widget class whose
  UIA Invoke silently no-ops? Add it to `_INVOKE_DENYLIST_CLASSES` in
  `iris/semantic.py` along with a test that proves it fires.

Medium changes:

- New MCP tools that fit Iris's "eyes and hands" scope (see the README's
  "Where Iris is technically better" section for what's in vs out of scope).
- Performance improvements with bench numbers attached.

Big changes:

- A new resolver backend. Talk to maintainers first; the resolver contract
  is small but invariants matter.
- A second harness for non-Tk apps. UIA-rich apps need different
  ground-truth.

## What "ready for review" means

1. **Tests pass.** `python -m pytest tests/unit tests/integration` is green.
2. **Bench is green.** `python -m iris.bench.runner` reports correct-button
   rate at or above the baseline you saw before your change. If your change
   improves it, name the metric in the PR.
3. **No new personal references.** Don't bake in paths or usernames. If your
   feature needs config, externalize it via env var + `platformdirs`.
4. **No new hardcoded coordinates.** Iris is per-monitor DPI aware on
   purpose. New code that touches pixels must read `current_bounds(hwnd)`
   live, never from `bounds_at_creation`.
5. **Errors are dicts, not exceptions.** MCP-facing code returns
   `{"ok": False, "reason": "..."}` rather than raising. Internal helpers
   may raise, but they get caught at the tool boundary.

## Style

- 4-space indent.
- Type hints on every public function.
- Docstrings on every public function. Concise. Explain the WHY when the
  HOW isn't obvious.
- No emoji in code or PR titles unless the change is specifically about
  emoji handling.
- Comments explain non-obvious decisions (workarounds, invariants), not
  what the code does.

## PR process

1. Open an issue first for anything bigger than a typo. Saves you work.
2. Fork, branch from `main`, push, open PR.
3. CI runs unit + integration tests on Windows. Bench is run by maintainers
   manually since it needs a real display.
4. Squash-merge by default. Commit message becomes the title; body becomes
   the squash body.

## Reporting bugs

Use the bug template. Include:
- Your Windows version + Python version
- `iris-mcp doctor` output
- A minimal repro
- What you expected vs what happened

## Reporting accuracy regressions

Run `iris-mcp bench` and attach the JSON report from
`$TEMP/iris_bench_report.json`. Include screenshots from
`$TEMP/iris_bench_failures/` if your scenario hit any.

## Security

See SECURITY.md. Don't file public issues for vulnerabilities.

## License

Iris is MIT. By contributing you agree your contributions are licensed under
the same terms.
