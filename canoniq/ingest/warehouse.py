"""Warehouse schema introspection connector (DuckDB for v0, Snowflake next)."""

import re
from collections.abc import Callable
from typing import Any

import duckdb

from canoniq.ingest.base import ColumnSchema, Connector, RawQuery, TableSchema

MAX_SAMPLE_VALUES = 5

_ID_HEURISTIC = re.compile(r"_id$")

_NUMBER_TYPES = (
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "HUGEINT",
    "UINTEGER",
    "UBIGINT",
    "USMALLINT",
    "UTINYINT",
    "DECIMAL",
    "NUMERIC",
    "DOUBLE",
    "FLOAT",
    "REAL",
)
_TIME_TYPES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
_BOOLEAN_TYPES = ("BOOLEAN", "BOOL")


def normalize_type(data_type: str) -> str:
    """Normalize a warehouse-native type string to string | number | time | boolean."""
    upper = data_type.upper()
    if upper.startswith(_BOOLEAN_TYPES):
        return "boolean"
    if upper.startswith(_NUMBER_TYPES):
        return "number"
    if upper.startswith(_TIME_TYPES):
        return "time"
    return "string"


class DuckDBWarehouseConnector(Connector):
    """Introspects a DuckDB database's schema via information_schema."""

    def __init__(self, path: str, schema: str = "main"):
        self.path = path
        self.schema = schema
        self._con = duckdb.connect(path)

    def get_schemas(self) -> list[TableSchema]:
        tables = self._con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = ? AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            [self.schema],
        ).fetchall()

        return [self._build_table_schema(name) for (name,) in tables]

    def _build_table_schema(self, table_name: str) -> TableSchema:
        columns_raw = self._con.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [self.schema, table_name],
        ).fetchall()

        row_count_approx = self._row_count(table_name)

        columns = [
            ColumnSchema(
                name=col_name,
                data_type=normalize_type(data_type),
                is_nullable=(is_nullable == "YES"),
                sample_values=self._sample_values(table_name, col_name),
                cardinality_approx=self._cardinality(table_name, col_name),
            )
            for col_name, data_type, is_nullable in columns_raw
        ]

        primary_keys = self._primary_keys_from_constraints(table_name)
        if not primary_keys:
            primary_keys = self._primary_keys_from_heuristic(columns, row_count_approx)

        return TableSchema(
            fully_qualified_name=f"{self.schema}.{table_name}",
            columns=columns,
            primary_keys=primary_keys,
            row_count_approx=row_count_approx,
        )

    def _primary_keys_from_constraints(self, table_name: str) -> list[str]:
        rows = self._con.execute(
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "  AND tc.table_name = kcu.table_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "  AND tc.table_schema = ? AND tc.table_name = ? "
            "ORDER BY kcu.ordinal_position",
            [self.schema, table_name],
        ).fetchall()
        return [r[0] for r in rows]

    def _primary_keys_from_heuristic(
        self, columns: list[ColumnSchema], row_count_approx: int | None
    ) -> list[str]:
        """Fall back to `*_id` columns whose cardinality matches the row count."""
        if not row_count_approx:
            return []
        candidates = []
        for col in columns:
            if not _ID_HEURISTIC.search(col.name):
                continue
            if col.cardinality_approx is not None and col.cardinality_approx >= row_count_approx:
                candidates.append(col.name)
        return candidates

    def _row_count(self, table_name: str) -> int | None:
        try:
            row = self._con.execute(
                f'SELECT COUNT(*) FROM "{self.schema}"."{table_name}"'
            ).fetchone()
            return int(row[0]) if row is not None else None
        except duckdb.Error:
            return None

    def _sample_values(self, table_name: str, column_name: str) -> list[Any]:
        try:
            rows = self._con.execute(
                f'SELECT DISTINCT "{column_name}" FROM "{self.schema}"."{table_name}" '
                f'WHERE "{column_name}" IS NOT NULL LIMIT {MAX_SAMPLE_VALUES}'
            ).fetchall()
            return [r[0] for r in rows]
        except duckdb.Error:
            return []

    def _cardinality(self, table_name: str, column_name: str) -> int | None:
        try:
            row = self._con.execute(
                f'SELECT approx_count_distinct("{column_name}") '
                f'FROM "{self.schema}"."{table_name}"'
            ).fetchone()
            return int(row[0]) if row is not None else None
        except duckdb.Error:
            return None

    def get_query_log(self) -> list[RawQuery]:
        raise NotImplementedError(
            "DuckDBWarehouseConnector only introspects schema; "
            "use canoniq.ingest.query_log for query log ingestion."
        )

    def watch(self, callback: Callable[[Any], None]) -> None:
        raise NotImplementedError(
            "DuckDBWarehouseConnector does not support watching; "
            "use canoniq.ingest.watcher.SignalWatcher."
        )
