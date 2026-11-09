"""Start the Bridge team server from the source tree, before Bridge is installed:

    python <this file> start --name TEAM [...]

Puts services/agent-runtime/src and services/control-api/src first on sys.path and runs
`bridge_runtime.team` with the same arguments."""

import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _BIN]
sys.path[:0] = [str(_BIN.parent / "src"), str(_BIN.parents[1] / "control-api" / "src")]

from bridge_runtime.team import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
