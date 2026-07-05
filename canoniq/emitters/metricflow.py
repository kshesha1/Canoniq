"""MetricFlow emitter — converts a SemanticModelProposal into dbt
MetricFlow YAML."""

import logging
from datetime import UTC, datetime
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from canoniq.proposer.models import DimensionProposal, MetricProposal, SemanticModelProposal

logger = logging.getLogger(__name__)

_AGG_NAME_MAP = {
    "SUM": "sum",
    "COUNT": "count",
    "COUNT_DISTINCT": "count_distinct",
    "AVG": "average",
    "MIN": "min",
    "MAX": "max",
}


class CleanDumper(yaml.Dumper):
    """Indents block-sequence items under their parent key (PyYAML's default
    aligns list dashes with the key, which reads as flat/unclean)."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _dump_yaml(data: Any) -> str:
    return yaml.dump(
        data, Dumper=CleanDumper, sort_keys=False, default_flow_style=False, width=100
    )


def _parse_single_agg(expression: str, dialect: str = "duckdb") -> tuple[str, str] | None:
    """Parse a bare aggregation expression (e.g. "SUM(total_amount)") into
    (metricflow_agg_name, column). Returns None if the expression isn't a
    single clean aggregation over one column (e.g. a ratio/derived formula),
    in which case the caller falls back to a "derived" metric."""
    try:
        tree = sqlglot.parse_one(f"SELECT {expression}", dialect=dialect)
    except Exception:
        return None

    agg_funcs = list(tree.find_all(exp.AggFunc))
    if len(agg_funcs) != 1:
        return None

    agg = agg_funcs[0]
    is_count_distinct = isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct)
    agg_name = "COUNT_DISTINCT" if is_count_distinct else type(agg).__name__.upper()
    metricflow_agg = _AGG_NAME_MAP.get(agg_name)
    if metricflow_agg is None:
        return None

    if any(agg.find_all(exp.Star)):
        return metricflow_agg, "*"

    columns = list(agg.find_all(exp.Column))
    if len(columns) != 1:
        return None

    return metricflow_agg, columns[0].name


def _dimension_block(dim: DimensionProposal) -> dict[str, Any]:
    block: dict[str, Any] = {"name": dim.name, "type": "time" if dim.is_time else "categorical"}
    if dim.is_time:
        block["type_params"] = {"time_granularity": "day"}
    block["expr"] = dim.column
    block["description"] = dim.description
    return block


def _evidence_summary_text(metric: MetricProposal) -> str:
    if not metric.evidence:
        return "no evidence recorded"
    return ", ".join(
        f"{e.source} ({e.execution_count} executions)" if e.execution_count else e.source
        for e in metric.evidence
    )


def emit_metricflow(
    proposal: SemanticModelProposal,
    auto_merge_threshold: float = 0.85,
    dbt_manifest_available: bool = False,
    generated_at: str | None = None,
) -> str:
    """Emit a SemanticModelProposal as dbt MetricFlow YAML.

    Sorts metrics by trust_score descending, includes a canoniq evidence
    card (trust score + evidence summary + synonyms) on every metric, and
    marks metrics below `auto_merge_threshold` with a `# REVIEW REQUIRED`
    comment.
    """
    if not dbt_manifest_available:
        logger.warning(
            "No dbt manifest provided; emitted `model: ref('%s')` assumes a dbt "
            "model with that name exists.",
            proposal.source_table,
        )

    generated_at = generated_at or datetime.now(UTC).isoformat()

    entities = [
        {"name": e.name, "type": e.entity_type, "expr": e.column} for e in proposal.entities
    ]
    dimensions = [_dimension_block(d) for d in proposal.dimensions]

    measures: dict[str, dict[str, Any]] = {}
    metrics_sorted = sorted(proposal.metrics, key=lambda m: m.trust_score, reverse=True)
    metric_blocks: list[dict[str, Any]] = []

    for metric in metrics_sorted:
        block: dict[str, Any] = {"name": metric.name, "description": metric.description}
        parsed = _parse_single_agg(metric.expression)

        if parsed is not None:
            agg, column = parsed
            measure_name = f"{column}_{agg}" if column != "*" else f"row_{agg}"
            if measure_name not in measures:
                measures[measure_name] = {
                    "name": measure_name,
                    "agg": agg,
                    "expr": "1" if column == "*" else column,
                    "description": metric.description,
                    "create_metric": False,
                }
            block["type"] = "simple"
            block["type_params"] = {"measure": measure_name}
        else:
            # No structured numerator/denominator or referenced-metrics data
            # is available on MetricProposal for ratio/derived metrics, so
            # we fall back to a best-effort `expr`-based derived metric.
            # `mf validate` (Step 11) is the real backstop for these.
            block["type"] = "derived"
            block["type_params"] = {"expr": metric.expression}

        block["meta"] = {
            "canoniq_trust_score": round(metric.trust_score, 4),
            "canoniq_evidence": _evidence_summary_text(metric),
            "canoniq_synonyms": metric.synonyms,
        }
        metric_blocks.append(block)

    semantic_model: dict[str, Any] = {
        "name": proposal.dataset_name,
        "description": proposal.grain_description,
        "model": f"ref('{proposal.source_table}')",
    }
    if entities:
        semantic_model["entities"] = entities
    if dimensions:
        semantic_model["dimensions"] = dimensions
    if measures:
        semantic_model["measures"] = list(measures.values())

    header = (
        "# Auto-generated by canoniq — review before committing\n"
        f"# Trust score: {proposal.overall_trust_score:.2f} | Generated: {generated_at}\n\n"
    )

    output = header + _dump_yaml({"semantic_models": [semantic_model]})

    if metric_blocks:
        output += "\nmetrics:\n"
        for metric, block in zip(metrics_sorted, metric_blocks, strict=True):
            block_yaml = _dump_yaml([block]).rstrip("\n")
            indented = "\n".join("  " + line if line else line for line in block_yaml.split("\n"))
            if metric.trust_score < auto_merge_threshold:
                output += "  # REVIEW REQUIRED\n"
            output += indented + "\n"

    return output
