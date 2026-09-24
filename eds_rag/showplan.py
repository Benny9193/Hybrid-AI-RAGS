"""Summarise SQL Server SHOWPLAN_XML into something a model can act on."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = {"p": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}
SCAN_OPS = {"Table Scan", "Clustered Index Scan", "Index Scan"}


@dataclass
class Operator:
    physical_op: str
    logical_op: str
    est_rows: float
    subtree_cost: float
    obj: str = ""


@dataclass
class PlanSummary:
    statement_cost: float = 0.0
    est_rows: float = 0.0
    operators: list[Operator] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def scans(self) -> list[Operator]:
        return [o for o in self.operators if o.physical_op in SCAN_OPS]


def _clean(name: str | None) -> str:
    return (name or "").strip("[]")


def parse_showplan(xml: str) -> PlanSummary:
    root = ET.fromstring(xml)
    s = PlanSummary()
    stmt = root.find(".//p:StmtSimple", NS)
    if stmt is not None:
        s.statement_cost = float(stmt.get("StatementSubTreeCost", 0) or 0)
        s.est_rows = float(stmt.get("StatementEstRows", 0) or 0)

    for relop in root.iterfind(".//p:RelOp", NS):
        obj = ""
        # The Object element belongs to this operator's own child (e.g.
        # IndexScan), not to nested RelOps, so look only one level down.
        for child in relop:
            o = child.find("p:Object", NS)
            if o is not None:
                obj = ".".join(
                    p for p in (_clean(o.get("Schema")), _clean(o.get("Table"))) if p
                )
                if o.get("Index"):
                    obj += f" [{_clean(o.get('Index'))}]"
                break
        s.operators.append(
            Operator(
                physical_op=relop.get("PhysicalOp", ""),
                logical_op=relop.get("LogicalOp", ""),
                est_rows=float(relop.get("EstimateRows", 0) or 0),
                subtree_cost=float(relop.get("EstimatedTotalSubtreeCost", 0) or 0),
                obj=obj,
            )
        )

    for group in root.iterfind(".//p:MissingIndexGroup", NS):
        impact = group.get("Impact", "?")
        for mi in group.iterfind(".//p:MissingIndex", NS):
            table = ".".join(_clean(mi.get(k)) for k in ("Schema", "Table"))
            cols = {
                cg.get("Usage"): [_clean(c.get("Name")) for c in cg.iterfind("p:Column", NS)]
                for cg in mi.iterfind("p:ColumnGroup", NS)
            }
            keys = cols.get("EQUALITY", []) + cols.get("INEQUALITY", [])
            incl = cols.get("INCLUDE", [])
            desc = f"{table} ({', '.join(keys)})"
            if incl:
                desc += f" INCLUDE ({', '.join(incl)})"
            s.missing_indexes.append(f"{desc} - est. impact {impact}%")

    for conv in root.iterfind(".//p:PlanAffectingConvert", NS):
        s.warnings.append(f"implicit conversion affects {conv.get('ConvertIssue')}: {conv.get('Expression')}")
    for w in root.iterfind(".//p:Warnings", NS):
        for child in w:
            tag = child.tag.split("}")[-1]
            if tag not in ("PlanAffectingConvert",):
                s.warnings.append(tag)
    return s


def render_plan(s: PlanSummary, row_counts: dict[str, int] | None = None, top_n: int = 12) -> str:
    """``row_counts`` maps lower-case ``schema.table`` to table size for scan flags."""
    row_counts = row_counts or {}
    lines = [
        f"Estimated statement cost: {s.statement_cost:.3f}  |  estimated rows returned: {s.est_rows:,.0f}"
    ]
    risky = []
    for op in s.scans():
        table = op.obj.split(" [")[0].lower()
        size = row_counts.get(table)
        if size is not None and size >= 1_000_000:
            risky.append(f"{op.physical_op} on {op.obj} (~{size:,} rows)")
    if risky:
        lines.append("RISK - scans on large tables (add a selective, indexed predicate):")
        lines += [f"- {r}" for r in risky]
    lines.append("Most expensive operators:")
    for op in sorted(s.operators, key=lambda o: -o.subtree_cost)[:top_n]:
        target = f" on {op.obj}" if op.obj else ""
        lines.append(
            f"- {op.physical_op}{target}: est {op.est_rows:,.0f} rows, subtree cost {op.subtree_cost:.3f}"
        )
    if s.missing_indexes:
        lines.append("Optimizer missing-index hints (suggestions only - don't create on prod casually):")
        lines += [f"- {m}" for m in s.missing_indexes]
    if s.warnings:
        lines.append("Plan warnings:")
        lines += [f"- {w}" for w in dict.fromkeys(s.warnings)]
    return "\n".join(lines)
