"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str
    title: str = "Latency SLO Aggregator"
    version: str = "1.0.0"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=os.environ.get("SLO_DB_PATH", "/data/slo.db"),
        )


settings = Settings.from_env()
