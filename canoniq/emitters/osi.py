"""OSI v1.0 emitter — converts a SemanticModelProposal into OSI YAML.

Adapted to take a `SemanticModelProposal` as input (canoniq's own grounded,
LLM-proposed model) rather than rows from a spreadsheet.
"""

import logging
from typing import Any

import yaml

from canoniq.emitters.metricflow import CleanDumper
from canoniq.proposer.models import SemanticModelProposal

logger = logging.getLogger(__name__)

DEFAULT_OSI_VERSION = "0.1.1"
DEFAULT_DIALECT = "ANSI_SQL"


def _dump_yaml(data: Any) -> str:
    return yaml.dump(
        data, Dumper=CleanDumper, sort_keys=False, default_flow_style=False, width=100
    )


def _entity_field(entity: Any) -> dict[str, Any]:
    return {
        "name": entity.name,
        "expression": {"dialects": [{"dialect": DEFAULT_DIALECT, "expression": entity.column}]},
        "description": entity.description,
    }


def _dimension_field(dimension: Any) -> dict[str, Any]:
    return {
        "name": dimension.name,
        "expression": {
            "dialects": [{"dialect": DEFAULT_DIALECT, "expression": dimension.column}]
        },
        "dimension": {"is_time": dimension.is_time},
        "description": dimension.description,
    }


def _join_to_relationship(join: dict[str, Any]) -> dict[str, Any] | None:
    required_keys = {"from_table", "from_column", "to_table", "to_column"}
    if not required_keys.issubset(join):
        logger.warning("Skipping join with missing keys for OSI relationship: %s", join)
        return None
    return {
        "from": f"{join['from_table']}.{join['from_column']}",
        "to": f"{join['to_table']}.{join['to_column']}",
        "type": join.get("join_type", "INNER"),
    }


def _metric_block(metric: Any) -> dict[str, Any]:
    return {
        "name": metric.name,
        "expression": [{"dialect": DEFAULT_DIALECT, "expression": metric.expression}],
        "description": metric.description,
        "ai_context": {"synonyms": metric.synonyms},
    }


def emit_osi(
    proposal: SemanticModelProposal,
    source_identifier: str | None = None,
    osi_version: str = DEFAULT_OSI_VERSION,
    schema_ref: str = "../core-spec/osi-schema.json",
) -> str:
    """Emit a SemanticModelProposal as OSI v1.0 YAML."""
    fields = [_entity_field(e) for e in proposal.entities] + [
        _dimension_field(d) for d in proposal.dimensions
    ]

    dataset: dict[str, Any] = {
        "name": proposal.source_table,
        "source": source_identifier or proposal.source_table,
        "description": proposal.grain_description,
        "primary_key": proposal.primary_key,
        "fields": fields,
    }

    relationships = [
        rel for join in proposal.joins if (rel := _join_to_relationship(join)) is not None
    ]

    semantic_model: dict[str, Any] = {
        "name": proposal.dataset_name,
        "description": proposal.grain_description,
        "ai_context": {
            "instructions": f"Use this model to answer questions about {proposal.source_table}."
        },
        "datasets": [dataset],
        "relationships": relationships,
        "metrics": [_metric_block(m) for m in proposal.metrics],
    }

    header = f"# yaml-language-server: $schema={schema_ref}\n"
    version_block = _dump_yaml({"version": osi_version})
    body = _dump_yaml({"semantic_model": [semantic_model]})

    return f"{header}{version_block}\n{body}"
