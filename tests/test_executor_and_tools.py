import datetime as dt
import decimal
import json

import pytest

from eds_rag.executor import NotReadOnlyError, QueryResult, ReadOnlyExecutor, format_result
from eds_rag.showplan import parse_showplan, render_plan
from eds_rag.tools import EdsTools

PLAN_XML = """<?xml version="1.0"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.5">
 <BatchSequence><Batch><Statements>
  <StmtSimple StatementSubTreeCost="1843.2" StatementEstRows="1000">
   <QueryPlan>
    <MissingIndexes>
     <MissingIndexGroup Impact="97.1">
      <MissingIndex Database="[EDS]" Schema="[dbo]" Table="[CrossRefs]">
       <ColumnGroup Usage="EQUALITY"><Column Name="[VendorItemCode]" ColumnId="5"/></ColumnGroup>
       <ColumnGroup Usage="INCLUDE"><Column Name="[Price]" ColumnId="6"/></ColumnGroup>
      </MissingIndex>
     </MissingIndexGroup>
    </MissingIndexes>
    <RelOp PhysicalOp="Top" LogicalOp="Top" EstimateRows="1000" EstimatedTotalSubtreeCost="1843.2">
     <Top>
      <RelOp PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan"
             EstimateRows="1000" EstimatedTotalSubtreeCost="1843.1">
       <IndexScan><Object Database="[EDS]" Schema="[dbo]" Table="[CrossRefs]" Index="[PK_CrossRefs]"/></IndexScan>
      </RelOp>
     </Top>
    </RelOp>
    <Warnings><PlanAffectingConvert ConvertIssue="Seek Plan" Expression="CONVERT_IMPLICIT(nvarchar(50),[VendorItemCode],0)"/></Warnings>
   </QueryPlan>
  </StmtSimple>
 </Statements></Batch></BatchSequence>
</ShowPlanXML>"""


# ------------------------------------------------------------ fake pyodbc
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql):
        self.conn.log.append(sql)
        if sql.startswith("SELECT HAS_PERMS_BY_NAME"):
            names = ["INSERT", "UPDATE", "DELETE", "ALTER", "EXECUTE", "CREATE TABLE", "CONTROL",
                     "sysadmin", "db_owner", "db_datawriter", "db_ddladmin"]
            self.description = [(n,) for n in names]
            self._rows = [tuple(1 if n in self.conn.granted else 0 for n in names)]
        elif sql.startswith("SET"):
            self.description = None
            if sql == "SET SHOWPLAN_XML ON":
                self.conn.showplan = True
        elif self.conn.showplan:
            self.description = [("xml",)]
            self._rows = [(PLAN_XML,)]
        else:
            self.description = [("VendorId",), ("Price",), ("DateCreated",)]
            self._rows = [
                (i, decimal.Decimal("12.50"), dt.datetime(2026, 1, 2)) for i in range(self.conn.n_rows)
            ]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        return self._rows[:n]

    def close(self):
        pass


class FakeConn:
    def __init__(self, granted=(), n_rows=3):
        self.granted = set(granted)
        self.n_rows = n_rows
        self.log = []
        self.showplan = False
        self.timeout = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def make_executor(conn, **kw):
    return ReadOnlyExecutor("fake", connect=lambda: conn, **kw)


# ---------------------------------------------------------------- executor
def test_executor_sets_session_limits_and_caps_rows(tmp_path):
    conn = FakeConn(n_rows=10)
    ex = make_executor(conn, max_rows=5, timeout_s=7, audit_log=tmp_path / "audit.jsonl")
    res = ex.run("SELECT TOP (5) * FROM dbo.CrossRefs")
    assert conn.timeout == 7
    assert "SET LOCK_TIMEOUT 7000" in conn.log and "SET ROWCOUNT 6" in conn.log
    assert "SET DEADLOCK_PRIORITY LOW" in conn.log
    assert len(res.rows) == 5 and res.truncated
    assert res.rows[0] == [0, 12.5, "2026-01-02T00:00:00"]
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "run" and entry["rows"] == 5 and entry["status"] == "ok"
    assert conn.closed


@pytest.mark.parametrize("perm", ["INSERT", "db_owner", "db_datawriter", "EXECUTE", "sysadmin"])
def test_executor_refuses_writable_login(perm):
    with pytest.raises(NotReadOnlyError, match=perm):
        make_executor(FakeConn(granted=[perm])).run("SELECT 1")


def test_executor_permission_check_can_be_disabled_for_dev():
    make_executor(FakeConn(granted=["db_owner"]), verify_read_only=False).run("SELECT 1")


def test_format_result():
    out = format_result(QueryResult(["a", "b"], [[1, None], ["x|y", "z"]], True, 3))
    assert "| 1 | NULL |" in out and "x\\|y" in out and "TRUNCATED" in out


# ---------------------------------------------------------------- showplan
def test_parse_and_render_showplan():
    plan = parse_showplan(PLAN_XML)
    assert plan.statement_cost == pytest.approx(1843.2)
    assert [o.obj for o in plan.scans()] == ["dbo.CrossRefs [PK_CrossRefs]"]
    assert plan.missing_indexes == ["dbo.CrossRefs (VendorItemCode) INCLUDE (Price) - est. impact 97.1%"]
    text = render_plan(plan, {"dbo.crossrefs": 150_000_000})
    assert "RISK" in text and "150,000,000" in text and "implicit conversion" in text


# ------------------------------------------------------------------- tools
@pytest.fixture
def tools(retriever):
    return EdsTools(retriever, make_executor(FakeConn()), max_rows=100)


def test_run_query_rejects_writes(tools):
    out = tools.run_query("DELETE FROM dbo.PO")
    assert out.startswith("REJECTED") and "DELETE" in out


def test_run_query_rejects_unknown_tables_with_suggestions(tools):
    out = tools.run_query("SELECT * FROM dbo.CrossRef")
    assert out.startswith("REJECTED") and "dbo.CrossRefs" in out and "search_schema" in out


def test_run_query_injects_top_and_advises_on_big_tables(tools):
    out = tools.run_query("SELECT VendorId, Price FROM dbo.CrossRefs WHERE ItemId = 5")
    assert "Executed (rewritten): SELECT TOP (100) VendorId" in out
    assert "high volume" in out
    assert "| VendorId | Price | DateCreated |" in out


def test_run_query_without_connection(retriever):
    out = EdsTools(retriever, None).run_query("SELECT Name FROM Vendors")
    assert "no database connection" in out


def test_explain_query(tools):
    out = tools.explain_query("SELECT Price FROM dbo.CrossRefs WHERE VendorItemCode = 'A1'")
    assert "RISK" in out and "missing-index" in out


def test_get_table_detail(tools):
    out = tools.get_table_detail("CrossRefs")
    assert "## dbo.CrossRefs" in out and "IX_CrossRefs_Item" in out
    assert "Sample rows (SELECT TOP (5) * FROM [dbo].[CrossRefs] WITH (NOLOCK))" in out
    assert "Did you mean" in tools.get_table_detail("CrossRef")


def test_search_schema_renders_markdown(tools):
    out = tools.search_schema("vendor pricing", top_k=3)
    assert "## dbo.CrossRefs" in out and "Related join targets" in out
