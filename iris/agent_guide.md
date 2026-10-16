# Iris: desktop control for Windows

Iris sees and drives any Windows app. It is powerful but slower and more
fragile than a real API, and it acts on the user's live desktop. Use it last.

## Order of preference

1. A documented API, CLI, or file on disk
2. An app-native control surface (MIDI, OSC, a plugin API)
3. Only then Iris

## Working well

- Call `screen_info` before any coordinate work. Monitor count, layout, and
  DPI are discovered live; never assume them.
- Act by number, not coordinates: `see(token, marks=True)`, then
  `click(token, mark=N)`.
- Text entry: prefer `do` with `{"action": "set", ...}` (quiet UIA, no focus
  steal) over click + type.
- Batch known sequences in one `do(steps=[...])` call.
- Screenshots are downscaled. Convert with
  `screen = meta.origin + image_xy / meta.scale`; never click raw image
  coordinates. Use `zoom` for small targets.
- Clicks are quiet by default (no cursor or focus change). Games and
  raw-input apps need `click_method="mouse"`.
- A click went wrong? Read `events_tail` before retrying.
- Anything that raises or focuses windows interrupts the user. Ask before
  long or repeated runs.
