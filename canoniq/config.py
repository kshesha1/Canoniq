"""Config dataclass and canoniq.yaml loader."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

WAREHOUSE_TYPES = ("duckdb", "snowflake", "bigquery", "trino")


class ConfigError(ValueError):
    """Raised when canoniq.yaml is missing, malformed, or fails validation."""


@dataclass
class OntoRankWeights:
    source_authority: float = 0.30
    usage_frequency: float = 0.25
    cross_source_agreement: float = 0.20
    recency: float = 0.15
    certification_status: float = 0.10


@dataclass
class OntoRankThresholds:
    auto_merge: float = 0.85
    review: float = 0.50
    drop: float = 0.50


@dataclass
class Config:
    project_name: str
    warehouse_type: Literal["duckdb", "snowflake", "bigquery", "trino"]
    output_formats: list[str]
    output_dir: str
    ontorank_weights: OntoRankWeights = field(default_factory=OntoRankWeights)
    ontorank_thresholds: OntoRankThresholds = field(default_factory=OntoRankThresholds)
    llm_model: str = "claude-sonnet-4-6"
    llm_max_retries: int = 3
    continuous_enabled: bool = False
    poll_interval_seconds: int = 300
    warehouse_path: str = ""           # required at runtime for warehouse_type=duckdb
    query_log_path: str = ""           # required at runtime for query_log.type=file
    dbt_manifest_path: str | None = None

    # Layer 1 — new input sources
    ddl_files: list[str] = field(default_factory=list)
    # Paths to .sql DDL files. If provided, DDLExtractor runs even without
    # a live warehouse connection.

    document_files: list[str] = field(default_factory=list)
    # Paths to BRD/PDF/Word/text documents. DocumentExtractor will process each.

    excel_files: list[str] = field(default_factory=list)
    # Paths to Excel reports. ExcelExtractor will process each.

    require_query_log: bool = False
    # If False (default), pipeline runs without query logs.
    # DDL + documents are sufficient inputs.


def _get(d: dict[str, Any], path: str) -> Any:
    """Look up a dotted path in a nested dict, returning None if missing."""
    node: Any = d
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _require(d: dict[str, Any], path: str) -> Any:
    value = _get(d, path)
    if value is None:
        raise ConfigError(f"canoniq.yaml is missing required field: {path}")
    return value


def load_config(path: str = "canoniq.yaml") -> Config:
    """Load and validate canoniq.yaml into a Config instance."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse {path} as YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    project_name = _require(raw, "project_name")

    warehouse_type = _require(raw, "warehouse.type")
    if warehouse_type not in WAREHOUSE_TYPES:
        raise ConfigError(
            f"warehouse.type must be one of {WAREHOUSE_TYPES}, got: {warehouse_type!r}"
        )

    output_formats = _require(raw, "output.formats")
    if not isinstance(output_formats, list) or not output_formats:
        raise ConfigError("output.formats must be a non-empty list")

    output_dir = _require(raw, "output.dir")

    weights_raw = _get(raw, "ontorank.weights") or {}
    ontorank_weights = OntoRankWeights(
        **{k: v for k, v in weights_raw.items() if k in OntoRankWeights.__dataclass_fields__}
    )
    weight_sum = (
        ontorank_weights.source_authority
        + ontorank_weights.usage_frequency
        + ontorank_weights.cross_source_agreement
        + ontorank_weights.recency
        + ontorank_weights.certification_status
    )
    if abs(weight_sum - 1.0) > 1e-6:
        raise ConfigError(f"ontorank.weights must sum to 1.0, got: {weight_sum}")

    thresholds_raw = _get(raw, "ontorank.thresholds") or {}
    ontorank_thresholds = OntoRankThresholds(
        **{
            k: v
            for k, v in thresholds_raw.items()
            if k in OntoRankThresholds.__dataclass_fields__
        }
    )
    if not (
        0.0
        <= ontorank_thresholds.drop
        <= ontorank_thresholds.review
        <= ontorank_thresholds.auto_merge
        <= 1.0
    ):
        raise ConfigError(
            "ontorank.thresholds must satisfy 0 <= drop <= review <= auto_merge <= 1"
        )

    llm_model = _get(raw, "llm.model") or "claude-sonnet-4-6"
    llm_max_retries = _get(raw, "llm.max_retries")
    if llm_max_retries is None:
        llm_max_retries = 3

    continuous_enabled = _get(raw, "continuous.enabled")
    if continuous_enabled is None:
        continuous_enabled = False

    poll_interval_seconds = _get(raw, "continuous.poll_interval_seconds")
    if poll_interval_seconds is None:
        poll_interval_seconds = 300

    warehouse_path = _get(raw, "warehouse.path") or ""
    query_log_path = _get(raw, "query_log.path") or ""
    dbt_manifest_path = _get(raw, "sources.dbt_manifest")

    ddl_files = _get(raw, "ddl_files") or []
    document_files = _get(raw, "document_files") or []
    excel_files = _get(raw, "excel_files") or []
    require_query_log = _get(raw, "require_query_log")
    if require_query_log is None:
        require_query_log = False

    return Config(
        project_name=project_name,
        warehouse_type=warehouse_type,
        output_formats=output_formats,
        output_dir=output_dir,
        ontorank_weights=ontorank_weights,
        ontorank_thresholds=ontorank_thresholds,
        llm_model=llm_model,
        llm_max_retries=llm_max_retries,
        continuous_enabled=continuous_enabled,
        poll_interval_seconds=poll_interval_seconds,
        warehouse_path=warehouse_path,
        query_log_path=query_log_path,
        dbt_manifest_path=dbt_manifest_path,
        ddl_files=ddl_files,
        document_files=document_files,
        excel_files=excel_files,
        require_query_log=require_query_log,
    )
