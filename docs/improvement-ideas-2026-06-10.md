# Iris improvement ideas — from live Telegram Desktop driving (2026-06-10)

Captured while driving Telegram Desktop through channel creation + bot connect.
These are real friction points hit during the session, not hypotheticals.

## 1. `find()` over-matches text-heavy apps (HIGH)
`find(token, "Create")` in Telegram returned ~200 ListItem chat rows whose
message text contained "create" before the actual **Create button**. The
resolver should rank by **role match** when the target looks like a control:
prefer Button/MenuItem/Tab over ListItem/Text. Add an optional
`role=` / `prefer_roles=` arg to `find`/`click` so callers can say
"the Create *button*, not a message that says create". Quick win, big
reliability gain on chat/feed UIs.

## 2. Re-focus invalidates list-position clicks (HIGH)
Pattern that bit me: focus → see list → `click(x,y)` of a row. Between the
snapshot and the click, the chat list scrolled/reshuffled on re-focus and the
click opened the *wrong* chat. Mitigations to build:
- A `read_active_context()` / assert helper: "what window+title is focused
  right now" so a caller can verify before acting (I had to screenshot+OCR the
  title bar manually).
- `click(..., expect_title_contains=...)` guard that re-checks the focused
  window's title/active-chat header and refuses if it changed.

## 3. No first-class "open chat by name" for Telegram (MED)
Driving Telegram meant: click search box (guessed coords) → type → click a
result row (guessed coords). A small **app recipe** library for Telegram
(`telegram.open_chat`, `telegram.send_to`, `telegram.new_channel`) would turn
10 brittle coordinate clicks into one call. The recipe engine already exists
(`recipes/`); ship a `telegram.*` pack.

## 4. Coordinate math leaked through the cross-process driver (MED)
Because the MCP server wasn't loaded this session, I imported Iris functions
directly and had to hand-add `window.x` offsets to OCR/UIA bounds (the MCP
layer normally does this). Expose a stable **Python API** (`iris.api`) with the
same coordinate handling the MCP tools get, so headless/script use doesn't
re-implement offset logic. Would also make Iris scriptable in cron jobs.

## 5. `click(target=...)` "click_outside_window" was right but opaque (LOW)
The refusal correctly caught a stale coordinate, but the error gave no
recovery hint. Include the resolved point, the window bounds, and a
"did you mean (cx,cy)?" suggestion so the caller can self-correct without a
separate `find`.

## 6. UIA invoke fell back to mouse silently on some buttons (LOW)
"Save"/"New Channel" reported `mouse_after_invoke_failed`. Worth logging
*why* invoke failed (no Invoke pattern? access denied?) so we can tell apps
that need real input from apps that just lack the pattern.

## 7. Deep-link / protocol-launch helper (LOW/NEW)
I opened a `tg://` link via `cmd start`. A first-class
`open_uri("tg://...")` / `open_uri("https://...")` tool (shell-exec a
protocol URL) would be a clean primitive for "hand a deep link to whatever
app owns the scheme" — useful well beyond Telegram.

## Meta
Biggest theme: Iris is excellent at *acting on a known target* but thin on
*verifying the world state before/after* in fast-changing apps (chat lists,
feeds). The verification primitives (active-window assert, role-aware find,
post-action title check) are the highest-leverage additions.
