"""Environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


@dataclass
class Settings:
    index_path: str = "data/eds_schema.db"
    seed_path: str | None = None
    conn_str: str | None = None  # ODBC connection string for the READ-ONLY login
    embedder: str = "hashing"
    max_rows: int = 1000
    timeout_s: int = 10
    sample_rows: int = 5
    audit_log: str | None = None
    # Escape hatch for local dev against a throwaway DB. Never set in prod.
    allow_writable_login: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            index_path=os.environ.get("EDS_RAG_INDEX", cls.index_path),
            seed_path=os.environ.get("EDS_RAG_SEED") or None,
            conn_str=os.environ.get("EDS_SQL_CONN") or None,
            embedder=os.environ.get("EDS_RAG_EMBEDDER", cls.embedder),
            max_rows=_int("EDS_MAX_ROWS", cls.max_rows),
            timeout_s=_int("EDS_QUERY_TIMEOUT", cls.timeout_s),
            sample_rows=_int("EDS_SAMPLE_ROWS", cls.sample_rows),
            audit_log=os.environ.get("EDS_AUDIT_LOG") or None,
            allow_writable_login=os.environ.get("EDS_ALLOW_WRITABLE_LOGIN") == "1",
        )
