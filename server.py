"""Iris MCP server entry shim for source checkouts.

When running from a cloned repo (`python server.py`) this file is on
sys.path's first entry, so we can import the iris package directly. After
pip install, callers should use either `iris-mcp` (console script) or
`python -m iris`; both go through iris/__main__.py and skip this file.

Kept as a one-liner so changes to server logic happen in one place
(iris/__main__.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure the iris package next to this file is importable.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

try:
    from iris.__main__ import main
except ModuleNotFoundError as exc:  # pragma: no cover - environment problem, not logic
    # The most common way this server "fails to launch" is being started by an
    # interpreter that never received the dependencies: a bare "python" in an
    # MCP config resolves differently for GUI-launched Claude sessions than in
    # the shell where `pip install` ran (Windows Store stub, base conda, ...).
    # Say so plainly on stderr (stdout must stay clean for MCP stdio).
    sys.stderr.write(
        f"iris: cannot import {exc.name!r} with interpreter {sys.executable}.\n"
        "iris: this interpreter does not have Iris's dependencies. Register the MCP\n"
        "iris: server with the ABSOLUTE path of the python that ran `pip install -r\n"
        "iris: requirements.txt`, e.g.\n"
        f"iris:   claude mcp add --scope user iris -- <that python.exe> {_HERE / 'server.py'}\n"
    )
    raise

if __name__ == "__main__":
    main()
