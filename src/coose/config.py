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

    @property
    def server_db_path(self) -> Path:
        return self.data_dir / "server.db"

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
        return cls(
            host=server.get("host", "127.0.0.1"),
            port=int(server.get("port", 8000)),
            data_dir=_resolve(base, server.get("data_dir", "data")),
            stories_dir=_resolve(base, server.get("stories_dir", "stories")),
            secret_key_env=server.get("secret_key_env", "COOSE_SECRET_KEY"),
            api_key_env=ai.get("api_key_env", "ANTHROPIC_API_KEY"),
        )


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()
