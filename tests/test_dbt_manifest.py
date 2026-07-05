from pathlib import Path

import pytest

from canoniq.ingest.base import TableSchema
from canoniq.ingest.dbt_manifest import DbtManifestConnector

MANIFEST_FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.json"


@pytest.fixture
def connector() -> DbtManifestConnector:
    return DbtManifestConnector(str(MANIFEST_FIXTURE))


def test_get_schemas_returns_models(connector: DbtManifestConnector) -> None:
    schemas = connector.get_schemas()
    assert all(isinstance(s, TableSchema) for s in schemas)
    names = {s.fully_qualified_name for s in schemas}
    assert names == {"store_sales", "customer"}


def test_get_schemas_extracts_columns(connector: DbtManifestConnector) -> None:
    schemas = {s.fully_qualified_name: s for s in connector.get_schemas()}
    store_sales = schemas["store_sales"]
    col_names = {c.name for c in store_sales.columns}
    assert col_names == {"ss_net_profit", "ss_customer_sk"}
    net_profit = next(c for c in store_sales.columns if c.name == "ss_net_profit")
    assert net_profit.data_type == "number"


def test_get_dbt_metrics_extracts_expression_and_table(
    connector: DbtManifestConnector,
) -> None:
    metrics = connector.get_dbt_metrics()
    assert len(metrics) == 1
    assert metrics[0] == {
        "name": "total_net_profit",
        "expression": "SUM(ss_net_profit)",
        "table": "store_sales",
    }


def test_dbt_metrics_feed_certification_in_evidence_bundle(
    connector: DbtManifestConnector,
) -> None:
    """The whole point of this connector: its output plugs directly into
    build_evidence_bundle's dbt_metrics parameter to mark mined candidates
    as certified."""
    from canoniq.ingest.base import TableSchema as TS
    from canoniq.mining.evidence_bundle import build_evidence_bundle
    from canoniq.mining.sql_extractor import AggregationCandidate

    table = TS(
        fully_qualified_name="store_sales",
        columns=[],
        primary_keys=[],
        row_count_approx=None,
    )
    agg_candidates = [
        AggregationCandidate(
            expression="SUM(ss_net_profit)",
            source_table="store_sales",
            source_column="ss_net_profit",
            agg_function="SUM",
            filter_expr=None,
            seen_in_queries=["q1"],
            execution_count=10,
            distinct_users=2,
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
    ]

    bundle = build_evidence_bundle(
        table, agg_candidates, [], [], dbt_metrics=connector.get_dbt_metrics()
    )

    assert bundle.metric_candidates[0].is_certified is True
    assert "dbt_metric" in bundle.metric_candidates[0].source_types


def test_get_query_log_not_implemented(connector: DbtManifestConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.get_query_log()


def test_watch_not_implemented(connector: DbtManifestConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.watch(lambda signal: None)
