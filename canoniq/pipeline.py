"""Report-first bootstrap pipeline (Module H).

LangGraph orchestration, consistent with the existing validation-loop
style:

    ingest artifacts (reports, tableau, docs)
      -> fingerprint tiers 1->2->3 + constraint solver
      -> drift diff (if >= 2 report editions)
      -> emit MetricFlow YAML + OSI YAML + OpenMetadata JSON
      -> validation loop (existing mf/dbt parse self-correction)
      -> conflict report
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from canoniq.config import Config
from canoniq.drift.report_diff import diff_reports
from canoniq.emit.openmetadata import emit_openmetadata, term_name
from canoniq.emitters.osi import emit_osi
from canoniq.extract.prose import mine_column_glossary, mine_document_date
from canoniq.extract.report import ReportExtraction, extract_report
from canoniq.extract.tableau import extract_from_twb
from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter, open_catalog
from canoniq.fingerprint.executor import SnapshotExecutor
from canoniq.fingerprint.solver import FingerprintContext, resolve_all
from canoniq.ingest.document_extractor import has_approval_signal
from canoniq.models import (
    ConfidenceBand,
    DriftFinding,
    DriftKind,
    ResolvedMapping,
    TableauEvidence,
)
from canoniq.proposer.models import (
    DimensionProposal,
    EntityProposal,
    EvidenceItem,
    MetricProposal,
    SemanticModelProposal,
)
from canoniq.report.conflict import (
    Contradiction,
    detect_contradictions,
    statements_from_document,
    statements_from_tableau,
    write_conflict_report,
)
from canoniq.validation.loop import build_validation_graph

logger = logging.getLogger(__name__)

_BAND_TRUST = {
    ConfidenceBand.CONFIRMED: 0.97,
    ConfidenceBand.PROBABLE: 0.85,
    ConfidenceBand.WEAK: 0.60,
}


@dataclass
class BootstrapResult:
    extractions: dict[str, ReportExtraction] = field(default_factory=dict)
    mappings_by_report: dict[str, dict[str, ResolvedMapping]] = field(default_factory=dict)
    drift_findings: list[DriftFinding] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    tableau_evidence: list[TableauEvidence] = field(default_factory=list)
    emitted: dict[str, str] = field(default_factory=dict)
    validation: dict[str, bool] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def all_mappings(self) -> list[ResolvedMapping]:
        return [m for by_name in self.mappings_by_report.values() for m in by_name.values()]


class BootstrapState(TypedDict, total=False):
    catalog_dir: str
    reports_dir: str
    tableau_dir: str
    docs_dir: str
    out_dir: str
    fp_config: FingerprintConfig
    adapter: Any
    executor: Any
    documents: dict[str, str]
    glossary: dict
    statements: list
    result: BootstrapResult


def _read_documents(docs_dir: str | None) -> dict[str, str]:
    """Read policy/BRD documents. Markdown is read directly; PDFs via the
    existing document reader."""
    if not docs_dir or not Path(docs_dir).is_dir():
        return {}
    from canoniq.ingest.document_extractor import read_document

    documents = {}
    for path in sorted(Path(docs_dir).iterdir()):
        if path.suffix.lower() in (".md", ".txt"):
            documents[path.name] = path.read_text()
        elif path.suffix.lower() == ".pdf":
            # skip the PDF twin when a same-stem markdown source exists
            if not (path.with_suffix(".md")).exists():
                try:
                    documents[path.name] = read_document(str(path))
                except Exception as exc:
                    logger.warning("skipping unreadable document %s: %s", path, exc)
    return documents


def ingest_node(state: BootstrapState) -> dict:
    result = state["result"]
    adapter = IcebergCatalogAdapter(open_catalog(state["catalog_dir"]))
    executor = SnapshotExecutor(adapter, state["fp_config"])

    reports_dir = Path(state["reports_dir"])
    for pdf in sorted(reports_dir.glob("*.pdf")):
        extraction = extract_report(str(pdf), report_id=pdf.stem)
        result.extractions[extraction.report_id] = extraction
        logger.info(
            "report %s: %d instances, %d formula hypotheses",
            extraction.report_id, len(extraction.instances),
            len(extraction.formula_hypotheses),
        )

    tableau_dir = state.get("tableau_dir")
    if tableau_dir and Path(tableau_dir).is_dir():
        for twb in sorted(Path(tableau_dir).glob("*.twb")):
            result.tableau_evidence.extend(extract_from_twb(str(twb)))

    documents = _read_documents(state.get("docs_dir"))
    glossary: dict[tuple[str, str], str] = {}
    statements = list(statements_from_tableau(result.tableau_evidence))
    for name, text in documents.items():
        glossary.update(mine_column_glossary(text))
        source_type = "brd_approved" if has_approval_signal(text) else "brd_draft"
        statements.extend(
            statements_from_document(name, text, source_type, mine_document_date(text))
        )

    return {
        "adapter": adapter,
        "executor": executor,
        "documents": documents,
        "glossary": glossary,
        "statements": statements,
        "result": result,
    }


def fingerprint_node(state: BootstrapState) -> dict:
    result = state["result"]
    for report_id, extraction in sorted(result.extractions.items()):
        ctx = FingerprintContext(
            adapter=state["adapter"],
            executor=state["executor"],
            config=state["fp_config"],
            hypotheses=extraction.formula_hypotheses,
            tableau_evidence=result.tableau_evidence,
            glossary=state["glossary"],
        )
        result.mappings_by_report[report_id] = {
            m.metric_name: m for m in resolve_all(extraction.instances, ctx)
        }
    return {"result": result}


def drift_node(state: BootstrapState) -> dict:
    result = state["result"]
    if len(result.extractions) >= 2:
        ordered = sorted(
            result.extractions.values(), key=lambda e: e.as_of_date
        )
        old, new = ordered[-2], ordered[-1]
        result.drift_findings = diff_reports(
            old.instances,
            new.instances,
            result.mappings_by_report[old.report_id],
            result.mappings_by_report[new.report_id],
            executor=state["executor"],
            documents=state["documents"],
        )
    return {"result": result}


def _proposals_from_mappings(
    result: BootstrapResult, adapter: IcebergCatalogAdapter
) -> dict[str, SemanticModelProposal]:
    """Convert accepted mappings into per-table SemanticModelProposals for
    the existing MetricFlow/OSI emitters and validation loop."""
    synonyms_by_new = {
        f.new_name: [f.old_name]
        for f in result.drift_findings
        if f.kind == DriftKind.RENAMED and f.old_name and f.new_name
    }

    # latest edition wins per metric name
    accepted: dict[str, ResolvedMapping] = {}
    for report_id in sorted(result.mappings_by_report):
        for name, mapping in result.mappings_by_report[report_id].items():
            if mapping.band in _BAND_TRUST:
                accepted[name] = mapping

    by_table: dict[str, list[ResolvedMapping]] = {}
    for mapping in accepted.values():
        by_table.setdefault(mapping.best.expr.lhs.table, []).append(mapping)

    proposals = {}
    for table, table_mappings in sorted(by_table.items()):
        metrics, dimensions, entities, joins = [], {}, {}, {}
        for mapping in table_mappings:
            expr = mapping.best.expr
            trust = _BAND_TRUST[mapping.band]
            if expr.op == "/":
                metric_type = "ratio"
            elif expr.op or expr.predicate:
                metric_type = "derived"
            else:
                metric_type = {"SUM": "sum", "COUNT": "count", "AVG": "average"}[expr.lhs.agg]
            metrics.append(
                MetricProposal(
                    name=term_name(mapping.metric_name),
                    description=mapping.prose_formula_hint
                    or f"{mapping.metric_name} as published in {mapping.report_id}.",
                    expression=expr.display_unqualified(),
                    metric_type=metric_type,
                    synonyms=synonyms_by_new.get(mapping.metric_name, [])
                    or [mapping.metric_name],
                    evidence=[
                        EvidenceItem(
                            source="numeric_fingerprint",
                            description=(
                                f"{mapping.best.satisfied}/{mapping.best.total} "
                                f"published figures reproduced ({mapping.band.value})"
                            ),
                            execution_count=0,
                            trust_contribution=trust,
                        )
                    ],
                    trust_score=trust,
                )
            )
            for binding in mapping.best.dimension_bindings:
                if binding.group_table == table:
                    dimensions[binding.dimension_key] = DimensionProposal(
                        name=binding.dimension_key,
                        column=binding.group_column,
                        table=table,
                        is_time=False,
                        description=f"Report breakdown dimension ({binding.dimension_key}).",
                        synonyms=[],
                    )
                elif binding.join_from:
                    local_table, local_col = binding.join_from.split(".")
                    ref_table, ref_col = binding.join_to.split(".")
                    entities[local_col] = EntityProposal(
                        name=local_col.lower(),
                        column=local_col,
                        table=local_table,
                        entity_type="foreign",
                        description=f"Join key to {binding.join_to}.",
                    )
                    joins[(local_table, local_col, ref_table, ref_col)] = {
                        "from_table": local_table,
                        "from_column": local_col,
                        "to_table": ref_table,
                        "to_column": ref_col,
                        "join_type": "INNER",
                    }

        trust_scores = [m.trust_score for m in metrics]
        proposals[table] = SemanticModelProposal(
            dataset_name=table.lower(),
            source_table=table,
            grain_description=(
                f"Rows of {table}, bootstrapped from published report figures."
            ),
            primary_key=[],
            entities=list(entities.values()),
            dimensions=list(dimensions.values()),
            metrics=metrics,
            joins=list(joins.values()),
            overall_trust_score=max(trust_scores),
            review_required=any(t < 0.85 for t in trust_scores),
        )
    return proposals


def emit_node(state: BootstrapState) -> dict:
    result = state["result"]
    out_dir = Path(state["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    adapter = state["adapter"]

    proposals = _proposals_from_mappings(result, adapter)
    validation_config = Config(
        project_name="canoniq-bootstrap",
        warehouse_type="duckdb",
        output_formats=["metricflow", "osi"],
        output_dir=str(out_dir),
    )
    graph = build_validation_graph(validation_config)

    for table, proposal in proposals.items():
        osi_path = out_dir / f"{proposal.dataset_name}_osi.yml"
        osi_path.write_text(emit_osi(proposal))
        result.emitted[f"osi:{table}"] = str(osi_path)

        # existing mf/dbt-parse self-correction loop — unchanged
        try:
            outcome = graph.invoke(
                {
                    "proposal": proposal,
                    "yaml_output": "",
                    "validation_errors": [],
                    "attempt": 0,
                    "passed": False,
                }
            )
            result.validation[table] = outcome["passed"]
        except Exception as exc:
            logger.warning("validation loop failed for %s: %s", table, exc)
            result.validation[table] = False
        result.emitted[f"metricflow:{table}"] = str(
            out_dir / f"{proposal.dataset_name}_metricflow.yml"
        )

    locators = {
        i.instance_id: f"{ex.report_id}: {i.source_locator}"
        for ex in result.extractions.values()
        for i in ex.instances
    }
    tables = {t: adapter.columns(t) for t in adapter.table_names()}
    om_paths = emit_openmetadata(
        out_dir, result.all_mappings(), tables, result.drift_findings, locators
    )
    result.emitted.update({f"openmetadata:{k}": v for k, v in om_paths.items()})
    return {"result": result}


def report_node(state: BootstrapState) -> dict:
    result = state["result"]
    latest = max(result.mappings_by_report) if result.mappings_by_report else None
    result.contradictions = detect_contradictions(
        state["statements"],
        result.mappings_by_report.get(latest, {}) if latest else {},
    )
    md_path, json_path = write_conflict_report(
        state["out_dir"],
        result.all_mappings(),
        result.contradictions,
        result.drift_findings,
    )
    result.emitted["conflict_report:md"] = md_path
    result.emitted["conflict_report:json"] = json_path
    return {"result": result}


def build_bootstrap_graph() -> CompiledStateGraph:
    graph: StateGraph[BootstrapState] = StateGraph(BootstrapState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("fingerprint", fingerprint_node)
    graph.add_node("drift", drift_node)
    graph.add_node("emit", emit_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "fingerprint")
    graph.add_edge("fingerprint", "drift")
    graph.add_edge("drift", "emit")
    graph.add_edge("emit", "report")
    graph.add_edge("report", END)
    return graph.compile()


def run_bootstrap(
    catalog_dir: str,
    reports_dir: str,
    out_dir: str,
    tableau_dir: str | None = None,
    docs_dir: str | None = None,
    fp_config: FingerprintConfig | None = None,
) -> BootstrapResult:
    started = time.monotonic()
    graph = build_bootstrap_graph()
    state = graph.invoke(
        {
            "catalog_dir": catalog_dir,
            "reports_dir": reports_dir,
            "tableau_dir": tableau_dir or "",
            "docs_dir": docs_dir or "",
            "out_dir": out_dir,
            "fp_config": fp_config or FingerprintConfig(),
            "result": BootstrapResult(),
        }
    )
    result: BootstrapResult = state["result"]
    result.elapsed_seconds = time.monotonic() - started
    return result
