"""Run Bridge MCP from the source tree, before Bridge is installed:

    python <this file> serve | join <invite-url> --name NAME | add-agent LABEL | hook

Puts services/agent-runtime/src first on sys.path and runs `bridge_mcp` with the same
arguments. It prints nothing of its own (stdout belongs to the MCP protocol)."""

import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
# This folder is sys.path[0] and this file is named bridge_mcp.py: drop it so the import
# below finds the package, not this script.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _BIN]
sys.path.insert(0, str(_BIN.parent / "src"))

from bridge_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
