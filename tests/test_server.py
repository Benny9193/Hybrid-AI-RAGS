import asyncio

from eds_rag.config import Settings
from eds_rag.embeddings import HashingEmbedder
from eds_rag.seed import Seed, merge
from eds_rag.server import create_server
from eds_rag.store import SchemaStore

from .conftest import fake_schema


def test_mcp_server_exposes_four_tools(tmp_path):
    index = tmp_path / "idx.db"
    SchemaStore(index).rebuild(merge(fake_schema(), Seed.load()).docs, HashingEmbedder())
    server = create_server(Settings(index_path=str(index)))

    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"search_schema", "get_table_detail", "run_query", "explain_query"}

    assert all(t.annotations.model_dump(by_alias=True)["readOnlyHint"] for t in tools)

    result = asyncio.run(server.call_tool("run_query", {"sql": "DROP TABLE dbo.PO"}))
    # mcp 2.x returns CallToolResult; 1.x returns (content, structured) or content.
    content = getattr(result, "content", None) or (result[0] if isinstance(result, tuple) else result)
    assert "REJECTED" in content[0].text
