"""Bridge MCP: an MCP stdio server that gives a Claude Code session a seat on the team
board. Standard library and httpx only; it never imports bridge_control."""


def __getattr__(name: str) -> str:
    """`__version__` is looked up on first use: importlib.metadata costs ~0.1 s, which every
    Claude Code hook call would otherwise pay."""
    if name != "__version__":
        raise AttributeError(name)
    from importlib import metadata

    try:
        version = metadata.version("bridge-runtime")
    except metadata.PackageNotFoundError:  # running from the source tree
        version = "0.0.1"
    globals()["__version__"] = version
    return version
