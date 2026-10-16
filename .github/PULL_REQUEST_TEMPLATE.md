<!-- Thanks for the contribution. Keep the PR focused; one concern per PR. -->

## What changed

<!-- One paragraph. What's different now? -->

## Why

<!-- The user-facing reason. Bug? New scenario? Accuracy improvement? -->

## How I verified

- [ ] `python -m pytest tests/unit tests/integration -q` (paste tail)
- [ ] `python -m iris.bench.runner` (paste summary block)
- [ ] Manual repro (if applicable): describe

## Scope check

- [ ] No new personal references (paths, usernames, host names)
- [ ] No new hardcoded coordinates (uses live `current_bounds(hwnd)`)
- [ ] MCP-facing errors are dict returns, not raised exceptions
- [ ] If I added/changed a tool, I updated README.md tool inventory

## Notes for reviewers

<!-- Anything I'm not sure about or want a second opinion on. -->
