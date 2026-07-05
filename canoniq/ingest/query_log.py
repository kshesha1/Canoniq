"""Query log connector — file mode (v0) and Snowflake QUERY_HISTORY (week 2)."""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from canoniq.ingest.base import Connector, RawQuery, TableSchema

logger = logging.getLogger(__name__)

_PLACEHOLDER = "?"


def _strip_comment_lines(text: str) -> str:
    """Drop standalone `--` comment lines so they don't get glued onto the
    following statement when splitting on `;`."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def _split_statements(text: str) -> list[str]:
    """Split a flat SQL file into individual statements (semicolon-separated)."""
    text = _strip_comment_lines(text)
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def _strip_literals(tree: exp.Expr) -> exp.Expr:
    """Replace every literal value with a placeholder so structurally
    identical queries normalize to the same shape regardless of the
    concrete string/numeric values used."""

    def _replace(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Literal):
            return exp.Literal.string(_PLACEHOLDER)
        return node

    return tree.transform(_replace)


def parameterized_hash(sql: str, dialect: str = "duckdb") -> str | None:
    """Parse `sql`, strip literals, and hash the resulting query shape.

    Returns None if `sql` is not syntactically valid SQL — callers should
    skip such statements rather than raise, since a query log may contain
    noise.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None
    normalized = _strip_literals(tree).sql(dialect=dialect)
    return hashlib.sha256(normalized.encode()).hexdigest()


class QueryLogFileConnector(Connector):
    """Reads a flat .sql query log file, one statement per line or
    semicolon-separated, and groups statements by parameterized shape.

    v0 file mode has no user attribution, so `distinct_users` is always 1.
    """

    def __init__(self, path: str, dialect: str = "duckdb"):
        self.path = path
        self.dialect = dialect

    def get_query_log(self) -> list[RawQuery]:
        text = Path(self.path).read_text()
        statements = _split_statements(text)

        groups: dict[str, list[str]] = {}
        for stmt in statements:
            shape_hash = parameterized_hash(stmt, dialect=self.dialect)
            if shape_hash is None:
                logger.warning("Skipping unparseable statement in %s: %s", self.path, stmt)
                continue
            groups.setdefault(shape_hash, []).append(stmt)

        now = datetime.now(UTC).isoformat()
        return [
            RawQuery(
                sql=stmts[0],
                execution_count=len(stmts),
                distinct_users=1,
                last_executed_at=now,
                source="query_log",
            )
            for stmts in groups.values()
        ]

    def get_schemas(self) -> list[TableSchema]:
        raise NotImplementedError(
            "QueryLogFileConnector only reads query logs; "
            "use canoniq.ingest.warehouse for schema introspection."
        )

    def watch(self, callback: Callable[[Any], None]) -> None:
        raise NotImplementedError(
            "QueryLogFileConnector does not support watching; "
            "use canoniq.ingest.watcher.SignalWatcher."
        )
