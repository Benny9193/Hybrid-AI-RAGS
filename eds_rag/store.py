"""SQLite-backed schema index: document rows, embeddings and an FTS5 index.

At EDS scale (~900 tables + views) a brute-force cosine over an in-memory
numpy matrix takes well under a millisecond, so we store vectors as BLOBs
rather than requiring the sqlite-vec extension. The ``SchemaStore`` API is
the seam to swap in sqlite-vec / pgvector if the corpus grows.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .embeddings import Embedder
from .models import TableDoc
from .text import expand_identifiers, tokens

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    object_type TEXT NOT NULL,
    signature TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    names, aliases, description, columns, gotchas,
    tokenize = 'porter unicode61'
);
"""

# BM25 column weights: names and aliases matter most.
FTS_WEIGHTS = (10.0, 6.0, 3.0, 2.0, 1.0)


def embed_text(doc: TableDoc) -> str:
    """Text that represents a doc in vector space (names + meaning, no types)."""
    parts = [doc.full_name, doc.name, doc.object_type, doc.domain, " ".join(doc.aliases), doc.description]
    if doc.columns:
        parts.append("columns: " + " ".join(c.name for c in doc.columns[:120]))
    if doc.foreign_keys:
        parts.append("joins " + " ".join(f.ref_table for f in doc.foreign_keys))
    return expand_identifiers("\n".join(p for p in parts if p))


def _fts_fields(doc: TableDoc) -> tuple[str, ...]:
    return (
        expand_identifiers(f"{doc.full_name} {doc.name}"),
        expand_identifiers(" ".join(doc.aliases) + " " + doc.domain),
        doc.description,
        expand_identifiers(" ".join(c.name for c in doc.columns)),
        " ".join(doc.gotchas),
    )


def _text_hash(text: str, embedder_name: str) -> str:
    return hashlib.sha256(f"{embedder_name}\n{text}".encode()).hexdigest()[:20]


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 OR-query of quoted terms."""
    terms = list(dict.fromkeys(t for t in tokens(text) if re.fullmatch(r"\w+", t)))
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


class SchemaStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA_DDL)
        self._matrix: np.ndarray | None = None
        self._ids: list[int] = []

    # ---------------------------------------------------------------- meta
    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # --------------------------------------------------------------- build
    def rebuild(self, docs: list[TableDoc], embedder: Embedder) -> dict[str, int]:
        """Replace the index contents with ``docs``.

        Embeddings are reused for docs whose embed text is unchanged, so a
        weekly refresh against a paid embedding API only pays for drift.
        """
        cached: dict[str, bytes] = dict(
            self.conn.execute("SELECT text_hash, embedding FROM docs").fetchall()
        )
        texts = [embed_text(d) for d in docs]
        hashes = [_text_hash(t, embedder.name) for t in texts]
        todo = [i for i, h in enumerate(hashes) if h not in cached]
        fresh = embedder.embed_documents([texts[i] for i in todo]) if todo else None
        vectors: dict[int, bytes] = {i: cached[h] for i, h in enumerate(hashes) if h in cached}
        for j, i in enumerate(todo):
            vectors[i] = fresh[j].astype(np.float32).tobytes()

        with self.conn:
            self.conn.execute("DELETE FROM docs")
            self.conn.execute("DELETE FROM docs_fts")
            for i, doc in enumerate(docs):
                cur = self.conn.execute(
                    "INSERT INTO docs (full_name, object_type, signature, text_hash, doc_json, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        doc.full_name,
                        doc.object_type,
                        doc.structure_signature(),
                        hashes[i],
                        json.dumps(doc.to_dict()),
                        vectors[i],
                    ),
                )
                self.conn.execute(
                    "INSERT INTO docs_fts (rowid, names, aliases, description, columns, gotchas)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (cur.lastrowid, *_fts_fields(doc)),
                )
            self._set_meta("embedder", embedder.name)
            self._set_meta("built_at", datetime.now(timezone.utc).isoformat())
            self._set_meta("doc_count", str(len(docs)))
            # Row ids are reassigned on every rebuild; readers holding caches
            # (a long-running MCP server) compare this to know they're stale.
            self._set_meta("generation", str(int(self.meta("generation") or 0) + 1))
        self._matrix = None
        return {"docs": len(docs), "embedded": len(todo), "reused": len(docs) - len(todo)}

    # --------------------------------------------------------------- reads
    def all_docs(self) -> list[TableDoc]:
        rows = self.conn.execute("SELECT doc_json FROM docs ORDER BY id").fetchall()
        return [TableDoc.from_dict(json.loads(r[0])) for r in rows]

    def get(self, doc_id: int) -> TableDoc:
        row = self.conn.execute("SELECT doc_json FROM docs WHERE id = ?", (doc_id,)).fetchone()
        return TableDoc.from_dict(json.loads(row[0]))

    def names(self) -> dict[str, int]:
        return {n: i for i, n in self.conn.execute("SELECT id, full_name FROM docs")}

    def _load_matrix(self) -> None:
        rows = self.conn.execute("SELECT id, embedding FROM docs ORDER BY id").fetchall()
        self._ids = [r[0] for r in rows]
        self._matrix = (
            np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else None
        )

    def vector_search(self, query_vec: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if self._matrix is None:
            self._load_matrix()
        if self._matrix is None:
            return []
        scores = self._matrix @ query_vec.astype(np.float32)
        top = np.argsort(-scores)[:limit]
        return [(self._ids[i], float(scores[i])) for i in top]

    def keyword_search(self, text: str, limit: int) -> list[tuple[int, float]]:
        q = fts_query(text)
        if not q:
            return []
        w = ", ".join(str(x) for x in FTS_WEIGHTS)
        rows = self.conn.execute(
            f"SELECT rowid, bm25(docs_fts, {w}) AS s FROM docs_fts WHERE docs_fts MATCH ? "
            "ORDER BY s LIMIT ?",
            (q, limit),
        ).fetchall()
        return [(r[0], -float(r[1])) for r in rows]

    def generation(self) -> int:
        return int(self.meta("generation") or 0)

    def invalidate(self) -> None:
        """Drop the in-memory vector matrix so the next search reloads it."""
        self._matrix = None

    def close(self) -> None:
        self.conn.close()
