from pathlib import Path

import duckdb
import pytest

from canoniq.ingest.base import TableSchema
from canoniq.ingest.warehouse import DuckDBWarehouseConnector, normalize_type

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_schema.sql"

EXPECTED_TABLES = {
    "call_center",
    "catalog_page",
    "catalog_returns",
    "catalog_sales",
    "customer",
    "customer_address",
    "customer_demographics",
    "date_dim",
    "household_demographics",
    "income_band",
    "inventory",
    "item",
    "promotion",
    "reason",
    "ship_mode",
    "store",
    "store_returns",
    "store_sales",
    "time_dim",
    "warehouse",
    "web_page",
    "web_returns",
    "web_sales",
    "web_site",
}


@pytest.fixture
def connector(tmp_path: Path) -> DuckDBWarehouseConnector:
    db_path = tmp_path / "tpcds.db"
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_FIXTURE.read_text())
    con.close()
    return DuckDBWarehouseConnector(str(db_path))


def test_normalize_type() -> None:
    assert normalize_type("INTEGER") == "number"
    assert normalize_type("DECIMAL(7,2)") == "number"
    assert normalize_type("BIGINT") == "number"
    assert normalize_type("VARCHAR") == "string"
    assert normalize_type("CHAR(16)") == "string"
    assert normalize_type("DATE") == "time"
    assert normalize_type("TIMESTAMP") == "time"
    assert normalize_type("BOOLEAN") == "boolean"


def test_get_schemas_returns_all_tpcds_tables(connector: DuckDBWarehouseConnector) -> None:
    schemas = connector.get_schemas()
    table_names = {s.fully_qualified_name.split(".")[-1] for s in schemas}
    assert table_names == EXPECTED_TABLES
    assert all(isinstance(s, TableSchema) for s in schemas)


def test_date_dim_primary_key_from_constraint(connector: DuckDBWarehouseConnector) -> None:
    schemas = {s.fully_qualified_name.split(".")[-1]: s for s in connector.get_schemas()}
    date_dim = schemas["date_dim"]
    assert date_dim.primary_keys == ["d_date_sk"]


def test_date_dim_columns_and_types(connector: DuckDBWarehouseConnector) -> None:
    schemas = {s.fully_qualified_name.split(".")[-1]: s for s in connector.get_schemas()}
    date_dim = schemas["date_dim"]
    columns_by_name = {c.name: c for c in date_dim.columns}

    assert columns_by_name["d_date_sk"].data_type == "number"
    assert columns_by_name["d_date_sk"].is_nullable is False
    assert columns_by_name["d_date"].data_type == "time"
    assert columns_by_name["d_day_name"].data_type == "string"


def test_row_count_and_sample_values(connector: DuckDBWarehouseConnector) -> None:
    schemas = {s.fully_qualified_name.split(".")[-1]: s for s in connector.get_schemas()}
    store_sales = schemas["store_sales"]
    assert store_sales.row_count_approx == 4

    ss_item_sk = next(c for c in store_sales.columns if c.name == "ss_item_sk")
    assert set(ss_item_sk.sample_values) == {1, 2, 3}
    assert ss_item_sk.cardinality_approx == 3


def test_store_sales_has_no_declared_pk_but_no_id_column_matches_heuristic(
    connector: DuckDBWarehouseConnector,
) -> None:
    # store_sales has no PRIMARY KEY constraint and its key columns end in
    # `_sk`, not `_id`, so the *_id heuristic correctly finds nothing here.
    schemas = {s.fully_qualified_name.split(".")[-1]: s for s in connector.get_schemas()}
    assert schemas["store_sales"].primary_keys == []


def test_customer_primary_key(connector: DuckDBWarehouseConnector) -> None:
    schemas = {s.fully_qualified_name.split(".")[-1]: s for s in connector.get_schemas()}
    assert schemas["customer"].primary_keys == ["c_customer_sk"]


def test_get_query_log_not_implemented(connector: DuckDBWarehouseConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.get_query_log()


def test_watch_not_implemented(connector: DuckDBWarehouseConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.watch(lambda signal: None)
