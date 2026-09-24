"""Pull the live schema from SQL Server catalog views into ``TableDoc``s.

Uses ``sys.*`` catalog views (not just INFORMATION_SCHEMA) because we also
want PK flags, row counts, indexes and MS_Description extended properties.
Everything here is read-only and works under a ``db_datareader`` login:
metadata visibility covers objects the login can SELECT from.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import Column, ForeignKey, TableDoc

OBJECTS_SQL = """
SELECT o.object_id, s.name AS schema_name, o.name, o.type
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
"""

COLUMNS_SQL = """
SELECT c.object_id, c.name, t.name AS type_name, c.max_length, c.precision, c.scale,
       c.is_nullable,
       CAST(CASE WHEN pk.column_id IS NULL THEN 0 ELSE 1 END AS bit) AS is_pk
FROM sys.columns c
JOIN sys.objects o ON o.object_id = c.object_id AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN (
    SELECT ic.object_id, ic.column_id
    FROM sys.indexes i
    JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    WHERE i.is_primary_key = 1
) pk ON pk.object_id = c.object_id AND pk.column_id = c.column_id
ORDER BY c.object_id, c.column_id
"""

# sys.partitions needs no VIEW DATABASE STATE permission (unlike
# sys.dm_db_partition_stats) and is accurate enough for tiering.
ROWCOUNT_SQL = """
SELECT p.object_id, SUM(p.rows) AS row_count
FROM sys.partitions p
WHERE p.index_id IN (0, 1)
GROUP BY p.object_id
"""

FK_SQL = """
SELECT fkc.parent_object_id, pc.name AS column_name,
       rs.name AS ref_schema, ro.name AS ref_table, rc.name AS ref_column
FROM sys.foreign_key_columns fkc
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.objects ro ON ro.object_id = fkc.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = ro.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
"""

INDEX_SQL = """
SELECT i.object_id, i.name, i.type_desc, i.is_unique, i.is_primary_key,
       STUFF((SELECT ', ' + c.name + CASE WHEN ic.is_included_column = 1 THEN ' (incl)' ELSE '' END
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
              ORDER BY ic.is_included_column, ic.key_ordinal
              FOR XML PATH('')), 1, 2, '') AS cols
FROM sys.indexes i
JOIN sys.objects o ON o.object_id = i.object_id AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0
WHERE i.index_id > 0 AND i.is_hypothetical = 0
"""

DESCRIPTION_SQL = """
SELECT ep.major_id AS object_id, CAST(ep.value AS nvarchar(4000)) AS description
FROM sys.extended_properties ep
WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description'
"""


def _type_label(type_name: str, max_length: int, precision: int, scale: int) -> str:
    t = type_name.lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if max_length == -1 else max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if max_length == -1 else max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({precision},{scale})"
    return t


def _rows(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r)) for r in cursor.fetchall()]


def introspect(connect: Callable[[], Any]) -> list[TableDoc]:
    """Read every user table and view. ``connect`` returns a DB-API connection."""
    conn = connect()
    try:
        cur = conn.cursor()
        objects = _rows(cur, OBJECTS_SQL)
        columns = _rows(cur, COLUMNS_SQL)
        counts = {r["object_id"]: int(r["row_count"]) for r in _rows(cur, ROWCOUNT_SQL)}
        fks = _rows(cur, FK_SQL)
        indexes = _rows(cur, INDEX_SQL)
        try:
            descriptions = {r["object_id"]: r["description"] for r in _rows(cur, DESCRIPTION_SQL)}
        except Exception:  # extended properties may be hidden; not fatal
            descriptions = {}
    finally:
        conn.close()

    cols_by_obj: dict[int, list[Column]] = defaultdict(list)
    for c in columns:
        cols_by_obj[c["object_id"]].append(
            Column(
                name=c["name"],
                data_type=_type_label(c["type_name"], c["max_length"], c["precision"], c["scale"]),
                nullable=bool(c["is_nullable"]),
                is_pk=bool(c["is_pk"]),
            )
        )
    fks_by_obj: dict[int, list[ForeignKey]] = defaultdict(list)
    for f in fks:
        fks_by_obj[f["parent_object_id"]].append(
            ForeignKey(f["column_name"], f"{f['ref_schema']}.{f['ref_table']}", f["ref_column"])
        )
    idx_by_obj: dict[int, list[str]] = defaultdict(list)
    for i in indexes:
        kind = "PK " if i["is_primary_key"] else ("UNIQUE " if i["is_unique"] else "")
        idx_by_obj[i["object_id"]].append(
            f"{i['name']}: {kind}{i['type_desc'].lower()} ({i['cols'] or ''})"
        )

    docs = []
    for o in objects:
        oid = o["object_id"]
        docs.append(
            TableDoc(
                schema=o["schema_name"],
                name=o["name"],
                object_type="view" if o["type"].strip() == "V" else "table",
                columns=cols_by_obj.get(oid, []),
                foreign_keys=fks_by_obj.get(oid, []),
                indexes=sorted(idx_by_obj.get(oid, [])),
                # Views have no partitions of their own; leave unknown.
                row_count=counts.get(oid) if o["type"].strip() == "U" else None,
                description=descriptions.get(oid) or "",
            )
        )
    docs.sort(key=lambda d: d.full_name.lower())
    return docs


# ------------------------------------------------------------------ snapshots
def save_snapshot(docs: list[TableDoc], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "objects": [d.to_dict() for d in docs],
            },
            indent=1,
        )
    )


def load_snapshot(path: str | Path) -> list[TableDoc]:
    data = json.loads(Path(path).read_text())
    return [TableDoc.from_dict(d) for d in data["objects"]]
