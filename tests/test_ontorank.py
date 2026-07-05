from datetime import UTC, datetime, timedelta

import pytest

from canoniq.config import OntoRankWeights
from canoniq.mining.evidence_bundle import MetricEvidence
from canoniq.ranking.ontorank import OntoRankScore, score

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _evidence(
    execution_count: int = 10,
    distinct_users: int = 1,
    last_seen_at: str = "2026-07-01T00:00:00+00:00",
    source_types: list[str] | None = None,
    is_certified: bool = False,
    filter_variants: list[str | None] | None = None,
) -> MetricEvidence:
    return MetricEvidence(
        expression="SUM(x)",
        source_table="t",
        execution_count=execution_count,
        distinct_users=distinct_users,
        last_seen_at=last_seen_at,
        source_types=["query_log_simple"] if source_types is None else source_types,
        is_certified=is_certified,
        filter_variants=filter_variants or [None],
    )


def test_certified_well_used_metric_scores_above_auto_merge_threshold() -> None:
    evidence = _evidence(
        execution_count=100,
        source_types=["dbt_metric", "looker", "tableau", "query_log_complex"],
        is_certified=True,
        last_seen_at=NOW.isoformat(),
    )
    result = score(evidence, OntoRankWeights(), max_execution_count=100, now=NOW)
    assert result.total > 0.85


def test_single_run_ad_hoc_scores_below_review_threshold() -> None:
    evidence = _evidence(
        execution_count=1,
        source_types=["ad_hoc"],
        is_certified=False,
        last_seen_at=NOW.isoformat(),
    )
    result = score(evidence, OntoRankWeights(), max_execution_count=100, now=NOW)
    assert result.total < 0.50


def test_source_authority_takes_max_across_source_types() -> None:
    evidence = _evidence(source_types=["ad_hoc", "dbt_metric", "slack"])
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.source_authority == 1.00


def test_source_authority_unknown_source_type_contributes_zero() -> None:
    evidence = _evidence(source_types=["some_future_source"])
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.source_authority == 0.0


def test_source_authority_no_sources_is_zero() -> None:
    evidence = _evidence(source_types=[])
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.source_authority == 0.0


def test_usage_frequency_is_log_normalized_and_monotonic() -> None:
    weights = OntoRankWeights()
    low = score(_evidence(execution_count=1), weights, max_execution_count=1000, now=NOW)
    mid = score(_evidence(execution_count=100), weights, max_execution_count=1000, now=NOW)
    high = score(_evidence(execution_count=1000), weights, max_execution_count=1000, now=NOW)
    assert 0.0 <= low.usage_frequency < mid.usage_frequency < high.usage_frequency <= 1.0
    assert high.usage_frequency == pytest.approx(1.0)


def test_usage_frequency_not_100x_for_1000x_more_executions() -> None:
    # log-normalization means a 1000-execution metric shouldn't dominate a
    # 10-execution one by 100x the way raw ratios would suggest.
    weights = OntoRankWeights()
    low = score(_evidence(execution_count=10), weights, max_execution_count=1000, now=NOW)
    high = score(_evidence(execution_count=1000), weights, max_execution_count=1000, now=NOW)
    assert high.usage_frequency / max(low.usage_frequency, 1e-9) < 10


def test_usage_frequency_zero_max_execution_count_is_safe() -> None:
    result = score(_evidence(execution_count=0), OntoRankWeights(), max_execution_count=0, now=NOW)
    assert result.usage_frequency == 0.0


def test_cross_source_agreement_capped_at_one() -> None:
    evidence = _evidence(source_types=["dbt_metric", "looker", "tableau", "notion", "slack"])
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.cross_source_agreement == 1.0


def test_cross_source_agreement_divides_by_four() -> None:
    evidence = _evidence(source_types=["dbt_metric", "looker"])
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.cross_source_agreement == pytest.approx(0.5)


@pytest.mark.parametrize(
    "days_ago,expected",
    [
        (0, 1.0),
        (7, 1.0),
        (8, 0.8),
        (30, 0.8),
        (31, 0.5),
        (90, 0.5),
        (91, 0.2),
        (365, 0.2),
        (366, 0.0),
    ],
)
def test_recency_thresholds(days_ago: int, expected: float) -> None:
    last_seen = (NOW - timedelta(days=days_ago)).isoformat()
    evidence = _evidence(last_seen_at=last_seen)
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.recency == expected


def test_recency_unparseable_timestamp_is_zero() -> None:
    evidence = _evidence(last_seen_at="not-a-date")
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert result.recency == 0.0


def test_certification_status_boolean_mapping() -> None:
    weights = OntoRankWeights()
    certified = score(_evidence(is_certified=True), weights, max_execution_count=10, now=NOW)
    uncertified = score(_evidence(is_certified=False), weights, max_execution_count=10, now=NOW)
    assert certified.certification_status == 1.0
    assert uncertified.certification_status == 0.0


def test_total_is_exact_weighted_sum() -> None:
    weights = OntoRankWeights()
    evidence = _evidence(
        execution_count=50,
        source_types=["looker"],
        is_certified=True,
        last_seen_at=NOW.isoformat(),
    )
    result = score(evidence, weights, max_execution_count=100, now=NOW)

    expected_total = (
        result.source_authority * weights.source_authority
        + result.usage_frequency * weights.usage_frequency
        + result.cross_source_agreement * weights.cross_source_agreement
        + result.recency * weights.recency
        + result.certification_status * weights.certification_status
    )
    assert result.total == pytest.approx(expected_total)


def test_evidence_summary_is_non_empty_and_mentions_sources() -> None:
    evidence = _evidence(source_types=["dbt_metric", "looker"], is_certified=True)
    result = score(evidence, OntoRankWeights(), max_execution_count=10, now=NOW)
    assert isinstance(result, OntoRankScore)
    assert "dbt_metric" in result.evidence_summary
    assert "looker" in result.evidence_summary
