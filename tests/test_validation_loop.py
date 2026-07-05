from pathlib import Path
from typing import Any

import pytest

from canoniq.config import Config
from canoniq.proposer.models import (
    EntityProposal,
    EvidenceItem,
    MetricProposal,
    SemanticModelProposal,
)
from canoniq.validation.loop import VALIDATION_FAILED_HEADER, build_validation_graph


class _FakeRepairClient:
    """Returns a sequence of canned proposals, one per repair call — lets
    tests simulate an LLM that eventually fixes the reported errors."""

    def __init__(self, proposals: list[SemanticModelProposal]):
        self._proposals = list(proposals)
        self.call_count = 0
        self.messages = self

    def create(self, **kwargs: Any) -> SemanticModelProposal:
        proposal = self._proposals[min(self.call_count, len(self._proposals) - 1)]
        self.call_count += 1
        return proposal


def _metric(
    name: str = "total_net_profit", expression: str = "SUM(ss_net_profit)"
) -> MetricProposal:
    return MetricProposal(
        name=name,
        description="A metric.",
        expression=expression,
        metric_type="sum",
        synonyms=["revenue"],
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


def _valid_proposal() -> SemanticModelProposal:
    return SemanticModelProposal(
        dataset_name="store_sales",
        source_table="store_sales",
        grain_description="One row per store sale",
        primary_key=[],
        entities=[
            EntityProposal(
                name="store_sale",
                column="ss_ticket_number",
                table="store_sales",
                entity_type="primary",
                description="ticket",
            )
        ],
        dimensions=[],
        metrics=[_metric()],
        joins=[],
        overall_trust_score=0.9,
        review_required=False,
    )


def _malformed_proposal() -> SemanticModelProposal:
    """MetricProposal/EntityProposal deliberately leave entity_type/metric_type
    as unconstrained strings (per spec) since the LLM has freedom here — but
    MetricFlow's real schema only accepts primary|foreign|unique. This is a
    realistic LLM mistake the jsonschema structural check should catch."""
    proposal = _valid_proposal()
    bad_entity = proposal.entities[0].model_copy(update={"entity_type": "not_a_real_type"})
    return proposal.model_copy(update={"entities": [bad_entity]})


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        project_name="test",
        warehouse_type="duckdb",
        output_formats=["metricflow"],
        output_dir=str(tmp_path),
        llm_max_retries=3,
    )


def test_valid_proposal_passes_on_first_attempt(config: Config) -> None:
    client = _FakeRepairClient([])  # should never be called
    graph = build_validation_graph(config, client=client)

    result = graph.invoke(
        {
            "proposal": _valid_proposal(),
            "yaml_output": "",
            "validation_errors": [],
            "attempt": 0,
            "passed": False,
        }
    )

    assert result["passed"] is True
    assert result["attempt"] == 1
    assert client.call_count == 0
    assert not result["yaml_output"].startswith(VALIDATION_FAILED_HEADER)


def test_malformed_yaml_triggers_repair_and_corrects_itself(config: Config) -> None:
    client = _FakeRepairClient([_valid_proposal()])
    graph = build_validation_graph(config, client=client)

    result = graph.invoke(
        {
            "proposal": _malformed_proposal(),
            "yaml_output": "",
            "validation_errors": [],
            "attempt": 0,
            "passed": False,
        }
    )

    assert result["passed"] is True
    assert result["attempt"] == 2  # first attempt failed, second (post-repair) passed
    assert client.call_count == 1
    assert not result["yaml_output"].startswith(VALIDATION_FAILED_HEADER)
    assert "entity_type" not in result["yaml_output"]


def test_validation_errors_reference_the_bad_field(config: Config) -> None:
    from canoniq.emitters.metricflow import emit_metricflow
    from canoniq.validation.loop import _structural_validate

    yaml_output = emit_metricflow(_malformed_proposal())
    errors = _structural_validate(yaml_output)

    assert errors  # non-empty
    assert any("entities" in e for e in errors)


def test_retries_exhausted_emits_manual_review_header(config: Config) -> None:
    # The repair client never actually fixes anything, so every attempt fails.
    client = _FakeRepairClient([_malformed_proposal()])
    graph = build_validation_graph(config, client=client)

    result = graph.invoke(
        {
            "proposal": _malformed_proposal(),
            "yaml_output": "",
            "validation_errors": [],
            "attempt": 0,
            "passed": False,
        }
    )

    assert result["passed"] is False
    assert result["attempt"] == config.llm_max_retries
    assert result["yaml_output"].startswith(VALIDATION_FAILED_HEADER)
    # repair is called until the retry cap, not indefinitely
    assert client.call_count == config.llm_max_retries - 1


def test_accept_writes_yaml_to_output_dir(config: Config, tmp_path: Path) -> None:
    client = _FakeRepairClient([])
    graph = build_validation_graph(config, client=client)

    graph.invoke(
        {
            "proposal": _valid_proposal(),
            "yaml_output": "",
            "validation_errors": [],
            "attempt": 0,
            "passed": False,
        }
    )

    output_file = tmp_path / "store_sales_metricflow.yml"
    assert output_file.exists()
    assert "semantic_models" in output_file.read_text()


def test_repair_prompt_includes_errors_and_current_yaml() -> None:
    from canoniq.proposer.llm import _build_repair_prompt

    prompt = _build_repair_prompt("name: broken", ["entities.0.type: 'x' is not one of..."])
    assert "Fix only the failing definitions" in prompt
    assert "entities.0.type" in prompt
    assert "name: broken" in prompt
