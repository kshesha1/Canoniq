from pathlib import Path

import duckdb
import pytest

from canoniq.ingest.base import RawQuery, TableSchema
from canoniq.ingest.query_log import QueryLogFileConnector
from canoniq.ingest.warehouse import DuckDBWarehouseConnector
from canoniq.mining.evidence_bundle import EvidenceBundle, build_evidence_bundle
from canoniq.mining.signal_classifier import SignalClass, classify_sql
from canoniq.mining.sql_extractor import (
    AggregationCandidate,
    DimensionCandidate,
    JoinCandidate,
    extract_candidates,
)

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_schema.sql"
QUERIES_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_queries.sql"


def _raw_query(sql: str, execution_count: int = 1, distinct_users: int = 1) -> RawQuery:
    return RawQuery(
        sql=sql,
        execution_count=execution_count,
        distinct_users=distinct_users,
        last_executed_at="2026-07-01T00:00:00+00:00",
        source="query_log",
    )


@pytest.fixture(scope="module")
def schemas(tmp_path_factory: pytest.TempPathFactory) -> dict[str, TableSchema]:
    db_path = tmp_path_factory.mktemp("evidence") / "tpcds.db"
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_FIXTURE.read_text())
    con.close()

    connector = DuckDBWarehouseConnector(str(db_path))
    return {s.fully_qualified_name: s for s in connector.get_schemas()}


@pytest.fixture(scope="module")
def mined_candidates(
    schemas: dict[str, TableSchema],
) -> tuple[list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]]:
    """Run the full mining pipeline (query log -> classify -> extract) across
    all 99 synthetic TPC-DS-style queries."""
    connector = QueryLogFileConnector(str(QUERIES_FIXTURE))
    raw_queries = connector.get_query_log()

    all_aggs: list[AggregationCandidate] = []
    all_dims: list[DimensionCandidate] = []
    all_joins: list[JoinCandidate] = []
    for query in raw_queries:
        if classify_sql(query.sql) != SignalClass.ANALYTICAL:
            continue
        aggs, dims, joins = extract_candidates(query, schemas)
        all_aggs.extend(aggs)
        all_dims.extend(dims)
        all_joins.extend(joins)

    return all_aggs, all_dims, all_joins


def test_store_sales_has_more_than_5_metric_candidates(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)

    assert isinstance(bundle, EvidenceBundle)
    assert len(bundle.metric_candidates) > 5
    assert all(m.execution_count > 1 for m in bundle.metric_candidates)


def test_metric_expressions_are_alias_independent_and_deduped(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)

    expressions = [m.expression for m in bundle.metric_candidates]
    assert len(expressions) == len(set(expressions))  # no duplicate groups
    assert "SUM(ss_net_profit)" in expressions
    assert "COUNT(DISTINCT ss_customer_sk)" in expressions
    assert "COUNT(*)" in expressions


def test_execution_counts_are_summed_across_merged_candidates(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)

    sum_net_profit = next(
        m for m in bundle.metric_candidates if m.expression == "SUM(ss_net_profit)"
    )
    # shape A in the fixture: 12 repeats with a year filter
    assert sum_net_profit.execution_count >= 12


def test_filter_variants_include_none_and_filtered(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)

    avg_price = next(m for m in bundle.metric_candidates if m.expression == "AVG(ss_sales_price)")
    assert None in avg_price.filter_variants


def test_bundle_excludes_other_tables(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)
    assert all(m.source_table == "store_sales" for m in bundle.metric_candidates)
    assert all(d.table == "store_sales" for d in bundle.dimension_candidates)
    assert all(
        j.from_table == "store_sales" or j.to_table == "store_sales"
        for j in bundle.join_candidates
    )


def test_join_direction_and_dedup(
    schemas: dict[str, TableSchema],
    mined_candidates: tuple[
        list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]
    ],
) -> None:
    aggs, dims, joins = mined_candidates
    bundle = build_evidence_bundle(schemas["main.store_sales"], aggs, dims, joins)

    date_dim_joins = [j for j in bundle.join_candidates if j.to_table == "date_dim"]
    assert len(date_dim_joins) == 1  # deduped despite appearing in 2 distinct query shapes
    assert date_dim_joins[0].from_table == "store_sales"
    assert date_dim_joins[0].from_column == "ss_sold_date_sk"
    assert date_dim_joins[0].to_column == "d_date_sk"
    assert len(date_dim_joins[0].seen_in_queries) == 2


def test_dbt_certification_marks_is_certified_and_adds_source_type() -> None:
    schemas_local = {"orders": _orders_table_schema()}
    agg_candidates = [
        AggregationCandidate(
            expression="SUM(o.total_amount)",
            source_table="orders",
            source_column="total_amount",
            agg_function="SUM",
            filter_expr=None,
            seen_in_queries=["q1"],
            execution_count=10,
            distinct_users=2,
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
    ]
    dbt_metrics = [{"expression": "SUM(total_amount)", "table": "orders", "name": "total_revenue"}]

    bundle = build_evidence_bundle(
        schemas_local["orders"], agg_candidates, [], [], dbt_metrics=dbt_metrics
    )

    assert len(bundle.metric_candidates) == 1
    metric = bundle.metric_candidates[0]
    assert metric.is_certified is True
    assert "dbt_metric" in metric.source_types


def test_no_dbt_metrics_means_not_certified() -> None:
    schemas_local = {"orders": _orders_table_schema()}
    agg_candidates = [
        AggregationCandidate(
            expression="SUM(o.total_amount)",
            source_table="orders",
            source_column="total_amount",
            agg_function="SUM",
            filter_expr=None,
            seen_in_queries=["q1"],
            execution_count=10,
            distinct_users=2,
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
    ]

    bundle = build_evidence_bundle(
        schemas_local["orders"], agg_candidates, [], [], dbt_metrics=None
    )

    assert bundle.metric_candidates[0].is_certified is False
    assert "dbt_metric" not in bundle.metric_candidates[0].source_types


def test_query_complexity_classification_affects_source_types() -> None:
    schemas_local = {"orders": _orders_table_schema()}
    # A single query mining 2 distinct aggregations -> "complex"
    agg_candidates = [
        AggregationCandidate(
            expression="SUM(o.total_amount)",
            source_table="orders",
            source_column="total_amount",
            agg_function="SUM",
            filter_expr=None,
            seen_in_queries=["complex_q"],
            execution_count=1,
            distinct_users=1,
            last_seen_at="2026-06-01T00:00:00+00:00",
        ),
        AggregationCandidate(
            expression="COUNT(o.order_id)",
            source_table="orders",
            source_column="order_id",
            agg_function="COUNT",
            filter_expr=None,
            seen_in_queries=["complex_q"],
            execution_count=1,
            distinct_users=1,
            last_seen_at="2026-06-01T00:00:00+00:00",
        ),
        AggregationCandidate(
            expression="AVG(o.total_amount)",
            source_table="orders",
            source_column="total_amount",
            agg_function="AVG",
            filter_expr=None,
            seen_in_queries=["simple_q"],
            execution_count=1,
            distinct_users=1,
            last_seen_at="2026-06-01T00:00:00+00:00",
        ),
    ]

    bundle = build_evidence_bundle(schemas_local["orders"], agg_candidates, [], [])

    by_expr = {m.expression: m for m in bundle.metric_candidates}
    assert by_expr["SUM(total_amount)"].source_types == ["query_log_complex"]
    assert by_expr["COUNT(order_id)"].source_types == ["query_log_complex"]
    assert by_expr["AVG(total_amount)"].source_types == ["query_log_simple"]


def _orders_table_schema() -> TableSchema:
    from canoniq.ingest.base import ColumnSchema

    return TableSchema(
        fully_qualified_name="main.orders",
        columns=[
            ColumnSchema(
                name="order_id",
                data_type="number",
                is_nullable=False,
                sample_values=[1, 2],
                cardinality_approx=2,
            ),
            ColumnSchema(
                name="total_amount",
                data_type="number",
                is_nullable=True,
                sample_values=[10.0, 20.0],
                cardinality_approx=2,
            ),
        ],
        primary_keys=["order_id"],
        row_count_approx=2,
    )
