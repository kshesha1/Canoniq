"""LangGraph validation loop: generate -> mf validate -> repair -> retry.

`mf validate-configs` (the real dbt MetricFlow CLI) is used when available;
otherwise this falls back to a structural jsonschema check of the emitted
YAML, which is what actually gets exercised in this repo's test environment
and in any environment without a full dbt project scaffolded around it.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypedDict

import jsonschema
import yaml
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from canoniq.config import Config
from canoniq.emitters.metricflow import emit_metricflow
from canoniq.proposer.llm import StructuredClient, repair_proposal
from canoniq.proposer.models import SemanticModelProposal

logger = logging.getLogger(__name__)

VALIDATION_FAILED_HEADER = "# VALIDATION FAILED — manual review required\n"

METRICFLOW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["semantic_models"],
    "properties": {
        "semantic_models": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "model"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "model": {"type": "string", "pattern": r"^ref\('.+'\)$"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "type", "expr"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "type": {"enum": ["primary", "foreign", "unique"]},
                                "expr": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "type", "expr"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "type": {"enum": ["time", "categorical"]},
                                "expr": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "measures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "agg", "expr"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "agg": {
                                    "enum": [
                                        "sum",
                                        "count",
                                        "count_distinct",
                                        "average",
                                        "min",
                                        "max",
                                    ]
                                },
                                "expr": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type", "type_params"],
                "properties": {
                    "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
                    "type": {"enum": ["simple", "ratio", "derived"]},
                    "type_params": {"type": "object"},
                },
            },
        },
    },
}


class ValidationState(TypedDict):
    proposal: SemanticModelProposal
    yaml_output: str                   # current generated YAML
    validation_errors: list[str]       # errors from last mf validate run
    attempt: int
    passed: bool


def _mf_available() -> bool:
    return shutil.which("mf") is not None


def _run_mf_validate(yaml_output: str, model_name: str) -> list[str]:
    """Best-effort real `mf validate-configs` invocation. Requires a dbt
    project to already exist around the generated YAML; if the subprocess
    itself can't run (no project scaffolding, etc.), the caller falls back
    to the structural jsonschema check instead."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yaml_path = Path(tmp_dir) / f"{model_name}.yml"
        yaml_path.write_text(yaml_output)
        try:
            result = subprocess.run(
                ["mf", "validate-configs", "--select", model_name],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("`mf validate-configs` failed to run: %s", e)
            raise

        if result.returncode == 0:
            return []
        output = (result.stdout or "") + (result.stderr or "")
        return [line.strip() for line in output.splitlines() if line.strip()]


def _structural_validate(yaml_output: str) -> list[str]:
    try:
        document = yaml.safe_load(yaml_output)
    except yaml.YAMLError as e:
        return [f"<root>: not valid YAML: {e}"]

    validator = jsonschema.Draft202012Validator(METRICFLOW_JSON_SCHEMA)
    errors = []
    for error in validator.iter_errors(document):
        path = ".".join(str(p) for p in error.path) or "<root>"
        errors.append(f"{path}: {error.message}")
    return errors


def emit_node(state: ValidationState, config: Config) -> dict[str, Any]:
    """Generate YAML from the current proposal."""
    yaml_output = emit_metricflow(
        state["proposal"],
        auto_merge_threshold=config.ontorank_thresholds.auto_merge,
    )
    return {"yaml_output": yaml_output}


def validate_node(state: ValidationState) -> dict[str, Any]:
    """Run MetricFlow validation against generated YAML.

    Preferred: `mf validate-configs --select {model}`.
    Fallback (if `mf` not installed, or it can't run): structural
    validation against MetricFlow's expected schema using jsonschema.
    """
    model_name = state["proposal"].dataset_name

    if _mf_available():
        try:
            errors = _run_mf_validate(state["yaml_output"], model_name)
        except Exception:
            errors = _structural_validate(state["yaml_output"])
    else:
        errors = _structural_validate(state["yaml_output"])

    return {
        "validation_errors": errors,
        "passed": len(errors) == 0,
        "attempt": state["attempt"] + 1,
    }


def repair_node(
    state: ValidationState, config: Config, client: StructuredClient | None
) -> dict[str, Any]:
    """Feed the failing YAML + errors back to the LLM proposer, asking it
    to fix only the failing definitions."""
    repaired = repair_proposal(
        state["proposal"],
        state["yaml_output"],
        state["validation_errors"],
        config,
        client=client,
    )
    return {"proposal": repaired}


def accept_node(state: ValidationState, config: Config) -> dict[str, Any]:
    """Finalize the YAML artifact. If retries were exhausted without a real
    pass, prepend a manual-review header rather than silently emitting
    possibly-broken YAML."""
    yaml_output = state["yaml_output"]
    if not state["passed"] and not yaml_output.startswith(VALIDATION_FAILED_HEADER):
        yaml_output = VALIDATION_FAILED_HEADER + yaml_output

    output_path = Path(config.output_dir) / f"{state['proposal'].dataset_name}_metricflow.yml"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_output)

    return {"yaml_output": yaml_output}


def build_validation_graph(
    config: Config, client: StructuredClient | None = None
) -> CompiledStateGraph:
    """
    Nodes:
      emit        -> generate YAML from proposal (MetricFlow emitter)
      validate    -> run `mf validate` or `dbt parse`, capture errors
      repair      -> if errors: feed back to LLM with error context, regenerate proposal
      accept      -> write YAML to output dir, finalize

    Edges:
      emit -> validate
      validate -> accept (if no errors or attempt >= max_retries)
      validate -> repair (if errors and attempt < max_retries)
      repair -> emit
      accept -> END
    """
    graph: StateGraph[ValidationState] = StateGraph(ValidationState)

    graph.add_node("emit", lambda s: emit_node(s, config))
    graph.add_node("validate", validate_node)
    graph.add_node("repair", lambda s: repair_node(s, config, client))
    graph.add_node("accept", lambda s: accept_node(s, config))

    graph.set_entry_point("emit")
    graph.add_edge("emit", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda s: (
            "accept" if (s["passed"] or s["attempt"] >= config.llm_max_retries) else "repair"
        ),
    )
    graph.add_edge("repair", "emit")
    graph.add_edge("accept", END)

    return graph.compile()
