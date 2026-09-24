# Hybrid-AI-RAGS: EDS schema RAG + guarded SQL tools

The model shouldn't see all 438 tables and 474 views in every prompt. This project
retrieves only the relevant schema for each question, then lets the model query EDS through
tools that enforce read-only access themselves. The prompt is not what keeps it read-only.

```
question ─► search_schema ─► hybrid retrieval (vector + BM25 + exact name) ─► top-K table chunks
                                                                              + FK join targets
model drafts SQL ─► run_query / explain_query
                      │
                      ├─ 1. SQL guard: deny-list + T-SQL parse, single SELECT only, TOP (N) injected
                      ├─ 2. table check: unknown table ⇒ rejected with suggestions (no guessing)
                      ├─ 3. executor: SET ROWCOUNT, fetch cap, query + lock timeout, low deadlock priority
                      └─ 4. login: db_datareader only; server refuses to start if it can write
```

## Components

| Piece | Where | What it does |
|---|---|---|
| Schema corpus | `eds_rag/introspect.py`, `eds_rag/data/eds_seed.yaml`, `eds_rag/seed.py` | Builds one chunk per table or view from the live catalog: columns and types, PKs, declared FKs, indexes, row count and tier, and `MS_Description`. Curated annotations from the seed are merged on top: a procurement-terms description, synonyms ("purchase order" → `PO`), expected joins and gotchas. It also adds one overview chunk per domain (procurement, catalog, bidding, organization). |
| Rule-based gotchas | `eds_rag/data/eds_seed.yaml` → `rules` | Applied to every object automatically. Any table with a `Manufacturor` column gets the typo warning. A table's active-flag note names the column it actually uses (`Active` vs `IsActive`). Tables in `archive.*` get the no-PK/no-index warning, and tables with ≥10M rows get the high-volume warning. |
| Retrieval | `eds_rag/store.py`, `eds_rag/retrieval.py` | SQLite index that stores vectors plus FTS5 BM25, with identifiers split so `PODetailItems` matches "po detail items". Three rankers are fused with Reciprocal Rank Fusion: vector, keyword, and exact table name or alias phrase. `archive.X` is folded into `dbo.X` unless the question is about history. FK neighbours of the top hits are appended so the model sees its join targets. |
| Second pass | `SchemaRetriever.check_tables` | Parses the draft SQL. Any table the index doesn't know is rejected with "did you mean …" suggestions, so the model looks it up instead of guessing. |
| MCP tools | `eds_rag/server.py`, `eds_rag/tools.py` | `search_schema`, `get_table_detail`, `run_query`, `explain_query`. All four are marked `readOnlyHint`. |
| Guardrails | `eds_rag/sql_guard.py`, `eds_rag/executor.py`, `sql/create_readonly_login.sql` | Enforced in the tool layers shown in the diagram above. |
| Drift refresh | `eds_rag/refresh.py`, `deploy/refresh.sh` | Re-introspects the database and diffs it against the index: added or removed objects, column adds, drops and type changes, FK and index changes, and row-tier moves. It also flags seed annotations that no longer match. It writes a markdown report and exits with code 2 on drift. |

### Tools the model gets

- **`search_schema(query, top_k=8)`**: returns markdown chunks for the best-matching tables and views, with columns, joins and gotchas, plus a short list of related join targets.
- **`get_table_detail(table_name)`**: returns every column, index and gotcha for one table or view, plus `TOP 5` sample rows (with `NOLOCK` on high-volume tables). It accepts `Vendors`, `dbo.Vendors` or `[archive].[PO]`.
- **`run_query(sql)`**: validates, rewrites and executes the query. The result shows any rewritten SQL, advisory notes (such as a high-volume table or an archive scan) and the rows as a markdown table.
- **`explain_query(sql)`**: returns `SET SHOWPLAN_XML` output, so nothing is executed. It shows estimated cost and rows, flags scans on tables with ≥1M rows, lists missing-index hints and reports implicit-conversion warnings.

### What the guard rejects

The guard rejects the following. It checks comments, string literals and `[bracketed]` identifiers separately, so `WHERE Name LIKE '%delete%'` and `SELECT [Update]` are allowed.

- Anything other than exactly one `SELECT` / `WITH … SELECT` / `UNION` statement.
- Writes and schema changes: `INSERT UPDATE DELETE MERGE DROP ALTER CREATE TRUNCATE SELECT … INTO`.
- Executing code: `EXEC`, `sp_*`, `xp_*`.
- Access outside the database: `OPENROWSET`, `OPENQUERY`, `OPENDATASOURCE`, `BULK`, 4-part linked-server names, and any 3-part (database-qualified) name, including `master`/`msdb`/`tempdb`/`model`. Queries must use `schema.table` in the connected database.
- Session and transaction control: `SET DECLARE USE BEGIN COMMIT WAITFOR`, plus `DBCC`, `BACKUP`, `KILL` and similar.
- Unterminated strings or comments.

**Row cap.** If the outer `SELECT` has no `TOP`, the guard adds `TOP (N)` (the CTE-aware check finds the real outer query). A larger `TOP` is clamped to N. UNION queries, `OFFSET/FETCH` and `TOP … PERCENT` are still capped by `SET ROWCOUNT` and `fetchmany` in the executor.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[sqlserver,dev]"        # pyodbc needs the Microsoft ODBC Driver 18 on the host

# 0. Works offline: a seed-only index (annotated tables, no columns yet)
eds-rag build-index
eds-rag search "pending approvals for Leon County" --names-only

# 1. Create the read-only login (DBA, lower environment first)
#    sql/create_readonly_login.sql

# 2. Index the real schema
export EDS_SQL_CONN='Driver={ODBC Driver 18 for SQL Server};Server=...;Database=EDS;UID=eds_rag_reader;PWD=...;Encrypt=yes;ApplicationIntent=ReadOnly'
eds-rag snapshot --out data/snapshot.json   # introspect once...
eds-rag build-index --snapshot data/snapshot.json   # ...index from the snapshot (or: --live)

# 3. Serve over MCP (stdio)
eds-rag serve
```

To register the server with Claude Code or Claude Desktop, see `deploy/mcp.json.example`.

Debug helpers:

```bash
eds-rag check-sql "SELECT * FROM dbo.CrossRefs WHERE ItemId = 42"
# OK
# SELECT TOP (1000) * FROM dbo.CrossRefs WHERE ItemId = 42
```

### Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `EDS_RAG_INDEX` | `data/eds_schema.db` | SQLite schema index |
| `EDS_SQL_CONN` | *(unset → tools validate but don't execute)* | ODBC string for the **read-only** login |
| `EDS_RAG_EMBEDDER` | `hashing` | `hashing[:dim]`, `fastembed[:model]`, `voyage[:model]` |
| `EDS_MAX_ROWS` | `1000` | Row cap (TOP injection + ROWCOUNT + fetch) |
| `EDS_QUERY_TIMEOUT` | `10` | Seconds, applied as the query timeout and `LOCK_TIMEOUT` |
| `EDS_SAMPLE_ROWS` | `5` | Sample rows in `get_table_detail` |
| `EDS_AUDIT_LOG` | *(off)* | JSONL audit log of every query and plan request |
| `EDS_RAG_SEED` | `eds_rag/data/eds_seed.yaml` | Seed annotations file |
| `EDS_ALLOW_WRITABLE_LOGIN` | *(off)* | `1` skips the read-only login check. Only for a throwaway local DB. |

### Embedders

The default `hashing` embedder is offline, deterministic and has no extra dependencies. Schema
text is mostly identifiers, and the BM25 ranker covers exact terms, so it holds up well at this
corpus size. For better matching of paraphrased questions:

```bash
pip install -e ".[fastembed]" && export EDS_RAG_EMBEDDER=fastembed   # local ONNX (bge-small)
pip install -e ".[voyage]"    && export EDS_RAG_EMBEDDER=voyage      # API, needs VOYAGE_API_KEY
eds-rag build-index --snapshot data/snapshot.json                   # rebuild after switching
```

On rebuild, the index reuses an embedding whenever a chunk's text hasn't changed. A weekly
refresh therefore only re-embeds the tables that drifted.

Vectors are stored as BLOBs and searched by brute-force cosine in numpy, which takes under a
millisecond for about 900 objects. `SchemaStore` is the place to swap in sqlite-vec or pgvector
if the corpus grows by orders of magnitude.

## Keeping it fresh

```bash
# crontab: Mondays 06:00
0 6 * * 1  /opt/eds-rag/deploy/refresh.sh
```

`eds-rag refresh --live --apply --report reports/drift-YYYYMMDD.md` does the following:

1. Re-introspects the database through `sys.*` catalog views.
2. Diffs the result against the docs stored in the index.
3. Checks the seed. It reports annotated tables that are gone and `expected_joins` whose column no longer exists.
4. Writes the markdown report.
5. With `--apply`, rebuilds the index.

It exits with `2` when there is drift, so cron or CI can alert on it. New tables show up under
"need a seed description". That list is your to-do for extending `eds_rag/data/eds_seed.yaml`.

## Curating the seed

`eds_rag/data/eds_seed.yaml` holds the knowledge the catalog can't provide: what a table means in
procurement terms, the synonyms people use and the gotchas. Only confident facts go there.
`expected_joins` follow the `{Table}Id` convention and show up as "not a declared FK - verify"
until introspection confirms the column exists. If the column is missing, the seed annotation is
flagged as stale instead of being shown to the model.

## Tests

```bash
pytest -q
```

The tests run against a small synthetic schema with EDS's shape and a fake pyodbc connection, so
no database is needed. They cover the guard (bypass attempts and allowed T-SQL), retrieval
quality and name resolution, seed merging, drift diffing, the executor limits and the read-only
login check, showplan parsing, and the MCP server surface.
