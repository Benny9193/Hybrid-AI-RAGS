import copy

from eds_rag.embeddings import HashingEmbedder
from eds_rag.introspect import load_snapshot, save_snapshot
from eds_rag.models import Column, TableDoc
from eds_rag.refresh import diff_docs
from eds_rag.seed import Seed, merge
from eds_rag.store import SchemaStore

from .conftest import fake_schema


def test_seed_only_mode_builds_annotated_docs():
    res = merge([], Seed.load())
    names = {d.full_name for d in res.docs}
    assert {"dbo.CrossRefs", "dbo.Requisitions", "domain.procurement"} <= names
    cr = next(d for d in res.docs if d.full_name == "dbo.CrossRefs")
    assert cr.source == "seed" and cr.row_count == 150_000_000
    assert any(not fk.declared for fk in cr.foreign_keys)
    assert res.stale_annotations == []


def test_merge_keeps_declared_fks_and_flags_stale_annotations():
    res = merge(fake_schema(), Seed.load())
    cr = next(d for d in res.docs if d.full_name == "dbo.CrossRefs")
    # VendorId/ItemId are declared; CatalogId only comes from the seed.
    assert [(f.column, f.declared) for f in cr.foreign_keys] == [
        ("VendorId", True), ("ItemId", True), ("CatalogId", False)
    ]
    assert cr.description.startswith("Vendor-item mapping")
    stale = "\n".join(res.stale_annotations)
    # Seed tables that aren't in the fake schema are reported, not invented.
    assert "dbo.BidHeaderDetail" in stale
    # Users table doesn't exist in the fake schema either.
    assert "dbo.Users" in stale


def test_merge_drops_expected_join_on_missing_column():
    schema = fake_schema()
    bh = next(d for d in schema if d.full_name == "dbo.Awards")
    bh.columns = [c for c in bh.columns if c.name != "VendorId"]
    bh.foreign_keys = [f for f in bh.foreign_keys if f.column != "VendorId"]
    res = merge(schema, Seed.load())
    assert any("dbo.Awards: expected_joins column 'VendorId'" in s for s in res.stale_annotations)


def test_snapshot_roundtrip(tmp_path):
    docs = fake_schema()
    save_snapshot(docs, tmp_path / "snap.json")
    loaded = load_snapshot(tmp_path / "snap.json")
    assert [d.structure_signature() for d in loaded] == [d.structure_signature() for d in docs]


def test_diff_detects_drift():
    old = merge(fake_schema(), Seed.load()).docs
    new_raw = copy.deepcopy(fake_schema())
    vendors = next(d for d in new_raw if d.full_name == "dbo.Vendors")
    vendors.columns.append(Column("DateModified", "datetime"))
    vendors.columns = [c for c in vendors.columns if c.name != "Active"]
    vendors.columns.append(Column("IsActive", "bit", nullable=False))
    po = next(d for d in new_raw if d.full_name == "dbo.PO")
    po.columns[3] = Column("Total", "decimal(18,2)", nullable=False)
    district = next(d for d in new_raw if d.full_name == "dbo.District")
    district.row_count = 12_000  # tiny -> small
    new_raw = [d for d in new_raw if d.full_name != "dbo.vw_OpenPOs"]
    new_raw.append(TableDoc(schema="dbo", name="VendorUploads"))
    new = merge(new_raw, Seed.load()).docs

    report = diff_docs(old, new)
    assert report.has_drift
    assert report.added == ["dbo.VendorUploads"]
    assert report.removed == ["dbo.vw_OpenPOs"]
    by = {c.name: c for c in report.changed}
    assert sorted(by["dbo.Vendors"].added_columns) == ["DateModified", "IsActive"]
    assert by["dbo.Vendors"].removed_columns == ["Active"]
    assert by["dbo.PO"].retyped_columns == ["Total money -> decimal(18,2)"]
    assert by["dbo.District"].tier_change.startswith("tiny -> small")
    md = report.to_markdown()
    assert "dbo.VendorUploads" in md and "Type changes" in md


def test_no_drift_on_identical_schema():
    docs = merge(fake_schema(), Seed.load()).docs
    assert not diff_docs(docs, copy.deepcopy(docs)).has_drift


def test_rebuild_reuses_unchanged_embeddings():
    store = SchemaStore(":memory:")
    emb = HashingEmbedder()
    docs = merge(fake_schema(), Seed.load()).docs
    first = store.rebuild(docs, emb)
    assert first["reused"] == 0
    docs[0].columns.append(Column("NewCol", "int"))
    second = store.rebuild(docs, emb)
    assert second["embedded"] == 1 and second["reused"] == len(docs) - 1
