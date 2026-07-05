"""canoniq CLI — wires together ingest, mining, ranking, the LLM proposer,
and the emitters/validation loop into runnable commands."""

import time
from pathlib import Path

import click
import duckdb
from rich.console import Console
from rich.table import Table

from canoniq.config import Config, ConfigError, load_config
from canoniq.emitters.metricflow import emit_metricflow
from canoniq.emitters.osi import emit_osi
from canoniq.evals.harness import report_eval_results, run_eval
from canoniq.evals.tpcds_gold import GOLD_QUERIES
from canoniq.ingest.base import (
    ColumnSchema,
    Connector,
    DDLTableEvidence,
    DocumentMetricCandidate,
    TableSchema,
)
from canoniq.ingest.dbt_manifest import DbtManifestConnector
from canoniq.ingest.ddl_extractor import parse_ddl_file
from canoniq.ingest.document_extractor import extract_from_document
from canoniq.ingest.excel_extractor import extract_from_excel
from canoniq.ingest.query_log import QueryLogFileConnector
from canoniq.ingest.warehouse import DuckDBWarehouseConnector
from canoniq.ingest.watcher import SignalWatcher
from canoniq.mining.evidence_bundle import EvidenceBundle, MetricEvidence, build_evidence_bundle
from canoniq.mining.signal_classifier import SignalClass, classify_sql
from canoniq.mining.sql_extractor import (
    AggregationCandidate,
    DimensionCandidate,
    JoinCandidate,
    extract_candidates,
)
from canoniq.proposer.llm import propose as propose_semantic_model
from canoniq.proposer.models import SemanticModelProposal
from canoniq.ranking.ontorank import OntoRankScore, score
from canoniq.validation.loop import (
    _mf_available,
    _run_mf_validate,
    _structural_validate,
    build_validation_graph,
)

console = Console()


def _resolve_path(base_dir: Path, path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else base_dir / p


def _load_config_or_exit(config_path: str) -> tuple[Config, Path]:
    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    return config, Path(config_path).resolve().parent


def _build_schemas(config: Config, base_dir: Path) -> dict[str, TableSchema]:
    if config.warehouse_type != "duckdb":
        raise click.ClickException(
            f"warehouse.type={config.warehouse_type!r} is not yet supported "
            "(only duckdb is implemented)."
        )
    if not config.warehouse_path:
        raise click.ClickException("warehouse.path is required in canoniq.yaml")

    warehouse_path = _resolve_path(base_dir, config.warehouse_path)
    connector = DuckDBWarehouseConnector(str(warehouse_path))
    return {s.fully_qualified_name: s for s in connector.get_schemas()}


def _mine_candidates(
    config: Config, base_dir: Path, schemas: dict[str, TableSchema]
) -> tuple[list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]]:
    """Mine the query log, if one is configured. Query logs are now
    SUPPLEMENTARY (see config.require_query_log) -- DDL and documents alone
    are sufficient inputs, so a missing query_log.path is only an error
    when require_query_log is explicitly set."""
    if not config.query_log_path:
        if config.require_query_log:
            raise click.ClickException("query_log.path is required in canoniq.yaml")
        return [], [], []

    query_log_path = _resolve_path(base_dir, config.query_log_path)
    raw_queries = QueryLogFileConnector(str(query_log_path)).get_query_log()

    all_aggs: list[AggregationCandidate] = []
    all_dims: list[DimensionCandidate] = []
    all_joins: list[JoinCandidate] = []
    for query in raw_queries:
        if classify_sql(query.sql) != SignalClass.ANALYTICAL:
            continue
        aggs, dims, joins = extract_candidates(query, schemas)
        all_aggs.extend(aggs)
        all_dims.extend(dims)
        all_joins.extend(joins)

    return all_aggs, all_dims, all_joins


def _ddl_evidence_to_table_schema(evidence: DDLTableEvidence) -> TableSchema:
    """Synthesize a TableSchema from parsed DDL when no live warehouse
    connection is available. DDL carries no data, only structure, so
    sample values and cardinality are always unknown here."""
    return TableSchema(
        fully_qualified_name=evidence.table_name,
        columns=[
            ColumnSchema(
                name=col.name,
                data_type=col.normalized_type,
                is_nullable=col.is_nullable,
                sample_values=[],
                cardinality_approx=None,
            )
            for col in evidence.column_candidates
        ],
        primary_keys=evidence.pk_columns,
        row_count_approx=None,
    )


def _resolve_schemas(
    config: Config, base_dir: Path
) -> tuple[dict[str, TableSchema], list[DDLTableEvidence]]:
    """Resolve table schemas from a live warehouse connection and/or DDL
    files. DDL files are always parsed when configured (even alongside a
    live warehouse) since the parsed evidence also feeds per-table
    measure/dimension inference in build_evidence_bundle, not just schema
    resolution. With no warehouse configured, DDL alone is sufficient to
    run the pipeline."""
    ddl_evidence_list: list[DDLTableEvidence] = []
    for ddl_path in config.ddl_files:
        resolved_path = _resolve_path(base_dir, ddl_path)
        evidence = parse_ddl_file(str(resolved_path))
        ddl_evidence_list.extend(evidence)
        console.print(f"  DDL: {resolved_path} -> {len(evidence)} table(s)")

    if config.warehouse_path:
        schemas = _build_schemas(config, base_dir)
    elif ddl_evidence_list:
        schemas = {
            evidence.table_name: _ddl_evidence_to_table_schema(evidence)
            for evidence in ddl_evidence_list
        }
    else:
        raise click.ClickException(
            "No schema source available: set warehouse.path or ddl_files in canoniq.yaml."
        )

    return schemas, ddl_evidence_list


def _build_bundles(config: Config, base_dir: Path) -> dict[str, EvidenceBundle]:
    """Mine every configured evidence source (query log, DDL, business
    documents, Excel reports) and group evidence into a bundle per table.
    Tables with no mined metric evidence are omitted."""
    schemas, ddl_evidence_list = _resolve_schemas(config, base_dir)
    all_aggs, all_dims, all_joins = _mine_candidates(config, base_dir, schemas)

    schema_list = list(schemas.values())
    all_doc_candidates: list[DocumentMetricCandidate] = []

    for doc_path in config.document_files:
        resolved_path = _resolve_path(base_dir, doc_path)
        candidates = extract_from_document(
            str(resolved_path), schema_list, llm_model=config.llm_model
        )
        all_doc_candidates.extend(candidates)
        console.print(f"  Doc: {resolved_path} -> {len(candidates)} candidate(s)")

    for xlsx_path in config.excel_files:
        resolved_path = _resolve_path(base_dir, xlsx_path)
        candidates = extract_from_excel(str(resolved_path), schema_list)
        all_doc_candidates.extend(candidates)
        console.print(f"  Excel: {resolved_path} -> {len(candidates)} candidate(s)")

    ddl_by_table = {evidence.table_name: evidence for evidence in ddl_evidence_list}

    bundles: dict[str, EvidenceBundle] = {}
    for fully_qualified_name, table in schemas.items():
        simple_name = fully_qualified_name.split(".")[-1]
        bundle = build_evidence_bundle(
            table,
            all_aggs,
            all_dims,
            all_joins,
            ddl_evidence=ddl_by_table.get(simple_name),
            document_candidates=[
                c
                for c in all_doc_candidates
                if c.resolved_table in (fully_qualified_name, simple_name)
                or c.resolved_table is None
            ],
        )
        if bundle.metric_candidates:
            bundles[simple_name] = bundle
    return bundles


def _score_bundle(
    bundle: EvidenceBundle, config: Config
) -> list[tuple[MetricEvidence, OntoRankScore]]:
    max_exec = max((m.execution_count for m in bundle.metric_candidates), default=1)
    return [
        (metric, score(metric, config.ontorank_weights, max_exec))
        for metric in bundle.metric_candidates
    ]


def _resolve_table_from_bundles(bundles: dict[str, EvidenceBundle], table: str | None) -> str:
    if table is not None:
        if table not in bundles:
            raise click.ClickException(
                f"No mined evidence for table {table!r}. Available: {sorted(bundles)}"
            )
        return table
    if len(bundles) == 1:
        return next(iter(bundles))
    raise click.ClickException(
        f"Multiple tables have mined evidence ({sorted(bundles)}); pass --table to pick one."
    )


def _proposal_path(config: Config, base_dir: Path, table: str) -> Path:
    output_dir = _resolve_path(base_dir, config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{table}.proposal.json"


def _resolve_table_from_proposals(output_dir: Path, table: str | None) -> str:
    if table is not None:
        return table
    candidates = sorted(
        p.stem.removesuffix(".proposal") for p in output_dir.glob("*.proposal.json")
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise click.ClickException(f"No proposal files found in {output_dir}.")
    raise click.ClickException(
        f"Multiple proposals found in {output_dir} ({candidates}); pass --table to pick one."
    )


@click.group()
def main() -> None:
    """canoniq — semantic layer authoring agent."""


@main.command(name="mine")
@click.option("--config", "config_path", default="canoniq.yaml", show_default=True)
def mine_command(config_path: str) -> None:
    """Run ingest + mining + ranking only. Print evidence bundle."""
    config, base_dir = _load_config_or_exit(config_path)
    bundles = _build_bundles(config, base_dir)

    if not bundles:
        console.print("[yellow]No analytical evidence mined from the query log.[/yellow]")
        return

    for table_name, bundle in bundles.items():
        scored = _score_bundle(bundle, config)
        table_view = Table(
            title=f"{table_name} — {len(bundle.metric_candidates)} metric candidates"
        )
        table_view.add_column("Expression")
        table_view.add_column("Trust", justify="right")
        table_view.add_column("Executions", justify="right")
        table_view.add_column("Sources")
        for evidence, ontorank in sorted(scored, key=lambda s: s[1].total, reverse=True):
            table_view.add_row(
                evidence.expression,
                f"{ontorank.total:.2f}",
                str(evidence.execution_count),
                ", ".join(evidence.source_types),
            )
        console.print(table_view)
        console.print(
            f"  dimensions: {len(bundle.dimension_candidates)}, "
            f"joins: {len(bundle.join_candidates)}\n"
        )


@main.command(name="propose")
@click.option("--config", "config_path", default="canoniq.yaml", show_default=True)
@click.option("--table", "table_name", default=None, help="Table to propose a model for.")
def propose_command(config_path: str, table_name: str | None) -> None:
    """Run mining + ranking + LLM proposer for one table. Print the proposal."""
    config, base_dir = _load_config_or_exit(config_path)
    bundles = _build_bundles(config, base_dir)
    resolved_table = _resolve_table_from_bundles(bundles, table_name)

    bundle = bundles[resolved_table]
    scored = _score_bundle(bundle, config)
    proposal = propose_semantic_model(bundle, scored, config)

    proposal_path = _proposal_path(config, base_dir, resolved_table)
    proposal_path.write_text(proposal.model_dump_json(indent=2))

    console.print_json(proposal.model_dump_json())
    console.print(f"[green]Wrote proposal to {proposal_path}[/green]")


@main.command(name="emit")
@click.option("--config", "config_path", default="canoniq.yaml", show_default=True)
@click.option("--table", "table_name", default=None, help="Table whose proposal to emit.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["metricflow", "osi", "all"]),
    default="all",
    show_default=True,
)
def emit_command(config_path: str, table_name: str | None, output_format: str) -> None:
    """Take an existing proposal (from `canoniq propose`) and emit YAML."""
    config, base_dir = _load_config_or_exit(config_path)
    output_dir = _resolve_path(base_dir, config.output_dir)
    resolved_table = _resolve_table_from_proposals(output_dir, table_name)

    proposal_path = output_dir / f"{resolved_table}.proposal.json"
    if not proposal_path.exists():
        raise click.ClickException(
            f"No proposal found at {proposal_path}; "
            f"run `canoniq propose --table {resolved_table}` first."
        )
    proposal = SemanticModelProposal.model_validate_json(proposal_path.read_text())

    formats = ["metricflow", "osi"] if output_format == "all" else [output_format]
    for fmt in formats:
        if fmt == "metricflow":
            text = emit_metricflow(
                proposal, auto_merge_threshold=config.ontorank_thresholds.auto_merge
            )
            out_path = output_dir / f"{resolved_table}_metricflow.yml"
        else:
            text = emit_osi(proposal)
            out_path = output_dir / f"{resolved_table}_osi.yml"
        out_path.write_text(text)
        console.print(f"[green]Wrote {out_path}[/green]")


@main.command(name="validate")
@click.option("--yaml", "yaml_path", required=True, help="Path to a MetricFlow YAML file.")
def validate_command(yaml_path: str) -> None:
    """Run MetricFlow validation on an existing YAML file."""
    text = Path(yaml_path).read_text()

    if _mf_available():
        try:
            errors = _run_mf_validate(text, Path(yaml_path).stem)
        except Exception:
            errors = _structural_validate(text)
    else:
        errors = _structural_validate(text)

    if errors:
        console.print(f"[red]FAILED[/red] — {len(errors)} error(s):")
        for error in errors:
            console.print(f"  - {error}")
        raise SystemExit(1)

    console.print("[green]PASSED[/green]")


def _run_pipeline_once(config: Config, base_dir: Path) -> bool:
    """Mine -> rank -> propose -> validate -> emit for every table with
    mined evidence. Returns True if at least one table was processed."""
    bundles = _build_bundles(config, base_dir)

    if not bundles:
        console.print("[yellow]No analytical evidence mined from the query log.[/yellow]")
        return False

    output_dir = _resolve_path(base_dir, config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for table_name, bundle in bundles.items():
        console.print(f"[bold]Proposing semantic model for {table_name}...[/bold]")
        scored = _score_bundle(bundle, config)
        proposal = propose_semantic_model(bundle, scored, config)

        if "metricflow" in config.output_formats:
            graph = build_validation_graph(config)
            result = graph.invoke(
                {
                    "proposal": proposal,
                    "yaml_output": "",
                    "validation_errors": [],
                    "attempt": 0,
                    "passed": False,
                }
            )
            status = "[green]PASSED[/green]" if result["passed"] else "[red]NEEDS REVIEW[/red]"
            console.print(f"  MetricFlow validation: {status} (attempt {result['attempt']})")

        if "osi" in config.output_formats:
            osi_path = output_dir / f"{table_name}_osi.yml"
            osi_path.write_text(emit_osi(proposal))
            console.print(f"  Wrote {osi_path}")

    return True


def _run_watch_loop(
    config: Config, base_dir: Path, max_iterations: int | None = None
) -> None:
    """Poll the query log (and dbt manifest, if configured) for new signals
    every `poll_interval_seconds`, re-running the full pipeline whenever new
    signals appear. `max_iterations` is test-only; production callers leave
    it unset and rely on Ctrl-C."""
    if not config.query_log_path:
        raise click.ClickException("query_log.path is required in canoniq.yaml for --watch")

    connectors: list[Connector] = [
        QueryLogFileConnector(str(_resolve_path(base_dir, config.query_log_path)))
    ]
    if config.dbt_manifest_path:
        connectors.append(
            DbtManifestConnector(str(_resolve_path(base_dir, config.dbt_manifest_path)))
        )

    watcher = SignalWatcher(config, connectors)
    console.print(
        f"[bold]Watching for new signals every {config.poll_interval_seconds}s "
        "(Ctrl-C to stop)...[/bold]"
    )

    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            new_signals = watcher.run_once()
            if new_signals:
                console.print(
                    f"[green]{len(new_signals)} new signal(s) detected — "
                    "re-running pipeline.[/green]"
                )
                _run_pipeline_once(config, base_dir)
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")


@main.command(name="run")
@click.option("--config", "config_path", default="canoniq.yaml", show_default=True)
@click.option("--watch", is_flag=True, default=False, help="Continuously poll for new signals.")
def run_command(config_path: str, watch: bool) -> None:
    """Run the full pipeline once (or continuously with --watch)."""
    config, base_dir = _load_config_or_exit(config_path)

    if watch:
        _run_watch_loop(config, base_dir)
        return

    _run_pipeline_once(config, base_dir)


@main.command(name="eval")
@click.option("--config", "config_path", default="canoniq.yaml", show_default=True)
@click.option("--table", "table_name", default=None, help="Table whose MetricFlow YAML to eval.")
@click.option("--output", "output_path", default="eval_results.json", show_default=True)
def eval_command(config_path: str, table_name: str | None, output_path: str) -> None:
    """Run the eval harness against generated YAML. Print accuracy report."""
    config, base_dir = _load_config_or_exit(config_path)
    output_dir = _resolve_path(base_dir, config.output_dir)
    resolved_table = _resolve_table_from_proposals(output_dir, table_name)

    yaml_path = output_dir / f"{resolved_table}_metricflow.yml"
    if not yaml_path.exists():
        raise click.ClickException(
            f"No MetricFlow YAML found at {yaml_path}; "
            f"run `canoniq emit --table {resolved_table}` first."
        )
    if not config.warehouse_path:
        raise click.ClickException("warehouse.path is required in canoniq.yaml")

    warehouse_path = _resolve_path(base_dir, config.warehouse_path)
    con = duckdb.connect(str(warehouse_path))
    try:
        results = run_eval(str(yaml_path), con, GOLD_QUERIES)
    finally:
        con.close()

    resolved_output = _resolve_path(output_dir, output_path)
    report_eval_results(results, output_path=str(resolved_output))


@main.command(name="bootstrap")
@click.option("--catalog", "catalog_dir", required=True,
              help="Iceberg warehouse directory (SQLite catalog.db + data).")
@click.option("--reports", "reports_dir", required=True,
              help="Directory of board-report PDFs (the consumption end).")
@click.option("--tableau", "tableau_dir", default=None, help="Directory of .twb workbooks.")
@click.option("--docs", "docs_dir", default=None,
              help="Directory of policy documents / BRDs (md, txt, pdf).")
@click.option("--out", "out_dir", required=True, help="Output directory.")
@click.option("--tolerance", default="0.005", show_default=True,
              help="Fingerprint tolerance: |computed-reported|/|reported|.")
def bootstrap_command(
    catalog_dir: str, reports_dir: str, tableau_dir: str | None,
    docs_dir: str | None, out_dir: str, tolerance: str,
) -> None:
    """Report-first cold-start bootstrap: read published reports, map every
    figure back to physical columns, prove mappings against Iceberg
    snapshots, and emit MetricFlow + OSI + OpenMetadata + conflict report."""
    from decimal import Decimal

    from canoniq.fingerprint import FingerprintConfig
    from canoniq.pipeline import run_bootstrap

    result = run_bootstrap(
        catalog_dir=catalog_dir,
        reports_dir=reports_dir,
        out_dir=out_dir,
        tableau_dir=tableau_dir,
        docs_dir=docs_dir,
        fp_config=FingerprintConfig(tolerance=Decimal(tolerance)),
    )

    view = Table(title="Resolved report metrics")
    view.add_column("Report")
    view.add_column("Metric")
    view.add_column("Band")
    view.add_column("Physical expression")
    view.add_column("Constraints", justify="right")
    for report_id in sorted(result.mappings_by_report):
        for mapping in result.mappings_by_report[report_id].values():
            view.add_row(
                report_id,
                mapping.metric_name,
                mapping.band.value,
                mapping.best.expr.canonical_key() if mapping.best else "—",
                f"{mapping.best.satisfied}/{mapping.best.total}" if mapping.best else "—",
            )
    console.print(view)
    if result.drift_findings:
        console.print(f"[yellow]{len(result.drift_findings)} drift finding(s) "
                      "— see the drift register in the conflict report.[/yellow]")
    console.print(f"Artifacts written under [bold]{out_dir}[/bold]:")
    for key, path in sorted(result.emitted.items()):
        console.print(f"  {key}: {path}")
    console.print(f"Completed in {result.elapsed_seconds:.1f}s")


@main.command(name="benchmark")
@click.option("--out", "out_dir", default=None,
              help="Output directory (default: benchmark/brownfield/output).")
@click.option("--regenerate/--no-regenerate", default=False, show_default=True,
              help="Force regeneration of the synthetic benchmark first.")
def benchmark_command(out_dir: str | None, regenerate: bool) -> None:
    """Run the full pipeline on benchmark/brownfield and score it against
    gold_labels.yaml. Prints the scorecard."""
    import json

    import yaml as _yaml

    try:
        from benchmark.brownfield import BENCHMARK_ROOT, GOLD_LABELS_NAME
        from benchmark.brownfield.generate import generate
    except ImportError:
        # console-script entry points don't put the cwd on sys.path
        import sys

        sys.path.insert(0, str(Path.cwd()))
        try:
            from benchmark.brownfield import BENCHMARK_ROOT, GOLD_LABELS_NAME
            from benchmark.brownfield.generate import generate
        except ImportError as e:
            raise click.ClickException(
                "the benchmark package is not importable — run from the repo root"
            ) from e

    from canoniq.evals.brownfield import score_benchmark
    from canoniq.pipeline import run_bootstrap

    gold_path = BENCHMARK_ROOT / GOLD_LABELS_NAME
    if regenerate or not gold_path.exists() or not (BENCHMARK_ROOT / "warehouse").exists():
        console.print("[bold]Generating synthetic brownfield benchmark...[/bold]")
        generate(BENCHMARK_ROOT)

    resolved_out = Path(out_dir) if out_dir else BENCHMARK_ROOT / "output"
    result = run_bootstrap(
        catalog_dir=str(BENCHMARK_ROOT / "warehouse"),
        reports_dir=str(BENCHMARK_ROOT / "reports"),
        tableau_dir=str(BENCHMARK_ROOT / "tableau"),
        docs_dir=str(BENCHMARK_ROOT / "docs"),
        out_dir=str(resolved_out),
    )
    gold = _yaml.safe_load(gold_path.read_text())
    card = score_benchmark(gold, result)

    metrics_view = Table(title="Mapping outcomes vs gold labels")
    metrics_view.add_column("Report")
    metrics_view.add_column("Metric")
    metrics_view.add_column("Band")
    metrics_view.add_column("Resolved expression")
    metrics_view.add_column("Correct")
    for outcome in card.metric_outcomes:
        metrics_view.add_row(
            outcome.report_id,
            outcome.metric,
            outcome.band,
            outcome.resolved_expression or "—",
            "[green]yes[/green]" if outcome.correct else "[red]NO[/red]",
        )
    console.print(metrics_view)

    summary = Table(title="Canoniq brownfield benchmark scorecard")
    summary.add_column("Measure")
    summary.add_column("Value", justify="right")
    for report_id, recall in card.extraction_recall.items():
        summary.add_row(f"Extraction recall ({report_id})", f"{recall:.1%}")
    summary.add_row("Mapping recall (correct expr, CONFIRMED/PROBABLE)",
                    f"{card.mapping_recall:.1%}")
    for band, precision in sorted(card.band_precision.items()):
        summary.add_row(
            f"Mapping precision @ {band} (n={card.band_counts[band]})",
            f"{precision:.1%}",
        )
    summary.add_row("Unmappable correctly escalated", f"{card.unmappable_accuracy:.1%}")
    for trap in card.traps:
        colour = "green" if trap.ok else "red"
        summary.add_row(f"Trap: {trap.name}", f"[{colour}]{trap.actual}[/{colour}]")
    summary.add_row("Drift findings", f"{card.drift_found}/{card.drift_expected}")
    summary.add_row("Wall-clock", f"{card.elapsed_seconds:.1f}s")
    summary.add_row(
        "Overall", "[green]PASSED[/green]" if card.passed else "[red]FAILED[/red]"
    )
    console.print(summary)

    scorecard_path = resolved_out / "benchmark_scorecard.json"
    scorecard_path.write_text(json.dumps(card.to_dict(), indent=2))
    console.print(f"Scorecard written to {scorecard_path}")
    if not card.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
