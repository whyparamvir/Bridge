"""Bridge MCP: an MCP stdio server that gives a Claude Code session a seat on the team
board. Standard library and httpx only; it never imports bridge_control."""

from importlib import metadata

try:
    __version__ = metadata.version("bridge-runtime")
except metadata.PackageNotFoundError:  # running from the source tree
    __version__ = "0.1.0"
