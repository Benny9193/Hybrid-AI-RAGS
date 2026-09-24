"""Hybrid retrieval over the schema index.

Three rankers are fused with Reciprocal Rank Fusion:

1. vector similarity (embedder of choice),
2. BM25 keyword match (SQLite FTS5, identifiers split into words),
3. exact table-name hits (the user typed ``CrossRefs`` - give them CrossRefs).

Then the result set is post-processed: archive twins are collapsed into
their dbo table (unless the question is about history) and FK neighbours of
the top hits are appended so the model sees the join targets it will need.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from sqlglot import exp

from .embeddings import Embedder
from .models import TableDoc
from .store import SchemaStore

HISTORY_WORDS = re.compile(r"\b(archive[ds]?|histor(y|ical)|old|prior years?|purged)\b", re.I)
# Catalog views the read-only login can always query to confirm the schema.
# They aren't in the index, so they're exempt from the unknown-table check.
CATALOG_SCHEMAS = frozenset({"sys", "information_schema"})


@dataclass
class SearchHit:
    doc: TableDoc
    score: float
    related: bool = False  # pulled in as an FK neighbour, not a direct match
    notes: list[str] = field(default_factory=list)


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


class SchemaRetriever:
    def __init__(self, store: SchemaStore, embedder: Embedder, candidates: int = 50):
        self.store = store
        self.embedder = embedder
        self.candidates = candidates
        self._generation: int | None = None
        self._ensure_fresh()

    def _ensure_fresh(self) -> None:
        """Reload caches if the index was rebuilt (e.g. by the refresh cron)."""
        generation = self.store.generation()
        if generation == self._generation:
            return
        built_with = self.store.meta("embedder")
        if built_with and built_with != self.embedder.name:
            raise ValueError(
                f"index was built with embedder {built_with!r} but {self.embedder.name!r} was "
                "requested - rebuild the index or set EDS_RAG_EMBEDDER to match"
            )
        self.store.invalidate()
        self._refresh_cache()
        self._generation = generation

    def _refresh_cache(self) -> None:
        self.docs: dict[int, TableDoc] = {}
        for doc_id in self.store.names().values():
            self.docs[doc_id] = self.store.get(doc_id)
        self._by_full = {d.full_name.lower(): i for i, d in self.docs.items()}
        self._by_short: dict[str, list[int]] = {}
        for i, d in self.docs.items():
            if d.object_type != "domain":
                self._by_short.setdefault(d.name.lower(), []).append(i)

    # ------------------------------------------------------------ lookups
    def resolve(self, name: str) -> TableDoc | None:
        """Resolve ``Vendors`` / ``[dbo].[Vendors]`` / ``EDS.dbo.Vendors``."""
        self._ensure_fresh()
        parts = [p.strip("[]\" ") for p in name.strip().split(".") if p.strip()]
        if not parts:
            return None
        if len(parts) >= 2:
            i = self._by_full.get(f"{parts[-2]}.{parts[-1]}".lower())
            return self.docs[i] if i is not None else None
        ids = self._by_short.get(parts[0].lower(), [])
        if not ids:
            return None
        # Unqualified names resolve the way SQL Server does for most logins: dbo.
        ids.sort(key=lambda i: (self.docs[i].schema.lower() != "dbo", self.docs[i].full_name))
        return self.docs[ids[0]]

    def suggest(self, name: str, n: int = 5) -> list[str]:
        short = name.split(".")[-1].strip("[]").lower()
        pool = {d.name.lower(): d.full_name for d in self.docs.values() if d.object_type != "domain"}
        close = difflib.get_close_matches(short, list(pool), n=n, cutoff=0.6)
        out = [pool[c] for c in close]
        if len(out) < n:
            out += [h.doc.full_name for h in self.search(name, top_k=n, expand_related=False)
                    if h.doc.object_type != "domain" and h.doc.full_name not in out]
        return out[:n]

    def check_tables(self, tables: list[exp.Table]) -> tuple[list[TableDoc], dict[str, list[str]]]:
        """Split a query's table references into known docs and unknown names.

        Unknown names come back with suggestions: this is the "second pass"
        that stops the model from guessing at tables retrieval didn't return.
        """
        known: list[TableDoc] = []
        unknown: dict[str, list[str]] = {}
        for t in tables:
            if t.catalog:
                # Never map OtherDb.dbo.X onto the indexed dbo.X.
                unknown[t.sql("tsql")] = []
                continue
            if t.db.lower() in CATALOG_SCHEMAS:
                continue
            qualified = ".".join(p for p in (t.db, t.name) if p)
            doc = self.resolve(qualified)
            if doc:
                if doc not in known:
                    known.append(doc)
            else:
                unknown[qualified] = self.suggest(qualified)
        return known, unknown

    # ------------------------------------------------------------- search
    def search(
        self,
        query: str,
        top_k: int = 8,
        expand_related: bool = True,
        max_related: int = 3,
    ) -> list[SearchHit]:
        self._ensure_fresh()
        vec = self.store.vector_search(self.embedder.embed_query(query), self.candidates)
        kw = self.store.keyword_search(query, self.candidates)
        exact = [
            i
            for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", query)
            for i in self._exact_ids(word)
        ] + self._alias_ids(query)
        fused = rrf([[i for i, _ in vec], [i for i, _ in kw], list(dict.fromkeys(exact))])
        ranked = sorted(fused, key=lambda i: -fused[i])

        want_history = bool(HISTORY_WORDS.search(query))
        hits: list[SearchHit] = []
        seen: set[int] = set()
        for doc_id in ranked:
            doc = self.docs[doc_id]
            # Fold archive.X into dbo.X rather than spending a result slot on it.
            if not want_history and self._twin(doc, "archive", "dbo") is not None:
                continue
            hits.append(SearchHit(doc, fused[doc_id]))
            seen.add(doc_id)
            if len(hits) >= top_k:
                break
        for h in hits:
            twin = self._twin(h.doc, "dbo", "archive")
            if twin is not None:
                h.notes.append(
                    f"History lives in {self.docs[twin].full_name} (no indexes - filter tightly)."
                )

        if expand_related:
            hits += self._neighbours(hits[:3], seen, max_related)
        return hits

    def _exact_ids(self, word: str) -> list[int]:
        doc = self.resolve(word) if "." in word else None
        if doc:
            return [self._by_full[doc.full_name.lower()]]
        return list(self._by_short.get(word.lower(), []))

    def _alias_ids(self, query: str) -> list[int]:
        """Docs whose alias phrase appears verbatim in the query, longest first."""
        q = f" {' '.join(re.findall(r'[a-z0-9]+', query.lower()))} "
        hits = [
            (len(a), i)
            for i, d in self.docs.items()
            for a in d.aliases
            if len(a) > 3 and f" {' '.join(re.findall(r'[a-z0-9]+', a.lower()))} " in q
        ]
        return [i for _, i in sorted(hits, key=lambda h: -h[0])]

    def _twin(self, doc: TableDoc, from_schema: str, to_schema: str) -> int | None:
        if doc.schema.lower() != from_schema:
            return None
        return self._by_full.get(f"{to_schema}.{doc.name}".lower())

    def _neighbours(self, top: list[SearchHit], seen: set[int], limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for h in top:
            for fk in h.doc.foreign_keys:
                i = self._by_full.get(fk.ref_table.lower())
                if i is None or i in seen:
                    continue
                seen.add(i)
                out.append(SearchHit(self.docs[i], 0.0, related=True,
                                     notes=[f"Join target of {h.doc.full_name}.{fk.column}"]))
                if len(out) >= limit:
                    return out
        return out


def render_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "No matching tables. Try different wording or get_table_detail with a guessed name."
    direct = [h for h in hits if not h.related]
    related = [h for h in hits if h.related]
    parts = []
    for h in direct:
        body = h.doc.render()
        if h.notes:
            body += "\n" + "\n".join(f"Note: {n}" for n in h.notes)
        parts.append(body)
    if related:
        lines = ["## Related join targets (summary)"]
        for h in related:
            d = h.doc
            cols = ", ".join(c.name for c in d.columns[:12]) or "columns not introspected"
            lines.append(f"- {d.full_name} ({d.rows_label()}): {d.description or cols} "
                         f"[{'; '.join(h.notes)}]")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
