"""Seed annotations: load the curated YAML and merge it onto introspected docs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import ForeignKey, TableDoc

# Shipped inside the package (see pyproject package-data) so wheels include it.
DEFAULT_SEED = Path(__file__).resolve().parent / "data" / "eds_seed.yaml"


@dataclass
class Seed:
    rules: dict[str, Any] = field(default_factory=dict)
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Seed":
        p = Path(path) if path else DEFAULT_SEED
        if not p.exists():
            # Fail loudly: an empty seed silently yields a useless index.
            raise FileNotFoundError(f"seed annotations file not found: {p}")
        raw = yaml.safe_load(p.read_text()) or {}
        return cls(
            rules=raw.get("rules") or {},
            domains=raw.get("domains") or {},
            tables={k: v or {} for k, v in (raw.get("tables") or {}).items()},
        )


@dataclass
class MergeResult:
    docs: list[TableDoc]
    # Human-readable problems with the seed itself, e.g. an annotated table
    # that no longer exists. Surfaced by `eds-rag refresh`.
    stale_annotations: list[str]


def _parse_ref(ref: str) -> tuple[str, str]:
    """``dbo.Vendors.VendorId`` -> (``dbo.Vendors``, ``VendorId``)."""
    table, _, column = ref.rpartition(".")
    return table, column


def _apply_rules(doc: TableDoc, rules: dict[str, Any]) -> None:
    gotchas: list[str] = []
    for col, text in (rules.get("column_gotchas") or {}).items():
        if doc.column(col):
            gotchas.append(text.strip())
    flag_cols = [c for c in rules.get("active_flag_columns") or [] if doc.column(c)]
    if flag_cols and rules.get("active_flag_gotcha"):
        gotchas.append(rules["active_flag_gotcha"].strip().format(column=doc.column(flag_cols[0]).name))
    schema_note = (rules.get("schema_gotchas") or {}).get(doc.schema.lower())
    if schema_note:
        gotchas.append(schema_note.strip())
    if doc.high_volume and rules.get("high_volume_gotcha"):
        gotchas.append(rules["high_volume_gotcha"].strip())
    # Rules go after hand-written gotchas; avoid duplicates on re-merge.
    doc.gotchas += [g for g in gotchas if g not in doc.gotchas]


def _annotate(doc: TableDoc, ann: dict[str, Any], stale: list[str], *, strict_joins: bool) -> None:
    doc.description = (ann.get("description") or doc.description or "").strip()
    doc.domain = ann.get("domain") or doc.domain
    doc.aliases = list(dict.fromkeys([*doc.aliases, *(ann.get("aliases") or [])]))
    doc.gotchas = [*(g.strip() for g in ann.get("gotchas") or []), *doc.gotchas]
    if doc.row_count is None and ann.get("approx_rows"):
        doc.row_count = int(ann["approx_rows"])

    declared = {f.column.lower() for f in doc.foreign_keys}
    for j in ann.get("expected_joins") or []:
        col = j["column"]
        if col.lower() in declared:
            continue  # a real FK already covers it
        if strict_joins and not doc.column(col):
            stale.append(
                f"{doc.full_name}: expected_joins column '{col}' does not exist in the live schema"
            )
            continue
        ref_table, ref_col = _parse_ref(j["ref"])
        doc.foreign_keys.append(ForeignKey(col, ref_table, ref_col, declared=False))


def merge(introspected: list[TableDoc], seed: Seed) -> MergeResult:
    """Overlay seed annotations on introspected docs.

    If ``introspected`` is empty we run in *seed-only* mode: every annotated
    table becomes a doc of its own (no columns), which is enough to make
    ``search_schema`` useful before anyone has pointed this at a database.
    """
    stale: list[str] = []
    seed_only = not introspected
    by_name = {d.full_name.lower(): d for d in introspected}

    docs = list(introspected)
    for key, ann in seed.tables.items():
        doc = by_name.get(key.lower())
        if doc is None:
            if not seed_only:
                stale.append(f"{key}: annotated in seed but not found in the live schema")
                continue
            schema, _, name = key.partition(".")
            doc = TableDoc(schema=schema, name=name, source="seed")
            docs.append(doc)
        _annotate(doc, ann, stale, strict_joins=not seed_only)

    for doc in docs:
        _apply_rules(doc, seed.rules)

    for name, d in seed.domains.items():
        docs.append(
            TableDoc(
                schema="domain",
                name=name,
                object_type="domain",
                description=(d.get("description") or "").strip(),
                aliases=list(d.get("aliases") or []),
                domain=name,
                source="seed",
            )
        )
    return MergeResult(docs=docs, stale_annotations=stale)

