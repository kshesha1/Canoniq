"""dbt manifest connector — parses dbt Core's `target/manifest.json` for
model schemas and metric definitions that are already certified by
human-authored dbt YAML (the highest OntoRank source-authority signal)."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from canoniq.ingest.base import ColumnSchema, Connector, RawQuery, TableSchema

logger = logging.getLogger(__name__)


class DbtManifestConnector(Connector):
    """Reads a dbt Core `manifest.json`."""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict[str, Any]:
        return json.loads(Path(self.path).read_text())

    def get_schemas(self) -> list[TableSchema]:
        """Extract model names and column descriptions as TableSchema
        objects. dbt's manifest doesn't carry sample values or approximate
        cardinality, so those fields are left empty/unknown."""
        manifest = self._load()
        tables = []
        for node in manifest.get("nodes", {}).values():
            if node.get("resource_type") != "model":
                continue

            columns = [
                ColumnSchema(
                    name=col_name,
                    data_type=str(col.get("data_type") or "string").lower(),
                    is_nullable=True,
                    sample_values=[],
                    cardinality_approx=None,
                )
                for col_name, col in (node.get("columns") or {}).items()
            ]
            tables.append(
                TableSchema(
                    fully_qualified_name=node.get("name", node.get("unique_id", "")),
                    columns=columns,
                    primary_keys=[],
                    row_count_approx=None,
                )
            )
        return tables

    def get_dbt_metrics(self) -> list[dict[str, Any]]:
        """Extract dbt-defined metrics (MetricFlow `metrics` nodes) in the
        shape `evidence_bundle.build_evidence_bundle`'s `dbt_metrics`
        parameter expects: {"name", "expression", "table"}. A metric
        appearing here marks matching mined candidates `is_certified=True`.
        """
        manifest = self._load()
        nodes = manifest.get("nodes", {})

        metrics = []
        for metric_node in manifest.get("metrics", {}).values():
            type_params = metric_node.get("type_params") or {}
            expression = type_params.get("expr") or metric_node.get("name", "")
            metrics.append(
                {
                    "name": metric_node.get("name"),
                    "expression": expression,
                    "table": self._resolve_source_table(metric_node, nodes),
                }
            )
        return metrics

    @staticmethod
    def _resolve_source_table(metric_node: dict[str, Any], nodes: dict[str, Any]) -> str:
        depends_on = (metric_node.get("depends_on") or {}).get("nodes", [])
        for dep_id in depends_on:
            if dep_id.startswith("model."):
                model_node = nodes.get(dep_id)
                if model_node:
                    return str(model_node.get("name", ""))
        return ""

    def get_query_log(self) -> list[RawQuery]:
        raise NotImplementedError(
            "DbtManifestConnector only reads models/metrics; "
            "use canoniq.ingest.query_log for query log ingestion."
        )

    def watch(self, callback: Callable[[Any], None]) -> None:
        raise NotImplementedError(
            "DbtManifestConnector does not support watching directly; "
            "use canoniq.ingest.watcher.SignalWatcher."
        )
