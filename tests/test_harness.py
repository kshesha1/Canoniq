from pathlib import Path

import duckdb
import pytest

from canoniq.emitters.metricflow import emit_metricflow
from canoniq.evals.harness import EvalResult, find_closest_metric, report_eval_results, run_eval
from canoniq.evals.tpcds_gold import GOLD_QUERIES, GoldQuery
from canoniq.proposer.models import EvidenceItem, MetricProposal, SemanticModelProposal

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_schema.sql"


def _metric(name: str, expression: str, synonyms: list[str]) -> MetricProposal:
    return MetricProposal(
        name=name,
        description=f"{name.replace('_', ' ')} description",
        expression=expression,
        metric_type="sum",
        synonyms=synonyms,
        evidence=[
            EvidenceItem(
                source="query_log_simple",
                description="mined",
                execution_count=10,
                trust_contribution=0.5,
            )
        ],
        trust_score=0.9,
    )


@pytest.fixture(scope="module")
def warehouse_conn(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    db_path = tmp_path_factory.mktemp("harness") / "tpcds.db"
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_FIXTURE.read_text())
    return con


@pytest.fixture
def store_sales_yaml(tmp_path: Path) -> Path:
    proposal = SemanticModelProposal(
        dataset_name="store_sales",
        source_table="store_sales",
        grain_description="One row per store sale",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[
            _metric(
                "total_net_profit", "SUM(ss_net_profit)", ["total net profit", "profit total"]
            ),
            _metric(
                "distinct_customers",
                "COUNT(DISTINCT ss_customer_sk)",
                ["number of distinct customers", "customer count"],
            ),
            _metric(
                "average_sales_price",
                "AVG(ss_sales_price)",
                ["avg sales price", "average price"],
            ),
            _metric(
                "total_quantity", "SUM(ss_quantity)", ["total quantity sold", "quantity total"]
            ),
            _metric(
                "high_profit_order_count",
                "COUNT(*)",
                ["count of high profit orders", "high profit orders"],
            ),
        ],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )
    yaml_path = tmp_path / "store_sales_metricflow.yml"
    yaml_path.write_text(emit_metricflow(proposal))
    return yaml_path


def test_find_closest_metric_matches_by_synonym_overlap() -> None:
    metrics = [
        {
            "name": "total_net_profit",
            "description": "x",
            "meta": {"canoniq_synonyms": ["total net profit"]},
        },
        {
            "name": "distinct_customers",
            "description": "y",
            "meta": {"canoniq_synonyms": ["customer count"]},
        },
    ]
    match = find_closest_metric("Number of distinct customers", metrics)
    assert match is not None
    assert match["name"] == "distinct_customers"


def test_find_closest_metric_returns_none_when_no_overlap() -> None:
    metrics = [{"name": "total_net_profit", "description": "x", "meta": {"canoniq_synonyms": []}}]
    assert find_closest_metric("completely unrelated question about weather", metrics) is None


def test_run_eval_matches_single_table_gold_queries(
    warehouse_conn: duckdb.DuckDBPyConnection, store_sales_yaml: Path
) -> None:
    # gold queries [1:5] are single-table (no cross-table dimension join
    # needed), so the SQL-translation fallback should match them exactly.
    single_table_queries = GOLD_QUERIES[1:5]

    results = run_eval(str(store_sales_yaml), warehouse_conn, single_table_queries)

    assert len(results) == 4
    assert all(isinstance(r, EvalResult) for r in results)
    failures = [r.error for r in results if not r.result_matches]
    assert all(r.result_matches for r in results), failures


def test_run_eval_reports_clear_error_for_unresolvable_cross_table_dimension(
    warehouse_conn: duckdb.DuckDBPyConnection, store_sales_yaml: Path
) -> None:
    # "Total net profit by year" needs date_dim joined in; the v0 fallback
    # queries store_sales directly and should fail with a clear DB error,
    # not a silent wrong answer.
    year_query = GOLD_QUERIES[0]

    results = run_eval(str(store_sales_yaml), warehouse_conn, [year_query])

    assert len(results) == 1
    assert results[0].result_matches is False
    assert results[0].generated_metric == "total_net_profit"
    assert results[0].error is not None


def test_run_eval_records_error_when_no_metric_matches(
    warehouse_conn: duckdb.DuckDBPyConnection, store_sales_yaml: Path
) -> None:
    unmatched = GoldQuery(
        question="Completely unrelated question with no metric overlap at all",
        sql="SELECT COUNT(*) FROM store_sales",
        expected_metric="nonexistent",
        expected_dimensions=[],
    )

    results = run_eval(str(store_sales_yaml), warehouse_conn, [unmatched])

    assert results[0].result_matches is False
    assert results[0].generated_metric is None
    assert "no matching" in (results[0].error or "")


def test_run_eval_records_error_for_bad_gold_sql(
    warehouse_conn: duckdb.DuckDBPyConnection, store_sales_yaml: Path
) -> None:
    bad_gold = GoldQuery(
        question="Total net profit",
        sql="SELECT SUM(this_column_does_not_exist) FROM store_sales",
        expected_metric="total_net_profit",
        expected_dimensions=[],
    )

    results = run_eval(str(store_sales_yaml), warehouse_conn, [bad_gold])

    assert results[0].result_matches is False
    assert "failed to run gold SQL" in (results[0].error or "")


def test_report_eval_results_computes_accuracy_and_writes_json(tmp_path: Path) -> None:
    results = [
        EvalResult("q1", "sql1", "m1", True, None),
        EvalResult("q2", "sql2", "m2", False, "mismatch"),
        EvalResult("q3", "sql3", None, False, "no match"),
        EvalResult("q4", "sql4", "m4", True, None),
    ]
    output_path = tmp_path / "eval_results.json"

    accuracy = report_eval_results(results, output_path=str(output_path))

    assert accuracy == pytest.approx(0.5)
    assert output_path.exists()

    import json

    saved = json.loads(output_path.read_text())
    assert saved["accuracy"] == pytest.approx(0.5)
    assert len(saved["results"]) == 4


def test_report_eval_results_handles_empty_results(tmp_path: Path) -> None:
    accuracy = report_eval_results([], output_path=str(tmp_path / "eval_results.json"))
    assert accuracy == 0.0


def test_gold_queries_has_ten_entries_covering_varied_patterns() -> None:
    assert len(GOLD_QUERIES) == 10
    agg_functions_used = {q.sql.split("(")[0].split()[-1].upper() for q in GOLD_QUERIES}
    assert {"SUM", "COUNT", "AVG"}.issubset(agg_functions_used)
    assert any(q.expected_dimensions for q in GOLD_QUERIES)
    assert any(not q.expected_dimensions for q in GOLD_QUERIES)
