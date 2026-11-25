"""Server configuration, read from a TOML file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_NAME = "coose.toml"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    stories_dir: Path
    secret_key_env: str
    api_key_env: str
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    cache_ttl: str = "1h"
    panel_host: str = "127.0.0.1"
    panel_port: int = 8001
    panel_password_env: str = "COOSE_PANEL_PASSWORD"

    @property
    def server_db_path(self) -> Path:
        return self.data_dir / "server.db"

    def panel_password(self) -> str | None:
        return os.environ.get(self.panel_password_env) or None

    def secret_key(self) -> str | None:
        return os.environ.get(self.secret_key_env) or None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load the config file. Missing file or keys fall back to defaults."""
        path = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME
        raw: dict = {}
        if path.exists():
            with path.open("rb") as f:
                raw = tomllib.load(f)
        base = path.resolve().parent
        server = raw.get("server", {})
        ai = raw.get("ai", {})
        panel = raw.get("panel", {})
        return cls(
            host=server.get("host", "127.0.0.1"),
            port=int(server.get("port", 8000)),
            data_dir=_resolve(base, server.get("data_dir", "data")),
            stories_dir=_resolve(base, server.get("stories_dir", "stories")),
            secret_key_env=server.get("secret_key_env", "COOSE_SECRET_KEY"),
            api_key_env=ai.get("api_key_env", "ANTHROPIC_API_KEY"),
            model=ai.get("model", "claude-opus-5-5"),
            effort=ai.get("effort", "medium"),
            cache_ttl=_cache_ttl(ai.get("cache_ttl", "1h")),
            panel_host=panel.get("host", "127.0.0.1"),
            panel_port=int(panel.get("port", 8001)),
            panel_password_env=panel.get("password_env", "COOSE_PANEL_PASSWORD"),
        )


def _cache_ttl(value) -> str:
    value = str(value).strip().lower()
    if value not in ("5m", "1h"):
        raise ValueError(f'cache_ttl in coose.toml must be "5m" or "1h", not "{value}"')
    return value


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()
