from pathlib import Path

import duckdb
import pytest

from canoniq.ingest.base import RawQuery, TableSchema
from canoniq.ingest.warehouse import DuckDBWarehouseConnector
from canoniq.mining.sql_extractor import extract_candidates

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_schema.sql"


def _raw_query(sql: str, execution_count: int = 1) -> RawQuery:
    return RawQuery(
        sql=sql,
        execution_count=execution_count,
        distinct_users=1,
        last_executed_at="2026-07-01T00:00:00+00:00",
        source="query_log",
    )


@pytest.fixture(scope="module")
def schemas(tmp_path_factory: pytest.TempPathFactory) -> dict[str, TableSchema]:
    db_path = tmp_path_factory.mktemp("extractor") / "tpcds.db"
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_FIXTURE.read_text())
    con.close()

    connector = DuckDBWarehouseConnector(str(db_path))
    return {s.fully_qualified_name: s for s in connector.get_schemas()}


def test_sum_with_join_and_group_by(schemas: dict[str, TableSchema]) -> None:
    sql = (
        "SELECT d_year, SUM(ss_net_profit) AS total_profit FROM store_sales "
        "JOIN date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
        "WHERE d_year = 2001 GROUP BY d_year"
    )
    aggs, dims, joins = extract_candidates(_raw_query(sql, execution_count=12), schemas)

    assert len(aggs) == 1
    agg = aggs[0]
    assert agg.agg_function == "SUM"
    assert agg.source_table == "store_sales"
    assert agg.source_column == "ss_net_profit"
    assert agg.filter_expr == "date_dim.d_year = 2001"
    assert agg.execution_count == 12

    assert len(dims) == 1
    assert (dims[0].table, dims[0].column, dims[0].is_time) == ("date_dim", "d_year", False)

    assert len(joins) == 1
    join = joins[0]
    assert join.from_table == "store_sales"
    assert join.to_table == "date_dim"
    assert join.from_column == "ss_sold_date_sk"
    assert join.to_column == "d_date_sk"
    assert join.join_type == "INNER"


def test_count_distinct_no_join_no_filter(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT COUNT(DISTINCT ss_customer_sk) AS distinct_customers FROM store_sales"
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)

    assert len(aggs) == 1
    assert aggs[0].agg_function == "COUNT_DISTINCT"
    assert aggs[0].source_table == "store_sales"
    assert aggs[0].source_column == "ss_customer_sk"
    assert aggs[0].filter_expr is None
    assert dims == []
    assert joins == []


def test_avg_with_join_and_where_dimension(schemas: dict[str, TableSchema]) -> None:
    sql = (
        "SELECT AVG(ss_sales_price) AS avg_price FROM store_sales "
        "JOIN item ON store_sales.ss_item_sk = item.i_item_sk "
        "WHERE item.i_category = 'Books'"
    )
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)

    assert len(aggs) == 1
    assert aggs[0].agg_function == "AVG"
    assert aggs[0].source_table == "store_sales"
    assert aggs[0].source_column == "ss_sales_price"
    assert aggs[0].filter_expr == "item.i_category = 'Books'"

    assert len(dims) == 1
    assert (dims[0].table, dims[0].column, dims[0].is_time) == ("item", "i_category", False)

    assert len(joins) == 1
    assert joins[0].from_table == "store_sales"
    assert joins[0].to_table == "item"
    assert joins[0].from_column == "ss_item_sk"
    assert joins[0].to_column == "i_item_sk"


def test_count_star_resolves_to_from_table(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT COUNT(*) AS high_profit_orders FROM store_sales WHERE ss_net_profit > 20"
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)

    assert len(aggs) == 1
    assert aggs[0].agg_function == "COUNT"
    assert aggs[0].source_table == "store_sales"
    assert aggs[0].source_column == "*"
    assert aggs[0].filter_expr == "store_sales.ss_net_profit > 20"

    assert len(dims) == 1
    assert (dims[0].table, dims[0].column, dims[0].is_time) == (
        "store_sales",
        "ss_net_profit",
        False,
    )
    assert joins == []


def test_group_by_aliased_join(schemas: dict[str, TableSchema]) -> None:
    sql = (
        "SELECT store.s_state, SUM(store_sales.ss_net_profit) AS state_profit "
        "FROM store_sales JOIN store ON store_sales.ss_store_sk = store.s_store_sk "
        "GROUP BY store.s_state"
    )
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)

    assert len(aggs) == 1
    assert aggs[0].source_table == "store_sales"
    assert aggs[0].source_column == "ss_net_profit"

    assert len(dims) == 1
    assert (dims[0].table, dims[0].column, dims[0].is_time) == ("store", "s_state", False)

    assert len(joins) == 1
    assert joins[0].from_table == "store_sales"
    assert joins[0].to_table == "store"
    assert joins[0].from_column == "ss_store_sk"
    assert joins[0].to_column == "s_store_sk"


def test_time_dimension_flagged(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT d_date, COUNT(*) FROM date_dim GROUP BY d_date"
    _, dims, _ = extract_candidates(_raw_query(sql), schemas)
    assert len(dims) == 1
    assert dims[0].is_time is True


def test_hallucinated_column_is_dropped(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT SUM(ss_fake_column) FROM store_sales"
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)
    assert aggs == []
    assert dims == []
    assert joins == []


def test_hallucinated_table_is_dropped(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT SUM(x) FROM fake_table"
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)
    assert aggs == []
    assert dims == []
    assert joins == []


def test_unparseable_query_returns_empty(schemas: dict[str, TableSchema]) -> None:
    sql = "SELECT 'unterminated"
    aggs, dims, joins = extract_candidates(_raw_query(sql), schemas)
    assert aggs == []
    assert dims == []
    assert joins == []
