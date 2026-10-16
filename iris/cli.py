"""Console-script entry points for the `iris-mcp` package.

`iris-mcp` -> serves the MCP server (stdio transport)
`iris-mcp-bench` -> runs the live accuracy bench (see iris.bench.runner)
`iris-mcp-doctor` -> environment diagnostics (see iris.doctor)
"""

from __future__ import annotations


def serve() -> int:
    """Entry point for `iris-mcp`. Imports the server module and runs main().

    This is a thin re-export so the pyproject [project.scripts] entry can
    target a function rather than a runpy invocation. The actual server lives
    in iris/__main__.py so `python -m iris` works identically.
    """
    from iris.__main__ import main

    main()
    return 0
