"""MCP server exposing the EDS schema-RAG + guarded query tools.

Run with ``eds-rag serve`` (stdio) or point an MCP client at
``python -m eds_rag.server``.
"""

from __future__ import annotations

from pathlib import Path

from mcp.types import ToolAnnotations

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from .config import Settings
from .embeddings import get_embedder
from .executor import ReadOnlyExecutor
from .retrieval import SchemaRetriever
from .store import SchemaStore
from .tools import EdsTools

INSTRUCTIONS = """\
Tools for answering questions from the EDS K-12 cooperative procurement SQL Server database.
Workflow: (1) search_schema with the user's question to find relevant tables - never guess
table or column names; (2) get_table_detail for exact columns when writing joins/filters;
(3) explain_query first for anything touching high-volume tables; (4) run_query.
Queries are read-only, single-SELECT, row-capped and time-limited; rejected queries return the
reason - fix and retry rather than working around the guard."""


def build_tools(settings: Settings) -> EdsTools:
    if not Path(settings.index_path).exists():
        raise SystemExit(
            f"schema index not found at {settings.index_path} - run `eds-rag build-index` first"
        )
    store = SchemaStore(settings.index_path)
    retriever = SchemaRetriever(store, get_embedder(settings.embedder))
    executor = None
    if settings.conn_str:
        executor = ReadOnlyExecutor(
            settings.conn_str,
            max_rows=settings.max_rows,
            timeout_s=settings.timeout_s,
            verify_read_only=not settings.allow_writable_login,
            audit_log=settings.audit_log,
        )
    return EdsTools(retriever, executor, max_rows=settings.max_rows, sample_rows=settings.sample_rows)


# Every tool is read-only by construction; tell clients so they can auto-approve.
# (Built from the wire-format dict so it works with both 1.x and 2.x field names.)
READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
)


def create_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    tools = build_tools(settings)
    mcp = MCPServer("eds-schema", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def search_schema(query: str, top_k: int = 8) -> str:
        """Find the EDS tables/views relevant to a question (hybrid semantic + keyword search).

        Returns table descriptions, columns, join paths and known gotchas for the top matches,
        plus summaries of their FK join targets. Call this before writing any SQL.
        """
        return tools.search_schema(query, top_k)

    @mcp.tool(annotations=READ_ONLY)
    def get_table_detail(table_name: str, include_sample_rows: bool = True) -> str:
        """Full detail for one table or view: every column with type, PK/FK, indexes, gotchas,
        and (if connected) a few sample rows. Accepts 'Vendors', 'dbo.Vendors' or 'archive.PO'."""
        return tools.get_table_detail(table_name, include_sample_rows)

    @mcp.tool(annotations=READ_ONLY)
    def run_query(sql: str) -> str:
        """Run ONE read-only T-SQL SELECT against EDS and return the rows as a markdown table.

        Enforced by the tool (not optional): no DDL/DML/EXEC/multi-statement, unknown tables are
        rejected with suggestions, TOP (row cap) is injected if missing, and a query timeout
        applies. Filter high-volume tables (CrossRefs, BidHeaderDetail, Detail, Items,
        PODetailItems) on indexed keys or dates.
        """
        return tools.run_query(sql)

    @mcp.tool(annotations=READ_ONLY)
    def explain_query(sql: str) -> str:
        """Get the estimated execution plan for a SELECT without running it: cost, row estimates,
        scans on large tables, and missing-index hints. Use before running anything heavy."""
        return tools.explain_query(sql)

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
