"""Static checks that run *before* any SQL reaches the database.

Defense in depth - this is one of four layers, and none of them trusts the
prompt:

1. this guard: lexical keyword deny-list + T-SQL parse that must yield exactly
   one read-only query, plus hard ``TOP (N)`` injection;
2. the executor: ``SET ROWCOUNT``, ``fetchmany`` cap, query/lock timeouts;
3. the login: ``db_datareader`` only, verified at startup (see executor);
4. the connection: ``ApplicationIntent=ReadOnly`` where an AG secondary exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

MAX_SQL_CHARS = 20_000

# Anything that writes, changes schema/permissions/session state, runs code,
# or reaches outside the database. Matched as whole words on SQL with
# comments, string literals and [bracketed] identifiers removed.
FORBIDDEN_KEYWORDS = (
    "INSERT UPDATE DELETE MERGE UPSERT DROP ALTER CREATE TRUNCATE RENAME "
    "EXEC EXECUTE CALL GRANT REVOKE DENY "
    "BACKUP RESTORE DBCC SHUTDOWN KILL RECONFIGURE CHECKPOINT "
    "BULK OPENROWSET OPENQUERY OPENDATASOURCE OPENXML "
    "WAITFOR USE SET DECLARE INTO GO "
    "BEGIN COMMIT ROLLBACK TRAN TRANSACTION "
    "READTEXT WRITETEXT UPDATETEXT"
).split()
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", re.I)
_PROC_RE = re.compile(r"\b(sp|xp)_\w+", re.I)
BLOCKED_DATABASES = frozenset({"master", "msdb", "tempdb", "model"})

_READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class GuardError(ValueError):
    pass


@dataclass
class GuardResult:
    ok: bool
    sql: str  # possibly rewritten (TOP injected / clamped)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[exp.Table] = field(default_factory=list)
    top_applied: int | None = None

    def raise_for_errors(self) -> None:
        if not self.ok:
            raise GuardError("; ".join(self.errors))


def scrub(sql: str) -> str:
    """Blank out comments, string literals and delimited identifiers.

    Keeps the string length stable-ish (not required) and raises on
    unterminated constructs, which are a classic smuggling trick.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
        elif c == "/" and nxt == "*":
            depth, i = 1, i + 2  # T-SQL block comments nest
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise GuardError("unterminated /* comment")
            out.append(" ")
        elif c in "'[\"":
            close = {"'": "'", "[": "]", '"': '"'}[c]
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j == -1:
                    raise GuardError(f"unterminated {c} literal/identifier")
                if sql.startswith(close * 2, j):  # escaped '' / ]] / ""
                    j += 2
                    continue
                break
            out.append(" '' " if c == "'" else " _ident_ ")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class SqlGuard:
    def __init__(self, max_rows: int = 1000):
        self.max_rows = max_rows

    def check(self, sql: str) -> GuardResult:
        res = GuardResult(ok=False, sql=sql.strip().rstrip(";").strip())
        if not res.sql:
            res.errors.append("empty query")
            return res
        if len(res.sql) > MAX_SQL_CHARS:
            res.errors.append(f"query longer than {MAX_SQL_CHARS} characters")
            return res

        # --- layer 1a: lexical deny-list --------------------------------
        try:
            scrubbed = scrub(res.sql)
        except GuardError as e:
            res.errors.append(str(e))
            return res
        bad = sorted({m.group(1).upper() for m in _FORBIDDEN_RE.finditer(scrubbed)})
        if bad:
            res.errors.append(
                "only single read-only SELECT queries are allowed; found forbidden keyword(s): "
                + ", ".join(bad)
            )
        if _PROC_RE.search(scrubbed):
            res.errors.append("calling system/extended procedures (sp_*/xp_*) is not allowed")
        if ";" in scrubbed:
            res.errors.append("multiple statements are not allowed")
        if res.errors:
            return res

        # --- layer 1b: parse -------------------------------------------
        try:
            statements = [s for s in sqlglot.parse(res.sql, read="tsql") if s is not None]
        except (ParseError, TokenError) as e:
            res.errors.append(f"could not parse as T-SQL ({str(e).splitlines()[0]}); simplify the query")
            return res
        if len(statements) != 1:
            res.errors.append("exactly one statement is allowed")
            return res
        tree = statements[0]
        if not isinstance(tree, _READ_ONLY_ROOTS):
            res.errors.append(f"statement type {type(tree).__name__} is not allowed; SELECT only")
            return res
        if any(True for _ in tree.find_all(exp.Into)) or any(True for _ in tree.find_all(exp.Command)):
            res.errors.append("SELECT ... INTO and embedded commands are not allowed")
            return res

        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if not t.name:
                res.errors.append("table-valued / rowset functions in FROM are not allowed")
                continue
            if isinstance(t.this, exp.Dot) or (t.catalog and "." in t.catalog):
                res.errors.append(f"linked-server (4-part) name not allowed: {t.sql('tsql')}")
                continue
            if t.catalog and t.catalog.lower() in BLOCKED_DATABASES:
                res.errors.append(f"system database access not allowed: {t.catalog}")
                continue
            if t.name.lower() in cte_names and not t.db:
                continue
            res.tables.append(t)
        if res.errors:
            return res

        # --- row cap ----------------------------------------------------
        self._apply_top(res, tree)
        res.ok = True
        return res

    # ------------------------------------------------------------------
    def _apply_top(self, res: GuardResult, tree: exp.Expression) -> None:
        cap = self.max_rows
        if not isinstance(tree, exp.Select):
            res.warnings.append(
                f"set operation (UNION/EXCEPT/INTERSECT): TOP not injected; result capped at {cap} rows by the executor"
            )
            return
        limit = tree.args.get("limit")
        if isinstance(limit, exp.Fetch) or tree.args.get("offset") is not None:
            res.warnings.append(f"OFFSET/FETCH present: result capped at {cap} rows by the executor")
            return

        toks = sqlglot.tokenize(res.sql, read="tsql")
        depth, select_idx = 0, []
        for idx, tok in enumerate(toks):
            if tok.token_type == TokenType.L_PAREN:
                depth += 1
            elif tok.token_type == TokenType.R_PAREN:
                depth -= 1
            elif tok.token_type == TokenType.SELECT and depth == 0:
                select_idx.append(idx)
        if len(select_idx) != 1:
            res.warnings.append(f"could not locate outer SELECT; result capped at {cap} rows by the executor")
            return
        i = select_idx[0]

        if isinstance(limit, exp.Limit):
            options = limit.args.get("limit_options")
            n = limit.expression
            if options is not None and options.args.get("percent"):
                res.warnings.append(f"TOP ... PERCENT: result capped at {cap} rows by the executor")
                return
            if isinstance(n, exp.Literal) and not n.is_string:
                value = int(float(n.this))
                if value <= cap:
                    res.top_applied = value
                    return
                # Clamp the literal in place: find the first number after TOP.
                for tok in toks[i + 1:]:
                    if tok.token_type == TokenType.NUMBER:
                        res.sql = res.sql[: tok.start] + str(cap) + res.sql[tok.end + 1:]
                        res.top_applied = cap
                        res.warnings.append(f"TOP {value} clamped to TOP {cap}")
                        return
            res.warnings.append(f"non-literal TOP: result capped at {cap} rows by the executor")
            return

        # No TOP: insert after SELECT [ALL | DISTINCT].
        anchor = toks[i]
        if i + 1 < len(toks) and toks[i + 1].token_type in (TokenType.DISTINCT, TokenType.ALL):
            anchor = toks[i + 1]
        res.sql = f"{res.sql[: anchor.end + 1]} TOP ({cap}){res.sql[anchor.end + 1:]}"
        res.top_applied = cap
