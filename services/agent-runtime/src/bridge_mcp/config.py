"""Where the bridge finds its server: ~/.bridge/bridge.json (or BRIDGE_CONFIG), with the
BRIDGE_URL, BRIDGE_TOKEN, BRIDGE_PROJECT and BRIDGE_SEAT_LABEL environment variables winning."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BridgeConfig:
    url: str
    token: str = field(repr=False)  # an agent credential (ha_); never printed
    project_id: str
    person_name: str = ""
    seat_label: str = ""
    agent_id: str = ""  # saved at join so the hook knows which events are mine

    def missing(self) -> list[str]:
        return [k for k in ("url", "token", "project_id") if not getattr(self, k)]


def default_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("BRIDGE_CONFIG") or Path.home() / ".bridge" / "bridge.json")


FILE_WINS = ("BRIDGE_URL", "BRIDGE_TOKEN", "BRIDGE_PROJECT")


def explicit_env(env: Mapping[str, str] | None, path: str | os.PathLike) -> dict[str, str]:
    """The environment for a config file named on the command line: that file, with the
    BRIDGE_URL/BRIDGE_TOKEN/BRIDGE_PROJECT overrides dropped so the file wins."""
    base = os.environ if env is None else env
    return {k: v for k, v in base.items() if k not in FILE_WINS} | {"BRIDGE_CONFIG": str(path)}


def load(env: Mapping[str, str] | None = None) -> BridgeConfig:
    env = os.environ if env is None else env
    path = default_path(env)
    data: dict = {}
    if path.is_file():
        data = json.loads(path.read_text("utf-8") or "{}")

    def pick(var: str, key: str) -> str:
        return str(env.get(var) or data.get(key) or "").strip()

    return BridgeConfig(
        url=pick("BRIDGE_URL", "url").rstrip("/"),
        token=pick("BRIDGE_TOKEN", "token"),
        project_id=pick("BRIDGE_PROJECT", "project_id"),
        person_name=str(data.get("person_name") or "").strip(),
        seat_label=pick("BRIDGE_SEAT_LABEL", "seat_label") or Path.cwd().name or "session",
        agent_id=str(data.get("agent_id") or "").strip(),
    )


def save(cfg: BridgeConfig, path: str | os.PathLike) -> None:
    """Atomic write, readable only by the user where the OS supports file modes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {k: v for k, v in asdict(cfg).items() if v}
    # mkstemp creates a new file (O_EXCL) readable only by the user; a leftover temp
    # file with wider permissions is never reused.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
