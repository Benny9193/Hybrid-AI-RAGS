"""Core data model: one ``TableDoc`` per table/view (or per domain overview).

A ``TableDoc`` is the unit that gets embedded, keyword-indexed, diffed for
drift, and rendered back to the model by the MCP tools.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Row-count tiers. Anything at or above HIGH_VOLUME_ROWS is treated as a
# "Tier 1" table: always filter, always TOP, never scan casually.
HIGH_VOLUME_ROWS = 10_000_000
_TIERS = [
    (10_000, "tiny"),
    (1_000_000, "small"),
    (HIGH_VOLUME_ROWS, "medium"),
    (100_000_000, "large"),
]


def row_tier(row_count: int | None) -> str:
    if row_count is None:
        return "unknown"
    for limit, name in _TIERS:
        if row_count < limit:
            return name
    return "huge"


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True
    is_pk: bool = False

    def render(self) -> str:
        parts = [self.name, self.data_type]
        if self.is_pk:
            parts.append("PK")
        if not self.nullable and not self.is_pk:
            parts.append("NOT NULL")
        return " ".join(parts)


@dataclass
class ForeignKey:
    column: str
    ref_table: str  # schema-qualified, e.g. "dbo.Vendors"
    ref_column: str
    # False for relationships that come from seed annotations / naming
    # convention rather than a declared FK constraint.
    declared: bool = True

    def render(self) -> str:
        suffix = "" if self.declared else " (not a declared FK - verify)"
        return f"{self.column} -> {self.ref_table}.{self.ref_column}{suffix}"


@dataclass
class TableDoc:
    schema: str
    name: str
    object_type: str = "table"  # table | view | domain
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    row_count: int | None = None
    description: str = ""
    domain: str = ""
    aliases: list[str] = field(default_factory=list)
    gotchas: list[str] = field(default_factory=list)
    # "introspected" (came from the live catalog) or "seed" (annotation only).
    source: str = "introspected"

    # ------------------------------------------------------------------ ids
    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def tier(self) -> str:
        return row_tier(self.row_count)

    @property
    def high_volume(self) -> bool:
        return self.row_count is not None and self.row_count >= HIGH_VOLUME_ROWS

    def column(self, name: str) -> Column | None:
        lname = name.lower()
        return next((c for c in self.columns if c.name.lower() == lname), None)

    # ---------------------------------------------------------------- drift
    def structure_signature(self) -> str:
        """Hash of the *structural* facts that can drift in the database.

        Row counts are deliberately excluded (they change constantly); tier
        changes are reported separately by the drift report.
        """
        payload = {
            "type": self.object_type,
            "columns": [(c.name, c.data_type, c.nullable, c.is_pk) for c in self.columns],
            "fks": sorted(
                (f.column, f.ref_table, f.ref_column) for f in self.foreign_keys if f.declared
            ),
            "indexes": sorted(self.indexes),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    # ------------------------------------------------------------- rendering
    def rows_label(self) -> str:
        if self.row_count is None:
            return "row count unknown"
        label = f"~{self.row_count:,} rows ({self.tier})"
        if self.high_volume:
            label += " - HIGH VOLUME: always filter + TOP"
        return label

    def render(self, max_columns: int | None = 60, include_indexes: bool = False) -> str:
        """Markdown chunk returned to the model."""
        if self.object_type == "domain":
            lines = [f"## Domain overview: {self.name}", self.description]
            if self.gotchas:
                lines.append("Gotchas:")
                lines += [f"- {g}" for g in self.gotchas]
            return "\n".join(lines)

        lines = [f"## {self.full_name} ({self.object_type})"]
        meta = [self.rows_label()]
        if self.domain:
            meta.insert(0, f"domain: {self.domain}")
        lines.append(" | ".join(meta))
        if self.aliases:
            lines.append("Also known as: " + ", ".join(self.aliases))
        if self.description:
            lines.append(self.description)
        if self.columns:
            cols = self.columns if max_columns is None else self.columns[:max_columns]
            lines.append("Columns: " + "; ".join(c.render() for c in cols))
            if max_columns is not None and len(self.columns) > max_columns:
                lines.append(
                    f"(+{len(self.columns) - max_columns} more columns - call get_table_detail)"
                )
        elif self.source == "seed":
            lines.append("Columns: not introspected yet - call get_table_detail to confirm.")
        if self.foreign_keys:
            lines.append("Joins: " + "; ".join(f.render() for f in self.foreign_keys))
        if include_indexes and self.indexes:
            lines.append("Indexes:")
            lines += [f"- {i}" for i in self.indexes]
        if self.gotchas:
            lines.append("Gotchas:")
            lines += [f"- {g}" for g in self.gotchas]
        return "\n".join(lines)

    # ------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableDoc":
        d = dict(d)
        d["columns"] = [Column(**c) for c in d.get("columns", [])]
        d["foreign_keys"] = [ForeignKey(**f) for f in d.get("foreign_keys", [])]
        return cls(**d)
