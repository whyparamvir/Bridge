"""Environment configuration. Fail-safe defaults: an unset BRIDGE_ENV means production,
production refuses dev auth and SQLite, and live provider calls are off unless enabled.
See services/control-api/docs/ENVIRONMENT.md."""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ENVIRONMENTS = {"production", "staging", "development", "test", "local"}
DEV_ENVIRONMENTS = {"development", "test"}


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fd00::/8")]


class ConfigError(RuntimeError):
    pass


def default_data_dir(environ: dict[str, str] | None = None) -> Path:
    e = os.environ if environ is None else environ
    home = e.get("BRIDGE_HOME", "").strip()
    if home:
        return Path(home)
    if sys.platform == "win32":
        base = e.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Bridge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Bridge"
    xdg = e.get("XDG_DATA_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "bridge"


def is_private_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or any(ip in n for n in _PRIVATE_NETS if ip.version == n.version)


def _flag(env: dict[str, str], name: str) -> bool:
    v = env.get(name, "").strip().lower()
    if v in ("", "0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be a boolean (got {v!r})")


@dataclass(frozen=True)
class Settings:
    env: str = "production"
    database_url: str = ""
    dev_auth: bool = False
    artifact_dir: Path = field(default_factory=lambda: Path(".bridge-dev/artifacts"))
    event_retention_hours: int = 168
    session_ttl_hours: int = 12
    live_provider_calls: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    data_dir: Path = field(default_factory=default_data_dir)
    bind_host: str = "127.0.0.1"
    private_network: bool = False

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @property
    def is_dev(self) -> bool:
        return self.env in DEV_ENVIRONMENTS

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def validate(self) -> Settings:
        if self.env not in ENVIRONMENTS:
            raise ConfigError(f"BRIDGE_ENV must be one of {sorted(ENVIRONMENTS)}")
        if self.dev_auth and not self.is_dev:
            raise ConfigError(
                "BRIDGE_DEV_AUTH=1 is only allowed when BRIDGE_ENV is development or test; "
                f"refusing to start with BRIDGE_ENV={self.env}"
            )
        if not self.database_url:
            raise ConfigError("BRIDGE_DATABASE_URL is required")
        if self.is_sqlite and not (self.is_dev or self.is_local):
            raise ConfigError("SQLite is a development fallback only; use PostgreSQL")
        if self.is_local and self.bind_host.strip().lower() not in LOOPBACK_HOSTS:
            url = urlparse(self.public_base_url)
            private_ok = self.private_network and is_private_host(url.hostname or "")
            if url.scheme != "https" and not private_ok:
                raise ConfigError(
                    f"BRIDGE_BIND_HOST={self.bind_host} is reachable from the network: set "
                    "BRIDGE_PUBLIC_BASE_URL to an https URL, or set BRIDGE_PRIVATE_NETWORK=1 "
                    "with a private/Tailscale address"
                )
        return self

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if environ is None else environ)
        env = e.get("BRIDGE_ENV", "production").strip().lower()
        local = env == "local"
        data_dir = default_data_dir(e)
        if env in DEV_ENVIRONMENTS:
            default_db = "sqlite:///.bridge-dev/bridge.db"
        elif local:
            default_db = f"sqlite:///{data_dir.as_posix()}/bridge.db"
        else:
            default_db = ""
        default_art = str(data_dir / "files") if local else ".bridge-dev/artifacts"
        return cls(
            env=env,
            database_url=e.get("BRIDGE_DATABASE_URL", default_db),
            dev_auth=_flag(e, "BRIDGE_DEV_AUTH"),
            artifact_dir=Path(e.get("BRIDGE_ARTIFACT_DIR", default_art)),
            event_retention_hours=int(e.get("BRIDGE_EVENT_RETENTION_HOURS", "168")),
            session_ttl_hours=int(e.get("BRIDGE_SESSION_TTL_HOURS", "720" if local else "12")),
            live_provider_calls=_flag(e, "BRIDGE_LIVE_PROVIDER_CALLS"),  # off unless set, local too
            public_base_url=e.get("BRIDGE_PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
            data_dir=data_dir,
            bind_host=e.get("BRIDGE_BIND_HOST", "127.0.0.1").strip(),
            private_network=_flag(e, "BRIDGE_PRIVATE_NETWORK"),
        ).validate()
