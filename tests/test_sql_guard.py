import pytest

from eds_rag.sql_guard import GuardError, SqlGuard, scrub

guard = SqlGuard(max_rows=100)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dbo.Vendors (Name) VALUES ('x')",
        "UPDATE dbo.Vendors SET Active = 0",
        "DELETE FROM dbo.PO",
        "MERGE dbo.PO AS t USING x ON 1=1 WHEN MATCHED THEN DELETE;",
        "DROP TABLE dbo.PO",
        "ALTER TABLE dbo.PO ADD x int",
        "TRUNCATE TABLE dbo.PO",
        "CREATE TABLE x (a int)",
        "EXEC sp_who",
        "EXECUTE('SELECT 1')",
        "sp_helptext 'x'",
        "SELECT 1; DROP TABLE dbo.PO",
        "SELECT 1; SELECT 2",
        "SELECT * INTO dbo.Copy FROM dbo.PO",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
        "SELECT * FROM OPENQUERY(srv, 'SELECT 1')",
        "WAITFOR DELAY '00:01:00'",
        "DECLARE @x int; SELECT @x",
        "SET NOCOUNT ON",
        "USE master",
        "BEGIN TRAN; SELECT 1",
        "GRANT SELECT ON dbo.PO TO public",
        "DBCC CHECKDB",
        "SELECT * FROM master.sys.databases",
        "SELECT * FROM msdb.dbo.backupset",
        "SELECT * FROM linked.EDS.dbo.PO",
        "SELECT * FROM OtherDb.dbo.PO",  # another database on the same instance
        "SELECT * FROM [OtherDb].[dbo].[PO]",
        "SELECT p.POId FROM dbo.PO p JOIN OtherDb..Vendors v ON 1=1",
        "SELECT xp_cmdshell('dir')",
        "SELECT 1 /* unterminated",
        "SELECT 'unterminated",
        "",
        "   ;  ",
    ],
)
def test_rejects_non_read_queries(sql):
    res = guard.check(sql)
    assert not res.ok, sql
    assert res.errors


def test_rejects_keyword_hidden_after_comment_boundary():
    # A newline ends the line comment, so the DELETE is live code.
    assert not guard.check("SELECT 1 --harmless\nDELETE FROM dbo.PO").ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT Name FROM dbo.Vendors WHERE Name LIKE '%delete%'",  # keyword inside string
        "SELECT [Update] FROM dbo.Vendors",  # keyword as bracketed identifier
        "SELECT Name FROM dbo.Vendors -- drop table later",  # keyword in a comment
        "SELECT DateUpdated, IsDeleted FROM dbo.PO",  # keyword as part of a word
        "SELECT REPLACE(Name, 'a', 'b') FROM dbo.Vendors",
        "SELECT v.Name FROM dbo.Vendors v WITH (NOLOCK) WHERE v.Active = 1",
        "SELECT r.RequisitionId, x.n FROM dbo.Requisitions r CROSS APPLY "
        "(SELECT COUNT(*) n FROM dbo.Detail d WHERE d.RequisitionId = r.RequisitionId) x",
        "SELECT a FROM t UNION ALL SELECT b FROM u",
        "SELECT STUFF((SELECT ',' + Name FROM dbo.Vendors FOR XML PATH('')), 1, 1, '')",
        "SELECT Name FROM dbo.Vendors;",
    ],
)
def test_allows_read_queries(sql):
    res = guard.check(sql)
    assert res.ok, res.errors


def test_injects_top_when_missing():
    res = guard.check("SELECT Name FROM dbo.Vendors ORDER BY Name")
    assert res.sql == "SELECT TOP (100) Name FROM dbo.Vendors ORDER BY Name"
    assert res.top_applied == 100


def test_injects_top_after_distinct():
    res = guard.check("select distinct Name from dbo.Vendors")
    assert res.sql == "select distinct TOP (100) Name from dbo.Vendors"


def test_injects_top_into_outer_select_of_cte_only():
    res = guard.check("WITH v AS (SELECT VendorId FROM dbo.Vendors) SELECT * FROM v")
    assert res.sql == "WITH v AS (SELECT VendorId FROM dbo.Vendors) SELECT TOP (100) * FROM v"
    # CTE names aren't reported as tables.
    assert [t.name for t in res.tables] == ["Vendors"]


def test_keeps_smaller_top_and_clamps_larger_top():
    assert guard.check("SELECT TOP 10 * FROM dbo.PO").sql == "SELECT TOP 10 * FROM dbo.PO"
    res = guard.check("SELECT TOP (50000) * FROM dbo.PO")
    assert res.sql == "SELECT TOP (100) * FROM dbo.PO"
    assert any("clamped" in w for w in res.warnings)


def test_union_and_fetch_rely_on_executor_cap():
    res = guard.check("SELECT a FROM t UNION SELECT b FROM u")
    assert res.ok and res.top_applied is None and res.warnings
    res = guard.check("SELECT a FROM t ORDER BY a OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY")
    assert res.ok and "TOP" not in res.sql


def test_collects_tables():
    res = guard.check(
        "SELECT * FROM dbo.PO p JOIN archive.PO a ON a.POId = p.POId JOIN Vendors v ON 1=1"
    )
    assert sorted((t.db, t.name) for t in res.tables) == [("", "Vendors"), ("archive", "PO"), ("dbo", "PO")]


def test_scrub():
    assert "delete" not in scrub("SELECT 'it''s delete' /* drop /* nested */ */ -- x\n, [a]]b]").lower()
    with pytest.raises(GuardError):
        scrub("SELECT [unterminated")
