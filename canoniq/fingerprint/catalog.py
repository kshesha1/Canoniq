"""Iceberg catalog adapter: snapshot resolution by business as-of date,
field-id-based column-name normalization across schema evolution, and the
join graph used for dimension resolution and Tier-3 locality bounds."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import Table as IcebergTable
from pyiceberg.types import (
    DecimalType,
    DoubleType,
    FloatType,
    IntegerType,
    LongType,
    StringType,
)

logger = logging.getLogger(__name__)

AS_OF_SNAPSHOT_PROPERTY = "canoniq.as_of"
CATALOG_DB_NAME = "catalog.db"

_NUMERIC_TYPES = (DoubleType, FloatType, DecimalType, LongType, IntegerType)


class SnapshotNotFoundError(RuntimeError):
    """No snapshot within the allowed window of the requested as-of date.
    Raised loudly — fingerprinting against the wrong snapshot silently
    would be worse than failing."""


def open_catalog(
    warehouse_dir: str | Path, name: str = "canoniq", create: bool = False
) -> SqlCatalog:
    """Open a local SQLite-cataloged Iceberg warehouse directory
    (a directory containing catalog.db plus table data)."""
    warehouse_dir = Path(warehouse_dir)
    db = warehouse_dir / CATALOG_DB_NAME
    if not db.exists() and not create:
        raise FileNotFoundError(f"no Iceberg catalog at {db}")
    return SqlCatalog(
        name, uri=f"sqlite:///{db}", warehouse=f"file://{warehouse_dir}"
    )


class IcebergCatalogAdapter:
    """Read-side view over one Iceberg namespace, with caching."""

    def __init__(self, catalog: SqlCatalog, namespace: str | None = None,
                 snapshot_window_days: int = 3):
        self.catalog = catalog
        if namespace is None:
            namespaces = [ns for (ns,) in catalog.list_namespaces()]
            if len(namespaces) != 1:
                raise ValueError(
                    f"namespace must be given when the catalog has {namespaces}"
                )
            namespace = namespaces[0]
        self.namespace = namespace
        self.snapshot_window_days = snapshot_window_days
        self._tables: dict[str, IcebergTable] = {}
        self._arrow_cache: dict[tuple[str, date], pa.Table] = {}
        self._distinct_cache: dict[tuple[str, str, date], list[str]] = {}

    # -- structure -------------------------------------------------------

    def table_names(self) -> list[str]:
        return sorted(name for (_, name) in self.catalog.list_tables(self.namespace))

    def load(self, table_name: str) -> IcebergTable:
        if table_name not in self._tables:
            self._tables[table_name] = self.catalog.load_table(
                f"{self.namespace}.{table_name}"
            )
        return self._tables[table_name]

    def columns(self, table_name: str) -> dict[str, str]:
        """column name -> 'numeric' | 'string' | 'other' (current schema)."""
        out = {}
        for field in self.load(table_name).schema().fields:
            if isinstance(field.field_type, _NUMERIC_TYPES):
                out[field.name] = "numeric"
            elif isinstance(field.field_type, StringType):
                out[field.name] = "string"
            else:
                out[field.name] = "other"
        return out

    def numeric_columns(self) -> list[tuple[str, str]]:
        return [
            (table, col)
            for table in self.table_names()
            for col, kind in self.columns(table).items()
            if kind == "numeric"
        ]

    def string_columns(self, table_name: str) -> list[str]:
        return [c for c, k in self.columns(table_name).items() if k == "string"]

    def join_edges(self) -> list[tuple[str, str, str, str]]:
        """(table_a, col, table_b, col) for columns shared by name across
        tables — the brownfield stand-in for declared FK edges. DDL-declared
        FKs, when ingested, take the same shape."""
        by_column: dict[str, list[str]] = {}
        for table in self.table_names():
            for col in self.columns(table):
                by_column.setdefault(col, []).append(table)
        edges = []
        for col, tables in by_column.items():
            for i, t1 in enumerate(tables):
                for t2 in tables[i + 1 :]:
                    edges.append((t1, col, t2, col))
        return edges

    def joined_tables(self, table_name: str) -> list[tuple[str, str, str]]:
        """(other_table, local_col, other_col) one FK-hop away."""
        out = []
        for t1, c1, t2, c2 in self.join_edges():
            if t1 == table_name:
                out.append((t2, c1, c2))
            elif t2 == table_name:
                out.append((t1, c2, c1))
        return out

    def schema_renames(self, table_name: str) -> dict[str, str]:
        """Historical column name -> current name, via Iceberg field ids.
        Schema-evolution history is a usable signal, and it also lets the
        executor read pre-rename snapshots under current names."""
        table = self.load(table_name)
        current = {f.field_id: f.name for f in table.schema().fields}
        renames = {}
        for schema in table.metadata.schemas:
            for field in schema.fields:
                new_name = current.get(field.field_id)
                if new_name and new_name != field.name:
                    renames[field.name] = new_name
        return renames

    # -- snapshots ---------------------------------------------------------

    def resolve_snapshot(self, table_name: str, as_of: date):
        """Latest snapshot whose business as-of (snapshot property, falling
        back to commit timestamp) is within the allowed window."""
        table = self.load(table_name)
        window = timedelta(days=self.snapshot_window_days)
        match = None
        for snap in table.snapshots():
            prop = snap.summary.additional_properties.get(AS_OF_SNAPSHOT_PROPERTY)
            snap_date = (
                date.fromisoformat(prop)
                if prop
                else datetime.fromtimestamp(snap.timestamp_ms / 1000).date()
            )
            if abs(snap_date - as_of) <= window:
                match = snap
        if match is None:
            raise SnapshotNotFoundError(
                f"{self.namespace}.{table_name}: no snapshot within "
                f"±{self.snapshot_window_days} days of {as_of}"
            )
        return match

    def arrow_for(self, table_name: str, as_of: date) -> pa.Table:
        """Snapshot-scoped scan with historical column names normalized to
        the current schema (so a query written against today's names reads
        pre-rename snapshots correctly)."""
        key = (table_name, as_of)
        if key not in self._arrow_cache:
            table = self.load(table_name)
            snap = self.resolve_snapshot(table_name, as_of)
            arrow = table.scan(snapshot_id=snap.snapshot_id).to_arrow()
            renames = self.schema_renames(table_name)
            if renames:
                arrow = arrow.rename_columns(
                    [renames.get(n, n) for n in arrow.column_names]
                )
            self._arrow_cache[key] = arrow
        return self._arrow_cache[key]

    def distinct_values(self, table_name: str, column: str, as_of: date) -> list[str]:
        key = (table_name, column, as_of)
        if key not in self._distinct_cache:
            arrow = self.arrow_for(table_name, as_of)
            if column not in arrow.column_names:
                self._distinct_cache[key] = []
            else:
                values = arrow.column(column).unique().to_pylist()
                self._distinct_cache[key] = [str(v) for v in values if v is not None]
        return self._distinct_cache[key]
