"""LLM proposer — grounds Claude in real schema evidence, then asks it to
propose a complete semantic model via Instructor's structured output.
"""

import logging
from typing import Any, Protocol

import anthropic
import instructor
import sqlglot
from sqlglot import exp

from canoniq.config import Config
from canoniq.ingest.base import TableSchema
from canoniq.mining.evidence_bundle import EvidenceBundle, MetricEvidence
from canoniq.proposer.models import SemanticModelProposal
from canoniq.ranking.ontorank import OntoRankScore

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096


class StructuredClient(Protocol):
    """Minimal shape of an Instructor-patched Anthropic client, so tests can
    inject a fake without needing a real API key."""

    messages: Any


def build_system_prompt() -> str:
    return """
You are a semantic layer architect. Your job is to propose named, described
metric and dimension definitions from SQL evidence.

CRITICAL RULES:
1. NEVER invent column names. Only use column names from the provided schema.
2. NEVER invent table names. Only use table names from the provided schema.
3. ALL expressions must use only columns confirmed to exist in the schema.
4. Descriptions must be plain English. No technical jargon. No "This metric".
5. Synonyms must reflect how business users actually talk about this number
   in meetings — not technical aliases.
6. If evidence is ambiguous or conflicting, surface the conflict in the
   conflicts field with both alternatives ranked by trust score.
7. metric_type must be one of: sum | count | average | ratio | derived.

You are generating YAML that will be compiled by the dbt MetricFlow compiler.
Output must be parseable and correct.
"""


def _format_column(col: Any) -> str:
    flags = [] if col.is_nullable else ["NOT NULL"]
    if col.cardinality_approx is not None:
        flags.append("high cardinality" if col.cardinality_approx > 100 else "low cardinality")
    flag_str = f", {', '.join(flags)}" if flags else ""
    sample = f" — sample: {col.sample_values[:5]}" if col.sample_values else ""
    return f"  - {col.name} ({col.data_type}{flag_str}){sample}"


def _format_table_schema(table: TableSchema) -> str:
    grain = table.row_count_approx if table.row_count_approx is not None else "unknown"
    lines = [
        f"Table: {table.fully_qualified_name}",
        f"Grain: {grain} rows",
        "Columns:",
    ]
    lines.extend(_format_column(col) for col in table.columns)
    return "\n".join(lines)


def _format_metric_evidence(
    scored_metrics: list[tuple[MetricEvidence, OntoRankScore]],
) -> str:
    lines = []
    for i, (evidence, ontorank) in enumerate(scored_metrics, start=1):
        lines.append(f"{i}. {evidence.expression} — trust: {ontorank.total:.2f}")
        sources = ", ".join(evidence.source_types) if evidence.source_types else "none"
        lines.append(
            f"   Sources: {sources} ({evidence.execution_count} executions, "
            f"{evidence.distinct_users} distinct users)"
        )
        lines.append(f"   Last seen: {evidence.last_seen_at}")
        lines.append(f"   Filter variants: {evidence.filter_variants}")
    return "\n".join(lines)


def _format_dimension_evidence(bundle: EvidenceBundle) -> str:
    time_dims = [d for d in bundle.dimension_candidates if d.is_time]
    categorical_dims = [d for d in bundle.dimension_candidates if not d.is_time]

    def _fmt(dims: list[Any]) -> str:
        return ", ".join(f"{d.column} ({len(d.seen_in_queries)} queries)" for d in dims) or "none"

    return f"Time dimensions: {_fmt(time_dims)}\nCategorical: {_fmt(categorical_dims)}"


def _format_join_evidence(bundle: EvidenceBundle) -> str:
    if not bundle.join_candidates:
        return "none"
    return "\n".join(
        f"{j.from_table}.{j.from_column} -> {j.to_table}.{j.to_column} "
        f"({len(j.seen_in_queries)} occurrences, {j.join_type} JOIN)"
        for j in bundle.join_candidates
    )


def _build_user_prompt(
    bundle: EvidenceBundle,
    scored_metrics: list[tuple[MetricEvidence, OntoRankScore]],
    drop_threshold: float,
) -> str:
    """Build the grounded user prompt.

    Only metric candidates above the drop threshold are included, ranked by
    trust score descending — the LLM never sees noise-tier evidence.
    """
    ranked = sorted(
        (sm for sm in scored_metrics if sm[1].total > drop_threshold),
        key=lambda sm: sm[1].total,
        reverse=True,
    )

    return f"""=== TABLE SCHEMA ===
{_format_table_schema(bundle.table)}

=== METRIC EVIDENCE (ranked by trust score) ===
{_format_metric_evidence(ranked)}

=== DIMENSION EVIDENCE ===
{_format_dimension_evidence(bundle)}

=== JOIN EVIDENCE ===
{_format_join_evidence(bundle)}

=== YOUR TASK ===
Propose a complete semantic model for this table. For each metric:
- Give it a clear snake_case name
- Write a plain-English description (include caveats if filter variants differ)
- Use ONLY the column names shown above
- List 2-5 synonyms business users would use
- If you see conflicting definitions, surface them in conflicts[]
"""


def _columns_referenced(expression: str, dialect: str = "duckdb") -> set[str]:
    """Best-effort extraction of column identifiers from a bare SQL
    expression (e.g. "SUM(total_amount)"), by wrapping it in a SELECT."""
    try:
        tree = sqlglot.parse_one(f"SELECT {expression}", dialect=dialect)
    except Exception:
        return set()
    return {c.name.lower() for c in tree.find_all(exp.Column)}


def validate_proposal(
    proposal: SemanticModelProposal, table: TableSchema
) -> SemanticModelProposal:
    """Anti-hallucination guardrail on the LLM's OUTPUT: drop any metric,
    dimension, or entity that references a column not present in the real
    warehouse schema. Mirrors the guardrail already applied to the LLM's
    INPUT evidence in the SQL extractor (Step 6)."""
    known_columns = {col.name.lower() for col in table.columns}

    kept_metrics = []
    for metric in proposal.metrics:
        invented = _columns_referenced(metric.expression) - known_columns
        if invented:
            logger.warning(
                "Dropping proposed metric %r: references unknown column(s) %s",
                metric.name,
                sorted(invented),
            )
            continue
        kept_metrics.append(metric)

    kept_dimensions = []
    for dimension in proposal.dimensions:
        if dimension.column.lower() not in known_columns:
            logger.warning(
                "Dropping proposed dimension %r: unknown column %r",
                dimension.name,
                dimension.column,
            )
            continue
        kept_dimensions.append(dimension)

    kept_entities = []
    for entity in proposal.entities:
        if entity.column.lower() not in known_columns:
            logger.warning(
                "Dropping proposed entity %r: unknown column %r", entity.name, entity.column
            )
            continue
        kept_entities.append(entity)

    dropped_any = (
        len(kept_metrics) != len(proposal.metrics)
        or len(kept_dimensions) != len(proposal.dimensions)
        or len(kept_entities) != len(proposal.entities)
    )

    return proposal.model_copy(
        update={
            "metrics": kept_metrics,
            "dimensions": kept_dimensions,
            "entities": kept_entities,
            "review_required": proposal.review_required or dropped_any,
        }
    )


def propose(
    evidence_bundle: EvidenceBundle,
    scored_metrics: list[tuple[MetricEvidence, OntoRankScore]],
    config: Config,
    client: StructuredClient | None = None,
) -> SemanticModelProposal:
    """Ground the LLM in real schema evidence, then propose the semantic
    model. Validates the response against the schema before returning it —
    any hallucinated column is dropped, never emitted downstream."""
    resolved_client: Any = client or instructor.from_anthropic(anthropic.Anthropic())

    prompt = _build_user_prompt(
        evidence_bundle, scored_metrics, config.ontorank_thresholds.drop
    )

    proposal = resolved_client.messages.create(
        model=config.llm_model,
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
        response_model=SemanticModelProposal,
    )

    return validate_proposal(proposal, evidence_bundle.table)


def _build_repair_prompt(yaml_output: str, validation_errors: list[str]) -> str:
    errors_text = "\n".join(f"- {e}" for e in validation_errors)
    return (
        "The following YAML failed dbt MetricFlow validation with these "
        "errors. Fix only the failing definitions. Keep everything else.\n\n"
        f"=== ERRORS ===\n{errors_text}\n\n"
        f"=== CURRENT YAML ===\n{yaml_output}\n"
    )


def repair_proposal(
    proposal: SemanticModelProposal,
    yaml_output: str,
    validation_errors: list[str],
    config: Config,
    client: StructuredClient | None = None,
) -> SemanticModelProposal:
    """Re-invoke the LLM with the failing YAML and validator errors, asking
    it to fix only the failing definitions. Used by the validation loop's
    repair node (Step 11)."""
    resolved_client: Any = client or instructor.from_anthropic(anthropic.Anthropic())

    prompt = _build_repair_prompt(yaml_output, validation_errors)

    return resolved_client.messages.create(
        model=config.llm_model,
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
        response_model=SemanticModelProposal,
    )
