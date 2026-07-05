from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from canoniq.config import Config, OntoRankWeights
from canoniq.ingest.base import RawQuery, TableSchema
from canoniq.ingest.query_log import QueryLogFileConnector
from canoniq.ingest.warehouse import DuckDBWarehouseConnector
from canoniq.mining.evidence_bundle import EvidenceBundle, build_evidence_bundle
from canoniq.mining.signal_classifier import SignalClass, classify_sql
from canoniq.mining.sql_extractor import extract_candidates
from canoniq.proposer.llm import build_system_prompt, propose, validate_proposal
from canoniq.proposer.models import (
    EntityProposal,
    EvidenceItem,
    MetricProposal,
    SemanticModelProposal,
)
from canoniq.ranking.ontorank import OntoRankScore, score

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_schema.sql"
QUERIES_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_queries.sql"


class _FakeStructuredClient:
    """Stands in for an Instructor-patched Anthropic client. Returns a
    pre-built proposal regardless of prompt contents, so tests never hit a
    real API."""

    def __init__(self, canned_proposal: SemanticModelProposal):
        self._canned_proposal = canned_proposal
        self.messages = self
        self.last_call_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> SemanticModelProposal:
        self.last_call_kwargs = kwargs
        return self._canned_proposal


@pytest.fixture(scope="module")
def schemas(tmp_path_factory: pytest.TempPathFactory) -> dict[str, TableSchema]:
    db_path = tmp_path_factory.mktemp("proposer") / "tpcds.db"
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_FIXTURE.read_text())
    con.close()

    connector = DuckDBWarehouseConnector(str(db_path))
    return {s.fully_qualified_name: s for s in connector.get_schemas()}


@pytest.fixture(scope="module")
def store_sales_bundle(schemas: dict[str, TableSchema]) -> EvidenceBundle:
    connector = QueryLogFileConnector(str(QUERIES_FIXTURE))
    raw_queries: list[RawQuery] = connector.get_query_log()

    all_aggs, all_dims, all_joins = [], [], []
    for query in raw_queries:
        if classify_sql(query.sql) != SignalClass.ANALYTICAL:
            continue
        aggs, dims, joins = extract_candidates(query, schemas)
        all_aggs.extend(aggs)
        all_dims.extend(dims)
        all_joins.extend(joins)

    return build_evidence_bundle(schemas["main.store_sales"], all_aggs, all_dims, all_joins)


@pytest.fixture(scope="module")
def scored_metrics(
    store_sales_bundle: EvidenceBundle,
) -> list[tuple[Any, OntoRankScore]]:
    weights = OntoRankWeights()
    max_exec = max((m.execution_count for m in store_sales_bundle.metric_candidates), default=1)
    return [
        (m, score(m, weights, max_execution_count=max_exec))
        for m in store_sales_bundle.metric_candidates
    ]


@pytest.fixture
def base_config() -> Config:
    return Config(
        project_name="test",
        warehouse_type="duckdb",
        output_formats=["metricflow"],
        output_dir="./out",
    )


def _valid_metric(name: str, expression: str) -> MetricProposal:
    return MetricProposal(
        name=name,
        description="A metric.",
        expression=expression,
        metric_type="sum",
        synonyms=["revenue", "sales"],
        evidence=[
            EvidenceItem(
                source="query_log", description="mined", execution_count=10, trust_contribution=0.5
            )
        ],
        trust_score=0.9,
    )


def test_build_system_prompt_contains_critical_rules() -> None:
    prompt = build_system_prompt()
    assert "NEVER invent column names" in prompt
    assert "NEVER invent table names" in prompt
    assert "sum | count | average | ratio | derived" in prompt


def test_metric_proposal_name_must_be_snake_case() -> None:
    _valid_metric("total_revenue", "SUM(ss_net_profit)")  # should not raise
    with pytest.raises(ValidationError):
        _valid_metric("TotalRevenue", "SUM(ss_net_profit)")
    with pytest.raises(ValidationError):
        _valid_metric("total revenue", "SUM(ss_net_profit)")


def test_propose_grounds_prompt_in_real_schema_evidence(
    store_sales_bundle: EvidenceBundle,
    scored_metrics: list[tuple[Any, OntoRankScore]],
    base_config: Config,
) -> None:
    canned = SemanticModelProposal(
        dataset_name="store_sales_model",
        source_table="store_sales",
        grain_description="One row per store sale line item",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[_valid_metric("total_net_profit", "SUM(ss_net_profit)")],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )
    client = _FakeStructuredClient(canned)

    result = propose(store_sales_bundle, scored_metrics, base_config, client=client)

    assert client.last_call_kwargs is not None
    prompt = client.last_call_kwargs["messages"][0]["content"]
    assert "ss_net_profit" in prompt  # real evidence made it into the prompt
    assert "=== TABLE SCHEMA ===" in prompt
    assert "=== METRIC EVIDENCE" in prompt
    assert result.metrics[0].name == "total_net_profit"


def test_propose_uses_configured_model_and_temperature(
    store_sales_bundle: EvidenceBundle,
    scored_metrics: list[tuple[Any, OntoRankScore]],
    base_config: Config,
) -> None:
    canned = SemanticModelProposal(
        dataset_name="d",
        source_table="store_sales",
        grain_description="g",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[],
        joins=[],
        overall_trust_score=0.0,
        review_required=False,
    )
    client = _FakeStructuredClient(canned)

    propose(store_sales_bundle, scored_metrics, base_config, client=client)

    assert client.last_call_kwargs["model"] == base_config.llm_model
    assert client.last_call_kwargs["temperature"] == 0
    assert client.last_call_kwargs["response_model"] is SemanticModelProposal


def test_validate_proposal_drops_hallucinated_metric_column(
    schemas: dict[str, TableSchema],
) -> None:
    proposal = SemanticModelProposal(
        dataset_name="d",
        source_table="store_sales",
        grain_description="g",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[
            _valid_metric("real_metric", "SUM(ss_net_profit)"),
            _valid_metric("fake_metric", "SUM(ss_totally_made_up_column)"),
        ],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )

    result = validate_proposal(proposal, schemas["main.store_sales"])

    assert [m.name for m in result.metrics] == ["real_metric"]
    assert result.review_required is True


def test_validate_proposal_drops_hallucinated_dimension_and_entity(
    schemas: dict[str, TableSchema],
) -> None:
    from canoniq.proposer.models import DimensionProposal

    proposal = SemanticModelProposal(
        dataset_name="d",
        source_table="store_sales",
        grain_description="g",
        primary_key=[],
        entities=[
            EntityProposal(
                name="store_sale",
                column="ss_ticket_number",
                table="store_sales",
                entity_type="primary",
                description="ticket",
            ),
            EntityProposal(
                name="fake_entity",
                column="nonexistent_column",
                table="store_sales",
                entity_type="foreign",
                description="fake",
            ),
        ],
        dimensions=[
            DimensionProposal(
                name="store",
                column="ss_store_sk",
                table="store_sales",
                is_time=False,
                description="store",
                synonyms=["location"],
            ),
            DimensionProposal(
                name="fake_dim",
                column="nonexistent_column",
                table="store_sales",
                is_time=False,
                description="fake",
                synonyms=[],
            ),
        ],
        metrics=[],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )

    result = validate_proposal(proposal, schemas["main.store_sales"])

    assert [e.name for e in result.entities] == ["store_sale"]
    assert [d.name for d in result.dimensions] == ["store"]
    assert result.review_required is True


def test_validate_proposal_keeps_review_required_false_when_nothing_dropped(
    schemas: dict[str, TableSchema],
) -> None:
    proposal = SemanticModelProposal(
        dataset_name="d",
        source_table="store_sales",
        grain_description="g",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[_valid_metric("real_metric", "SUM(ss_net_profit)")],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )

    result = validate_proposal(proposal, schemas["main.store_sales"])

    assert len(result.metrics) == 1
    assert result.review_required is False


def test_no_invented_columns_survive_end_to_end(
    store_sales_bundle: EvidenceBundle,
    scored_metrics: list[tuple[Any, OntoRankScore]],
    base_config: Config,
    schemas: dict[str, TableSchema],
) -> None:
    """The proposer's overall guarantee: whatever the LLM returns, no
    hallucinated column ever survives into the final proposal."""
    canned = SemanticModelProposal(
        dataset_name="store_sales_model",
        source_table="store_sales",
        grain_description="One row per store sale line item",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[
            _valid_metric("total_net_profit", "SUM(ss_net_profit)"),
            _valid_metric("distinct_customers", "COUNT(DISTINCT ss_customer_sk)"),
            _valid_metric("hallucinated", "SUM(ss_this_column_does_not_exist)"),
        ],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )
    client = _FakeStructuredClient(canned)
    known_columns = {c.name.lower() for c in schemas["main.store_sales"].columns}

    result = propose(store_sales_bundle, scored_metrics, base_config, client=client)

    assert len(result.metrics) == 2
    for metric in result.metrics:
        referenced = {
            tok.strip("(),").lower()
            for tok in metric.expression.replace("(", " ").replace(")", " ").split()
            if tok.strip("(),").lower() in known_columns
        }
        assert referenced  # each surviving metric does reference a real column
    assert "hallucinated" not in [m.name for m in result.metrics]
    assert result.review_required is True
