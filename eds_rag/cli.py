"""``eds-rag`` command line: build the index, refresh/diff, debug, serve."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .embeddings import get_embedder
from .introspect import introspect, load_snapshot, save_snapshot
from .models import TableDoc
from .refresh import diff_docs, docs_differ
from .seed import Seed, merge
from .store import SchemaStore

EXIT_DRIFT = 2


def _live_docs(conn_str: str | None) -> list[TableDoc]:
    if not conn_str:
        sys.exit("no connection string: pass --conn or set EDS_SQL_CONN")
    from .executor import _pyodbc_connect

    return introspect(lambda: _pyodbc_connect(conn_str, login_timeout=30))


def _source_docs(args: argparse.Namespace, settings: Settings) -> list[TableDoc]:
    if getattr(args, "snapshot", None):
        return load_snapshot(args.snapshot)
    if getattr(args, "live", False) or getattr(args, "conn", None):
        return _live_docs(args.conn or settings.conn_str)
    return []


def cmd_snapshot(args, settings: Settings) -> int:
    docs = _live_docs(args.conn or settings.conn_str)
    save_snapshot(docs, args.out)
    print(f"wrote {len(docs)} objects to {args.out}")
    return 0


def cmd_build(args, settings: Settings) -> int:
    introspected = _source_docs(args, settings)
    if not introspected:
        print("no snapshot/connection given: building SEED-ONLY index (annotated tables, no columns)")
    merged = merge(introspected, Seed.load(args.seed or settings.seed_path))
    store = SchemaStore(args.index or settings.index_path)
    stats = store.rebuild(merged.docs, get_embedder(args.embedder or settings.embedder))
    print(f"indexed {stats['docs']} docs ({stats['embedded']} embedded, {stats['reused']} reused) "
          f"-> {store.path}")
    for s in merged.stale_annotations:
        print(f"warning: {s}")
    return 0


def cmd_refresh(args, settings: Settings) -> int:
    index = args.index or settings.index_path
    if not Path(index).exists():
        sys.exit(f"no index at {index}; run build-index first")
    new = _source_docs(args, settings) or _live_docs(settings.conn_str)
    if args.snapshot_out:
        save_snapshot(new, args.snapshot_out)
    store = SchemaStore(index)
    merged = merge(new, Seed.load(args.seed or settings.seed_path))
    stored = store.all_docs()
    report = diff_docs(stored, merged.docs)
    report.stale_annotations = merged.stale_annotations
    text = report.to_markdown()
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n")
        print(f"report written to {args.report}")
    print(text)
    # Rebuild whenever the merged docs differ from what's stored - that includes
    # seed-only edits (descriptions, aliases, gotchas), which aren't schema drift.
    if args.apply and docs_differ(stored, merged.docs):
        stats = store.rebuild(merged.docs, get_embedder(args.embedder or settings.embedder))
        print(f"index rebuilt: {stats['embedded']} re-embedded, {stats['reused']} reused")
    return EXIT_DRIFT if report.has_drift else 0


def cmd_search(args, settings: Settings) -> int:
    from .retrieval import SchemaRetriever, render_hits

    r = SchemaRetriever(SchemaStore(args.index or settings.index_path),
                        get_embedder(args.embedder or settings.embedder))
    hits = r.search(args.query, top_k=args.top_k)
    if args.names_only:
        for h in hits:
            print(("  (related) " if h.related else "") + h.doc.full_name)
    else:
        print(render_hits(hits))
    return 0


def cmd_check_sql(args, settings: Settings) -> int:
    from .sql_guard import SqlGuard

    res = SqlGuard(max_rows=settings.max_rows).check(args.sql)
    print("OK" if res.ok else "REJECTED")
    for e in res.errors:
        print(f"error: {e}")
    for w in res.warnings:
        print(f"warning: {w}")
    if res.ok:
        print(res.sql)
    return 0 if res.ok else 1


def cmd_serve(args, settings: Settings) -> int:
    from .server import create_server

    create_server(settings).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eds-rag", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, source: bool = False):
        sp.add_argument("--index", help="SQLite index path (default $EDS_RAG_INDEX or data/eds_schema.db)")
        sp.add_argument("--embedder", help="hashing | fastembed[:model] | voyage[:model]")
        sp.add_argument("--seed", help="seed annotations YAML (default: the packaged eds_rag/data/eds_seed.yaml)")
        if source:
            g = sp.add_mutually_exclusive_group()
            g.add_argument("--snapshot", help="introspection snapshot JSON to use instead of a live DB")
            g.add_argument("--live", action="store_true", help="introspect the DB at $EDS_SQL_CONN")
            g.add_argument("--conn", help="ODBC connection string to introspect")

    sp = sub.add_parser("snapshot", help="introspect the live DB into a JSON snapshot")
    sp.add_argument("--conn")
    sp.add_argument("--out", default="data/snapshot.json")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser("build-index", help="build the schema index (seed-only if no source given)")
    common(sp, source=True)
    sp.set_defaults(fn=cmd_build)

    sp = sub.add_parser("refresh", help=f"re-introspect, diff vs index, report drift (exit {EXIT_DRIFT} on drift)")
    common(sp, source=True)
    sp.add_argument("--report", help="write the markdown drift report here")
    sp.add_argument("--snapshot-out", help="also save the fresh introspection snapshot")
    sp.add_argument("--apply", action="store_true", help="rebuild the index with the fresh schema")
    sp.set_defaults(fn=cmd_refresh)

    sp = sub.add_parser("search", help="debug: run search_schema from the shell")
    common(sp)
    sp.add_argument("query")
    sp.add_argument("--top-k", type=int, default=8)
    sp.add_argument("--names-only", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("check-sql", help="debug: run the SQL guard on a query")
    sp.add_argument("sql")
    sp.set_defaults(fn=cmd_check_sql)

    sp = sub.add_parser("serve", help="run the MCP server over stdio")
    sp.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args, Settings.from_env())


if __name__ == "__main__":
    sys.exit(main())
