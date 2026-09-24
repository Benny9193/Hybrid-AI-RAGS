"""Schema drift detection: compare the live catalog with what's indexed.

Run on a schedule (see README / deploy/). It never trusts the one-time
snapshot: every run re-introspects, diffs against the docs stored in the
index, and reports what went stale - optionally rebuilding the index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import TableDoc


@dataclass
class TableChange:
    name: str
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    retyped_columns: list[str] = field(default_factory=list)
    fk_changes: list[str] = field(default_factory=list)
    index_changes: list[str] = field(default_factory=list)
    tier_change: str | None = None

    @property
    def structural(self) -> bool:
        return bool(
            self.added_columns or self.removed_columns or self.retyped_columns
            or self.fk_changes or self.index_changes
        )


@dataclass
class DriftReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[TableChange] = field(default_factory=list)
    stale_annotations: list[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(
            self.added or self.removed or self.stale_annotations
            or any(c.structural for c in self.changed)
        )

    def to_markdown(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        out = [f"# EDS schema drift report - {ts}", ""]
        if not self.has_drift and not self.changed:
            out.append("No drift: the index matches the live schema.")
            return "\n".join(out)
        structural = [c for c in self.changed if c.structural]
        out.append(
            f"- {len(self.added)} new object(s), {len(self.removed)} removed, "
            f"{len(structural)} structurally changed, "
            f"{len(self.stale_annotations)} stale seed annotation(s)"
        )
        if self.added:
            out += ["", "## New objects (need a seed description)"] + [f"- {n}" for n in self.added]
        if self.removed:
            out += ["", "## Removed objects"] + [f"- {n}" for n in self.removed]
        if structural:
            out += ["", "## Changed objects"]
            for c in structural:
                out.append(f"### {c.name}")
                for label, items in (
                    ("Added columns", c.added_columns),
                    ("Removed columns", c.removed_columns),
                    ("Type changes", c.retyped_columns),
                    ("Foreign keys", c.fk_changes),
                    ("Indexes", c.index_changes),
                ):
                    if items:
                        out.append(f"- {label}: " + ", ".join(items))
        tiers = [c for c in self.changed if c.tier_change]
        if tiers:
            out += ["", "## Row-count tier changes (informational)"]
            out += [f"- {c.name}: {c.tier_change}" for c in tiers]
        if self.stale_annotations:
            out += ["", "## Stale seed annotations (fix the seed YAML)"]
            out += [f"- {s}" for s in self.stale_annotations]
        return "\n".join(out)


def _diff_sets(old: set[str], new: set[str]) -> list[str]:
    return [f"+{x}" for x in sorted(new - old)] + [f"-{x}" for x in sorted(old - new)]


def diff_docs(old: list[TableDoc], new: list[TableDoc]) -> DriftReport:
    """Compare two sets of *introspected* docs (domain / seed-only docs ignored)."""

    def real(docs: list[TableDoc]) -> dict[str, TableDoc]:
        return {
            d.full_name.lower(): d
            for d in docs
            if d.object_type != "domain" and d.source == "introspected"
        }

    o, n = real(old), real(new)
    report = DriftReport(
        added=sorted(n[k].full_name for k in n.keys() - o.keys()),
        removed=sorted(o[k].full_name for k in o.keys() - n.keys()),
    )
    for key in sorted(o.keys() & n.keys()):
        a, b = o[key], n[key]
        change = TableChange(b.full_name)
        if a.structure_signature() != b.structure_signature():
            ac = {c.name.lower(): c for c in a.columns}
            bc = {c.name.lower(): c for c in b.columns}
            change.added_columns = [bc[k].name for k in bc.keys() - ac.keys()]
            change.removed_columns = [ac[k].name for k in ac.keys() - bc.keys()]
            change.retyped_columns = [
                f"{bc[k].name} {ac[k].data_type} -> {bc[k].data_type}"
                for k in ac.keys() & bc.keys()
                if (ac[k].data_type, ac[k].nullable) != (bc[k].data_type, bc[k].nullable)
            ]
            fk = lambda d: {f.render() for f in d.foreign_keys if f.declared}  # noqa: E731
            change.fk_changes = _diff_sets(fk(a), fk(b))
            change.index_changes = _diff_sets(set(a.indexes), set(b.indexes))
        if a.tier != b.tier and "unknown" not in (a.tier, b.tier):
            change.tier_change = f"{a.tier} -> {b.tier} (~{b.row_count:,} rows)"
        if change.structural or change.tier_change:
            report.changed.append(change)
    return report


def docs_differ(old: list[TableDoc], new: list[TableDoc]) -> bool:
    """True if the stored docs are stale in *any* way (structure, seed text, row counts)."""

    def by_name(docs: list[TableDoc]) -> dict[str, dict]:
        return {d.full_name.lower(): d.to_dict() for d in docs}

    return by_name(old) != by_name(new)
