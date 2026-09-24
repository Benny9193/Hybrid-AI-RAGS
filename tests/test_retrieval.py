import sqlglot
from sqlglot import exp

from eds_rag.store import fts_query
from eds_rag.text import split_identifier, tokens


def names(hits):
    return [h.doc.full_name for h in hits if not h.related]


def test_split_identifier():
    assert split_identifier("PODetailItems") == ["po", "detail", "items"]
    assert split_identifier("BidHeaderDetail") == ["bid", "header", "detail"]
    assert split_identifier("date_created") == ["date", "created"]
    assert "vendor" in tokens("Vendors")


def test_fts_query_is_sanitised():
    q = fts_query('price" OR 1=1; DROP -- NEAR(')
    assert q and all(part.startswith('"') for part in q.split(" OR "))
    assert fts_query("the of and") is None


def test_exact_table_name_wins(retriever):
    assert names(retriever.search("CrossRefs", top_k=3))[0] == "dbo.CrossRefs"
    assert names(retriever.search("PODetailItems for a PO", top_k=3))[0] == "dbo.PODetailItems"


def test_semantic_matches(retriever):
    assert "dbo.CrossRefs" in names(retriever.search("vendor price for an item", top_k=3))
    assert "dbo.Awards" in names(retriever.search("which vendor was awarded the bid", top_k=3))
    assert names(retriever.search("requisition line items", top_k=3))[0] == "dbo.Detail"
    assert "dbo.School" in names(retriever.search("schools in a district", top_k=3))


def test_archive_twin_folded_unless_history_requested(retriever):
    hits = retriever.search("purchase orders", top_k=5)
    assert "archive.PO" not in names(hits)
    po = next(h for h in hits if h.doc.full_name == "dbo.PO")
    assert any("archive.PO" in n for n in po.notes)
    assert "archive.PO" in names(retriever.search("historical archived purchase orders", top_k=5))


def test_related_join_targets_appended(retriever):
    hits = retriever.search("CrossRefs", top_k=1)
    related = [h.doc.full_name for h in hits if h.related]
    assert "dbo.Vendors" in related and "dbo.Items" in related


def test_resolve(retriever):
    assert retriever.resolve("PO").full_name == "dbo.PO"  # dbo preferred
    assert retriever.resolve("archive.PO").full_name == "archive.PO"
    assert retriever.resolve("[dbo].[Vendors]").full_name == "dbo.Vendors"
    assert retriever.resolve("EDS.dbo.Vendors").full_name == "dbo.Vendors"
    assert retriever.resolve("vendors").full_name == "dbo.Vendors"
    assert retriever.resolve("Nope") is None


def test_check_tables_flags_unknown_with_suggestions(retriever):
    tree = sqlglot.parse_one("SELECT * FROM dbo.Vendor v JOIN dbo.PO p ON 1=1", read="tsql")
    known, unknown = retriever.check_tables(list(tree.find_all(exp.Table)))
    assert [d.full_name for d in known] == ["dbo.PO"]
    assert "dbo.Vendors" in unknown["dbo.Vendor"]


def test_gotchas_from_rules(retriever):
    items = retriever.resolve("Items")
    text = items.render()
    assert "Manufacturor" in text and "`IsActive`" in text and "High-volume" in text
    assert "`Active`" in retriever.resolve("Vendors").render()
    assert "cold historical storage" in retriever.resolve("archive.PO").render()


def test_retriever_picks_up_index_rebuilt_by_another_process(tmp_path, docs):
    # Simulates `eds-rag refresh --apply` (cron) rebuilding the index while the
    # MCP server keeps its own connection + caches open.
    from eds_rag.embeddings import HashingEmbedder
    from eds_rag.retrieval import SchemaRetriever
    from eds_rag.store import SchemaStore

    path = tmp_path / "idx.db"
    emb = HashingEmbedder()
    SchemaStore(path).rebuild(docs, emb)
    server_side = SchemaRetriever(SchemaStore(path), emb)
    assert names(server_side.search("CrossRefs", top_k=1)) == ["dbo.CrossRefs"]

    # Rebuild from a separate connection with a different row order, so every
    # doc gets a different rowid than before.
    SchemaStore(path).rebuild(list(reversed(docs)), emb)
    assert names(server_side.search("CrossRefs", top_k=1)) == ["dbo.CrossRefs"]
    assert names(server_side.search("vendor price for an item", top_k=3))[0] == "dbo.CrossRefs"
    assert server_side.resolve("PO").full_name == "dbo.PO"


def test_catalog_views_are_not_unknown_tables(retriever):
    tree = sqlglot.parse_one(
        "SELECT c.name FROM sys.columns c JOIN INFORMATION_SCHEMA.TABLES t ON 1=1", read="tsql"
    )
    known, unknown = retriever.check_tables(list(tree.find_all(exp.Table)))
    assert known == [] and unknown == {}



def test_check_tables_never_maps_other_database_onto_local_table(retriever):
    tree = sqlglot.parse_one("SELECT * FROM OtherDb.dbo.PO", read="tsql")
    known, unknown = retriever.check_tables(list(tree.find_all(exp.Table)))
    assert known == [] and list(unknown) == ["OtherDb.dbo.PO"]
