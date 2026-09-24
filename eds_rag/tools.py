"""The four tools the model calls, independent of the MCP transport.

Kept separate from ``server.py`` so they're unit-testable with a fake
executor and reusable from other agent frameworks.
"""

from __future__ import annotations

from .executor import ReadOnlyExecutor, format_result
from .retrieval import SchemaRetriever, render_hits
from .showplan import parse_showplan, render_plan
from .sql_guard import GuardResult, SqlGuard


class EdsTools:
    def __init__(
        self,
        retriever: SchemaRetriever,
        executor: ReadOnlyExecutor | None,
        *,
        max_rows: int = 1000,
        sample_rows: int = 5,
    ):
        self.retriever = retriever
        self.executor = executor
        self.guard = SqlGuard(max_rows=max_rows)
        self.sample_rows = sample_rows

    # --------------------------------------------------------- search_schema
    def search_schema(self, query: str, top_k: int = 8) -> str:
        top_k = max(1, min(int(top_k), 20))
        return render_hits(self.retriever.search(query, top_k=top_k))

    # ------------------------------------------------------ get_table_detail
    def get_table_detail(self, table_name: str, include_sample_rows: bool = True) -> str:
        doc = self.retriever.resolve(table_name)
        if doc is None:
            sugg = self.retriever.suggest(table_name)
            return f"Unknown table '{table_name}'." + (
                f" Did you mean: {', '.join(sugg)}?" if sugg else " Try search_schema."
            )
        out = [doc.render(max_columns=None, include_indexes=True)]
        if include_sample_rows and self.executor and doc.object_type in ("table", "view"):
            quoted = ".".join("[" + p.replace("]", "]]") + "]" for p in (doc.schema, doc.name))
            sql = f"SELECT TOP ({int(self.sample_rows)}) * FROM {quoted}"
            if doc.high_volume:
                sql += " WITH (NOLOCK)"  # peeking at shape only; dirty reads are fine
            try:
                res = self.executor.run(sql)
                out.append(f"Sample rows ({sql}):\n" + format_result(res))
            except Exception as e:  # sample rows are best effort
                out.append(f"(sample rows unavailable: {type(e).__name__}: {e})")
        return "\n\n".join(out)

    # ------------------------------------------------------------ checks
    def _precheck(self, sql: str) -> tuple[GuardResult, list[str]]:
        """Guard + table resolution + advisories shared by run/explain."""
        res = self.guard.check(sql)
        if not res.ok:
            return res, []
        known, unknown = self.retriever.check_tables(res.tables)
        if unknown:
            res.ok = False
            for name, sugg in unknown.items():
                hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
                res.errors.append(f"unknown table '{name}'.{hint}")
            res.errors.append(
                "Don't guess table names - call search_schema / get_table_detail, then retry."
            )
            return res, []
        advice = []
        for doc in known:
            if doc.schema.lower() == "archive":
                advice.append(f"{doc.full_name} is unindexed archive storage - expect scans.")
            if doc.high_volume:
                advice.append(
                    f"{doc.full_name} is high volume ({doc.rows_label()}); make sure the "
                    "WHERE clause hits an indexed key or date range."
                )
        return res, advice

    @staticmethod
    def _rejection(res: GuardResult) -> str:
        return "REJECTED - query was not run.\n" + "\n".join(f"- {e}" for e in res.errors)

    def _header(self, res: GuardResult, advice: list[str], sql: str) -> list[str]:
        lines = []
        if res.sql != sql.strip().rstrip(";").strip():
            lines.append(f"Executed (rewritten): {res.sql}")
        lines += [f"Note: {w}" for w in res.warnings + advice]
        return lines

    # ------------------------------------------------------------ run_query
    def run_query(self, sql: str) -> str:
        res, advice = self._precheck(sql)
        if not res.ok:
            return self._rejection(res)
        if self.executor is None:
            return "Query passed validation but no database connection is configured (EDS_SQL_CONN)."
        try:
            result = self.executor.run(res.sql)
        except Exception as e:
            msg = str(e)
            if "timeout" in msg.lower() or "HYT00" in msg:
                msg += " - query exceeded the time budget; narrow the filter or use explain_query."
            return f"Query failed: {type(e).__name__}: {msg}"
        return "\n".join(self._header(res, advice, sql) + [format_result(result)])

    # -------------------------------------------------------- explain_query
    def explain_query(self, sql: str) -> str:
        res, advice = self._precheck(sql)
        if not res.ok:
            return self._rejection(res)
        if self.executor is None:
            return "Query passed validation but no database connection is configured (EDS_SQL_CONN)."
        try:
            xml = self.executor.showplan_xml(res.sql)
        except Exception as e:
            return f"Could not get plan: {type(e).__name__}: {e}"
        sizes = {
            d.full_name.lower(): d.row_count
            for d in self.retriever.docs.values()
            if d.row_count is not None
        }
        plan = render_plan(parse_showplan(xml), sizes)
        return "\n".join(self._header(res, advice, sql) + [plan])
