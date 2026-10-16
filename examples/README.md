# Iris examples

Standalone scripts you can run to see Iris in action. None of them require
the MCP layer — they call Iris's Python API directly so you can read,
copy, and adapt.

```powershell
python examples/01_hello_iris.py
python examples/02_clipboard_pipeline.py
python examples/03_open_url_in_chrome.py
python examples/04_find_and_click_real_app.py
```

## Index

| # | File | What it shows |
|---|------|---------------|
| 1 | `01_hello_iris.py` | Status check + monitor topology. The 30-second tour. |
| 2 | `02_clipboard_pipeline.py` | Read selected text -> transform -> paste back. |
| 3 | `03_open_url_in_chrome.py` | Recipe-driven: focus Chrome, ctrl+L, type URL, Enter. |
| 4 | `04_find_and_click_real_app.py` | Spawn Notepad, find a menu item via UIA, invoke without mouse motion. |

Most examples are short enough that the full pattern is in the file itself
with comments. Browse them in numbered order if you want a tutorial.
