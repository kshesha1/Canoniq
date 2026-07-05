"""OntoRank — 5-signal trust scorer for metric evidence."""

from dataclasses import dataclass
from datetime import UTC, datetime
from math import log

from canoniq.config import OntoRankWeights
from canoniq.mining.evidence_bundle import MetricEvidence

SOURCE_AUTHORITY: dict[str, float] = {
    # from SourceType enum (canoniq/ingest/base.py) -- keep in sync
    "numeric_fingerprint": 0.98,
    "tableau_calc": 0.85,
    "dbt_metric": 1.00,
    "dbt_model": 0.85,
    "data_dictionary": 0.85,
    "ddl_constraint": 0.75,
    "looker_measure": 0.80,
    "tableau_field": 0.78,
    "brd_approved": 0.90,
    "brd_draft": 0.65,
    "excel_named": 0.70,
    "excel_formula": 0.50,
    "pdf_report": 0.55,
    "confluence_page": 0.60,
    "ddl_naming_convention": 0.45,
    "query_log_complex": 0.60,
    "query_log_simple": 0.40,
    "ad_hoc": 0.20,
}


@dataclass
class OntoRankScore:
    total: float                   # 0.0-1.0 weighted sum
    source_authority: float
    usage_frequency: float
    cross_source_agreement: float
    recency: float
    certification_status: float
    evidence_summary: str          # human-readable justification


def _parse_last_seen(last_seen_at: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _source_authority(evidence: MetricEvidence) -> float:
    if not evidence.source_types:
        return 0.0
    return max(SOURCE_AUTHORITY.get(s, 0.0) for s in evidence.source_types)


def _usage_frequency(evidence: MetricEvidence, max_execution_count: int) -> float:
    if max_execution_count <= 0:
        return 0.0
    return log(1 + evidence.execution_count) / log(1 + max_execution_count)


def _cross_source_agreement(evidence: MetricEvidence) -> float:
    return min(len(evidence.source_types) / 4.0, 1.0)


def _recency(evidence: MetricEvidence, now: datetime) -> float:
    last_seen = _parse_last_seen(evidence.last_seen_at)
    if last_seen is None:
        return 0.0

    days_since = (now - last_seen).days
    if days_since <= 7:
        return 1.0
    if days_since <= 30:
        return 0.8
    if days_since <= 90:
        return 0.5
    if days_since <= 365:
        return 0.2
    return 0.0


def _certification_status(evidence: MetricEvidence) -> float:
    return 1.0 if evidence.is_certified else 0.0


def _evidence_summary(
    evidence: MetricEvidence,
    source_authority: float,
    usage_frequency: float,
    recency: float,
) -> str:
    sources = ", ".join(evidence.source_types) if evidence.source_types else "no sources"
    certified = "certified" if evidence.is_certified else "not certified"
    return (
        f"{sources} ({certified}); {evidence.execution_count} executions "
        f"across {evidence.distinct_users} distinct users; last seen {evidence.last_seen_at}; "
        f"source_authority={source_authority:.2f}, usage_frequency={usage_frequency:.2f}, "
        f"recency={recency:.2f}"
    )


def score(
    evidence: MetricEvidence,
    weights: OntoRankWeights,
    max_execution_count: int,
    now: datetime | None = None,
) -> OntoRankScore:
    """Compute OntoRank trust score for a metric candidate."""
    now = now or datetime.now(UTC)

    source_authority = _source_authority(evidence)
    usage_frequency = _usage_frequency(evidence, max_execution_count)
    cross_source_agreement = _cross_source_agreement(evidence)
    recency = _recency(evidence, now)
    certification_status = _certification_status(evidence)

    total = (
        source_authority * weights.source_authority
        + usage_frequency * weights.usage_frequency
        + cross_source_agreement * weights.cross_source_agreement
        + recency * weights.recency
        + certification_status * weights.certification_status
    )

    return OntoRankScore(
        total=total,
        source_authority=source_authority,
        usage_frequency=usage_frequency,
        cross_source_agreement=cross_source_agreement,
        recency=recency,
        certification_status=certification_status,
        evidence_summary=_evidence_summary(evidence, source_authority, usage_frequency, recency),
    )
