import pytest
import yaml

from canoniq.emitters.metricflow import emit_metricflow
from canoniq.emitters.osi import emit_osi
from canoniq.proposer.models import (
    DimensionProposal,
    EntityProposal,
    EvidenceItem,
    MetricProposal,
    SemanticModelProposal,
)


def _metric(
    name: str,
    expression: str,
    trust_score: float,
    metric_type: str = "sum",
    synonyms: list[str] | None = None,
) -> MetricProposal:
    return MetricProposal(
        name=name,
        description=f"Description for {name}.",
        expression=expression,
        metric_type=metric_type,
        synonyms=synonyms or ["alias one", "alias two"],
        evidence=[
            EvidenceItem(
                source="query_log_simple",
                description="mined",
                execution_count=42,
                trust_contribution=0.3,
            )
        ],
        trust_score=trust_score,
    )


@pytest.fixture
def proposal() -> SemanticModelProposal:
    return SemanticModelProposal(
        dataset_name="orders",
        source_table="orders",
        grain_description="One row per customer order",
        primary_key=["order_id"],
        entities=[
            EntityProposal(
                name="order_id",
                column="order_id",
                table="orders",
                entity_type="primary",
                description="Unique order identifier",
            ),
            EntityProposal(
                name="customer",
                column="customer_id",
                table="orders",
                entity_type="foreign",
                description="Customer who placed the order",
            ),
        ],
        dimensions=[
            DimensionProposal(
                name="order_date",
                column="order_date",
                table="orders",
                is_time=True,
                description="Date the order was placed",
                synonyms=[],
            ),
            DimensionProposal(
                name="status",
                column="status",
                table="orders",
                is_time=False,
                description="Order status",
                synonyms=[],
            ),
        ],
        metrics=[
            _metric("total_revenue", "SUM(total_amount)", trust_score=0.92),
            _metric(
                "order_count", "COUNT(DISTINCT order_id)", trust_score=0.42, metric_type="count"
            ),
        ],
        joins=[
            {
                "from_table": "orders",
                "to_table": "customers",
                "from_column": "customer_id",
                "to_column": "customer_id",
                "join_type": "LEFT",
            }
        ],
        overall_trust_score=0.91,
        review_required=True,
    )


class TestMetricFlowEmitter:
    def test_output_is_valid_yaml(self, proposal: SemanticModelProposal) -> None:
        output = emit_metricflow(proposal, generated_at="2026-07-01T14:32:00Z")
        parsed = yaml.safe_load(output)
        assert "semantic_models" in parsed
        assert "metrics" in parsed

    def test_semantic_model_structure(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_metricflow(proposal))
        model = parsed["semantic_models"][0]
        assert model["name"] == "orders"
        assert model["description"] == "One row per customer order"
        assert model["model"] == "ref('orders')"

        entity_names = {e["name"] for e in model["entities"]}
        assert entity_names == {"order_id", "customer"}
        primary = next(e for e in model["entities"] if e["name"] == "order_id")
        assert primary["type"] == "primary"
        assert primary["expr"] == "order_id"

    def test_time_dimension_has_type_params(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_metricflow(proposal))
        dims = {d["name"]: d for d in parsed["semantic_models"][0]["dimensions"]}
        assert dims["order_date"]["type"] == "time"
        assert dims["order_date"]["type_params"] == {"time_granularity": "day"}
        assert dims["status"]["type"] == "categorical"
        assert "type_params" not in dims["status"]

    def test_measures_generated_from_metric_expressions(
        self, proposal: SemanticModelProposal
    ) -> None:
        parsed = yaml.safe_load(emit_metricflow(proposal))
        measures = {m["name"]: m for m in parsed["semantic_models"][0]["measures"]}
        assert measures["total_amount_sum"]["agg"] == "sum"
        assert measures["total_amount_sum"]["expr"] == "total_amount"
        assert measures["total_amount_sum"]["create_metric"] is False
        assert measures["order_id_count_distinct"]["agg"] == "count_distinct"

    def test_metrics_sorted_by_trust_score_descending(
        self, proposal: SemanticModelProposal
    ) -> None:
        parsed = yaml.safe_load(emit_metricflow(proposal))
        names = [m["name"] for m in parsed["metrics"]]
        assert names == ["total_revenue", "order_count"]

    def test_metric_meta_evidence_card(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_metricflow(proposal))
        metric = next(m for m in parsed["metrics"] if m["name"] == "total_revenue")
        assert metric["meta"]["canoniq_trust_score"] == 0.92
        assert "query_log_simple" in metric["meta"]["canoniq_evidence"]
        assert metric["meta"]["canoniq_synonyms"] == ["alias one", "alias two"]
        assert metric["type"] == "simple"
        assert metric["type_params"]["measure"] == "total_amount_sum"

    def test_review_required_comment_below_auto_merge_threshold(
        self, proposal: SemanticModelProposal
    ) -> None:
        output = emit_metricflow(proposal, auto_merge_threshold=0.85)
        lines = output.split("\n")
        order_count_idx = next(i for i, line in enumerate(lines) if "name: order_count" in line)
        assert "REVIEW REQUIRED" in lines[order_count_idx - 1]

        total_revenue_idx = next(
            i for i, line in enumerate(lines) if "name: total_revenue" in line
        )
        assert "REVIEW REQUIRED" not in lines[total_revenue_idx - 1]

    def test_header_includes_trust_score_and_generated_timestamp(
        self, proposal: SemanticModelProposal
    ) -> None:
        output = emit_metricflow(proposal, generated_at="2026-07-01T14:32:00Z")
        assert "# Trust score: 0.91" in output
        assert "2026-07-01T14:32:00Z" in output
        assert "# Auto-generated by canoniq" in output

    def test_non_simple_aggregation_falls_back_to_derived(self) -> None:
        proposal = SemanticModelProposal(
            dataset_name="orders",
            source_table="orders",
            grain_description="orders",
            primary_key=[],
            entities=[],
            dimensions=[],
            metrics=[
                _metric(
                    "profit_margin",
                    "SUM(profit) / SUM(revenue)",
                    trust_score=0.7,
                    metric_type="ratio",
                )
            ],
            joins=[],
            overall_trust_score=0.7,
            review_required=False,
        )
        parsed = yaml.safe_load(emit_metricflow(proposal))
        metric = parsed["metrics"][0]
        assert metric["type"] == "derived"
        assert metric["type_params"]["expr"] == "SUM(profit) / SUM(revenue)"

    def test_count_star_produces_row_count_measure(self) -> None:
        proposal = SemanticModelProposal(
            dataset_name="orders",
            source_table="orders",
            grain_description="orders",
            primary_key=[],
            entities=[],
            dimensions=[],
            metrics=[_metric("total_orders", "COUNT(*)", trust_score=0.7, metric_type="count")],
            joins=[],
            overall_trust_score=0.7,
            review_required=False,
        )
        parsed = yaml.safe_load(emit_metricflow(proposal))
        measure = parsed["semantic_models"][0]["measures"][0]
        assert measure["agg"] == "count"
        assert measure["expr"] == "1"


class TestOSIEmitter:
    def test_output_is_valid_yaml(self, proposal: SemanticModelProposal) -> None:
        output = emit_osi(proposal)
        parsed = yaml.safe_load(output)
        assert parsed["version"] == "0.1.1"
        assert "semantic_model" in parsed

    def test_schema_ref_comment_present(self, proposal: SemanticModelProposal) -> None:
        output = emit_osi(proposal)
        assert output.startswith("# yaml-language-server: $schema=")

    def test_dataset_structure(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_osi(proposal))
        model = parsed["semantic_model"][0]
        dataset = model["datasets"][0]
        assert dataset["name"] == "orders"
        assert dataset["primary_key"] == ["order_id"]
        assert dataset["description"] == "One row per customer order"

    def test_fields_combine_entities_and_dimensions(
        self, proposal: SemanticModelProposal
    ) -> None:
        parsed = yaml.safe_load(emit_osi(proposal))
        dataset = parsed["semantic_model"][0]["datasets"][0]
        field_names = {f["name"] for f in dataset["fields"]}
        assert field_names == {"order_id", "customer", "order_date", "status"}

        order_date = next(f for f in dataset["fields"] if f["name"] == "order_date")
        assert order_date["dimension"]["is_time"] is True
        assert (
            order_date["expression"]["dialects"][0]["expression"] == "order_date"
        )

    def test_relationships_from_joins(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_osi(proposal))
        relationships = parsed["semantic_model"][0]["relationships"]
        assert relationships == [
            {"from": "orders.customer_id", "to": "customers.customer_id", "type": "LEFT"}
        ]

    def test_malformed_join_is_skipped(self) -> None:
        proposal = SemanticModelProposal(
            dataset_name="orders",
            source_table="orders",
            grain_description="orders",
            primary_key=[],
            entities=[],
            dimensions=[],
            metrics=[],
            joins=[{"weird": "shape"}],
            overall_trust_score=0.5,
            review_required=False,
        )
        parsed = yaml.safe_load(emit_osi(proposal))
        assert parsed["semantic_model"][0]["relationships"] == []

    def test_metrics_structure(self, proposal: SemanticModelProposal) -> None:
        parsed = yaml.safe_load(emit_osi(proposal))
        metric = next(
            m for m in parsed["semantic_model"][0]["metrics"] if m["name"] == "total_revenue"
        )
        assert metric["expression"] == [{"dialect": "ANSI_SQL", "expression": "SUM(total_amount)"}]
        assert metric["ai_context"]["synonyms"] == ["alias one", "alias two"]

    def test_ai_context_instructions_mention_source_table(
        self, proposal: SemanticModelProposal
    ) -> None:
        parsed = yaml.safe_load(emit_osi(proposal))
        instructions = parsed["semantic_model"][0]["ai_context"]["instructions"]
        assert "orders" in instructions
