"""Shared fixtures: a small synthetic EDS-shaped schema (not real data)."""

from __future__ import annotations

import pytest

from eds_rag.embeddings import HashingEmbedder
from eds_rag.models import Column, ForeignKey, TableDoc
from eds_rag.retrieval import SchemaRetriever
from eds_rag.seed import Seed, merge
from eds_rag.store import SchemaStore


def _t(schema, name, cols, rows=None, fks=(), indexes=(), object_type="table"):
    return TableDoc(
        schema=schema,
        name=name,
        object_type=object_type,
        columns=[Column(n, t, nullable=not pk, is_pk=pk) for n, t, pk in cols],
        foreign_keys=[ForeignKey(*f) for f in fks],
        indexes=list(indexes),
        row_count=rows,
    )


def fake_schema() -> list[TableDoc]:
    return [
        _t("dbo", "Vendors", [("VendorId", "int", True), ("Name", "nvarchar(200)", False),
                              ("Active", "bit", False)], rows=40_000),
        _t("dbo", "Catalog", [("CatalogId", "int", True), ("VendorId", "int", False),
                              ("CatalogName", "nvarchar(200)", False)], rows=90_000,
           fks=[("VendorId", "dbo.Vendors", "VendorId")]),
        _t("dbo", "Items", [("ItemId", "int", True), ("Description", "nvarchar(max)", False),
                            ("Manufacturor", "nvarchar(100)", False), ("CategoryId", "int", False),
                            ("IsActive", "bit", False)], rows=30_000_000),
        _t("dbo", "Category", [("CategoryId", "int", True), ("CategoryName", "nvarchar(100)", False)],
           rows=5_000),
        _t("dbo", "CrossRefs", [("CrossRefId", "bigint", True), ("VendorId", "int", False),
                                ("CatalogId", "int", False), ("ItemId", "int", False),
                                ("VendorItemCode", "varchar(50)", False), ("Price", "money", False)],
           rows=150_000_000, fks=[("VendorId", "dbo.Vendors", "VendorId"),
                                  ("ItemId", "dbo.Items", "ItemId")],
           indexes=["IX_CrossRefs_Item: nonclustered (ItemId, VendorId)"]),
        _t("dbo", "Requisitions", [("RequisitionId", "int", True), ("SchoolId", "int", False),
                                   ("UserId", "int", False), ("DateCreated", "datetime", False),
                                   ("Status", "varchar(20)", False)], rows=4_000_000),
        _t("dbo", "Detail", [("DetailId", "bigint", True), ("RequisitionId", "int", False),
                             ("ItemId", "int", False), ("Quantity", "int", False),
                             ("UnitPrice", "money", False)], rows=30_000_000,
           fks=[("RequisitionId", "dbo.Requisitions", "RequisitionId")]),
        _t("dbo", "Approvals", [("ApprovalId", "int", True), ("RequisitionId", "int", False),
                                ("DateApproved", "datetime", True)], rows=9_000_000),
        _t("dbo", "PO", [("POId", "int", True), ("RequisitionId", "int", False),
                         ("VendorId", "int", False), ("Total", "money", False),
                         ("DateCreated", "datetime", False)], rows=3_900_000,
           fks=[("VendorId", "dbo.Vendors", "VendorId")]),
        _t("dbo", "PODetailItems", [("PODetailItemId", "bigint", True), ("POId", "int", False),
                                    ("ItemId", "int", False), ("Quantity", "int", False)],
           rows=24_000_000, fks=[("POId", "dbo.PO", "POId")]),
        _t("archive", "PO", [("POId", "int", False), ("RequisitionId", "int", False),
                             ("VendorId", "int", False), ("Total", "money", False)], rows=12_000_000),
        _t("dbo", "BidHeaders", [("BidHeaderId", "int", True), ("Title", "nvarchar(200)", False),
                                 ("DateOpened", "datetime", False)], rows=20_000),
        _t("dbo", "Awards", [("AwardId", "int", True), ("BidHeaderId", "int", False),
                             ("VendorId", "int", False)], rows=300_000,
           fks=[("BidHeaderId", "dbo.BidHeaders", "BidHeaderId"),
                ("VendorId", "dbo.Vendors", "VendorId")]),
        _t("dbo", "District", [("DistrictId", "int", True), ("Name", "nvarchar(200)", False),
                               ("State", "char(2)", False)], rows=3_500),
        _t("dbo", "School", [("SchoolId", "int", True), ("DistrictId", "int", False),
                             ("Name", "nvarchar(200)", False)], rows=12_900,
           fks=[("DistrictId", "dbo.District", "DistrictId")]),
        _t("dbo", "vw_OpenPOs", [("POId", "int", False), ("Total", "money", False)],
           object_type="view"),
    ]


@pytest.fixture
def docs() -> list[TableDoc]:
    return merge(fake_schema(), Seed.load()).docs


@pytest.fixture
def retriever(docs) -> SchemaRetriever:
    store = SchemaStore(":memory:")
    emb = HashingEmbedder()
    store.rebuild(docs, emb)
    return SchemaRetriever(store, emb)
