from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import canoniq.cli as cli_module
from canoniq.cli import main
from canoniq.mining.evidence_bundle import EvidenceBundle
from canoniq.proposer.models import EvidenceItem, MetricProposal, SemanticModelProposal
from canoniq.ranking.ontorank import OntoRankScore

EXAMPLE_CONFIG = (
    Path(__file__).parent.parent / "examples" / "tpcds_duckdb" / "canoniq.yaml"
)
EXAMPLE_WAREHOUSE_DB = (
    Path(__file__).parent.parent / "examples" / "tpcds_duckdb" / "warehouse.db"
)
QUERIES_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_queries.sql"


def _write_config(tmp_path: Path, output_dir: Path) -> Path:
    """Build a canoniq.yaml pointing at the real example warehouse/query log
    fixtures but an isolated tmp_path output dir, so tests never write into
    the checked-in examples/ directory."""
    config = {
        "project_name": "cli_test",
        "warehouse": {"type": "duckdb", "path": str(EXAMPLE_WAREHOUSE_DB)},
        "query_log": {"type": "file", "path": str(QUERIES_FIXTURE)},
        "output": {"formats": ["metricflow", "osi"], "dir": str(output_dir)},
        "ontorank": {
            "weights": {
                "source_authority": 0.30,
                "usage_frequency": 0.25,
                "cross_source_agreement": 0.20,
                "recency": 0.15,
                "certification_status": 0.10,
            },
            "thresholds": {"auto_merge": 0.85, "review": 0.50, "drop": 0.50},
        },
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-6", "max_retries": 3},
        "continuous": {"enabled": False, "poll_interval_seconds": 300},
    }
    config_path = tmp_path / "canoniq.yaml"
    config_path.write_text(yaml.dump(config))
    return config_path


def _fake_propose(
    bundle: EvidenceBundle, scored: list[tuple[object, OntoRankScore]], config: object
) -> SemanticModelProposal:
    """Stand-in for the real LLM proposer: builds a minimal, always-valid
    proposal directly from real mined evidence, so `canoniq run`/`propose`
    can be exercised end-to-end without hitting a real API."""
    metric_evidence, ontorank = scored[0]
    table_name = bundle.table.fully_qualified_name.split(".")[-1]
    return SemanticModelProposal(
        dataset_name=table_name,
        source_table=table_name,
        grain_description="test grain",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[
            MetricProposal(
                name="test_metric",
                description="A test metric.",
                expression=metric_evidence.expression,
                metric_type="sum",
                synonyms=["alias"],
                evidence=[
                    EvidenceItem(
                        source="query_log_simple",
                        description="mined",
                        execution_count=metric_evidence.execution_count,
                        trust_contribution=0.5,
                    )
                ],
                trust_score=ontorank.total,
            )
        ],
        joins=[],
        overall_trust_score=ontorank.total,
        review_required=False,
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_mine_prints_evidence_for_multiple_tables(runner: CliRunner) -> None:
    result = runner.invoke(main, ["mine", "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0
    assert "store_sales" in result.output
    assert "metric candidates" in result.output


def test_mine_missing_config_errors(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(main, ["mine", "--config", str(tmp_path / "does_not_exist.yaml")])
    assert result.exit_code != 0


def test_propose_writes_proposal_and_prints_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    result = runner.invoke(
        main, ["propose", "--config", str(config_path), "--table", "store_sales"]
    )

    assert result.exit_code == 0, result.output
    assert "store_sales" in result.output
    proposal_file = output_dir / "store_sales.proposal.json"
    assert proposal_file.exists()
    proposal = SemanticModelProposal.model_validate_json(proposal_file.read_text())
    assert proposal.source_table == "store_sales"


def test_propose_requires_table_when_ambiguous(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    config_path = _write_config(tmp_path, tmp_path / "out")

    result = runner.invoke(main, ["propose", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Multiple tables" in str(result.output) + str(result.exception)


def test_propose_unknown_table_errors(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    config_path = _write_config(tmp_path, tmp_path / "out")

    result = runner.invoke(
        main, ["propose", "--config", str(config_path), "--table", "not_a_real_table"]
    )

    assert result.exit_code != 0


def test_emit_round_trip_after_propose(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    propose_result = runner.invoke(
        main, ["propose", "--config", str(config_path), "--table", "store_sales"]
    )
    assert propose_result.exit_code == 0, propose_result.output

    emit_result = runner.invoke(
        main,
        ["emit", "--config", str(config_path), "--table", "store_sales", "--format", "all"],
    )
    assert emit_result.exit_code == 0, emit_result.output

    mf_path = output_dir / "store_sales_metricflow.yml"
    osi_path = output_dir / "store_sales_osi.yml"
    assert mf_path.exists()
    assert osi_path.exists()
    assert "semantic_models" in yaml.safe_load(mf_path.read_text())
    assert "semantic_model" in yaml.safe_load(osi_path.read_text())


def test_emit_without_prior_proposal_errors(runner: CliRunner, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, tmp_path / "out")
    result = runner.invoke(
        main, ["emit", "--config", str(config_path), "--table", "store_sales"]
    )
    assert result.exit_code != 0


def test_validate_pass_and_fail(runner: CliRunner, tmp_path: Path) -> None:
    from canoniq.emitters.metricflow import emit_metricflow

    proposal = SemanticModelProposal(
        dataset_name="orders",
        source_table="orders",
        grain_description="g",
        primary_key=[],
        entities=[],
        dimensions=[],
        metrics=[],
        joins=[],
        overall_trust_score=0.5,
        review_required=False,
    )
    valid_path = tmp_path / "valid.yml"
    valid_path.write_text(emit_metricflow(proposal))
    pass_result = runner.invoke(main, ["validate", "--yaml", str(valid_path)])
    assert pass_result.exit_code == 0
    assert "PASSED" in pass_result.output

    invalid_path = tmp_path / "invalid.yml"
    invalid_path.write_text("semantic_models:\n  - name: x\n")
    fail_result = runner.invoke(main, ["validate", "--yaml", str(invalid_path)])
    assert fail_result.exit_code != 0
    assert "FAILED" in fail_result.output


def test_run_end_to_end_produces_output_for_every_table(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 12's end-to-end check: `canoniq run` wires ingest -> mining ->
    ranking -> proposer -> validation loop -> emitters. The LLM call is
    mocked (never call a real LLM from tests), but every other layer runs
    for real against the TPC-DS example warehouse/query log."""
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    result = runner.invoke(main, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "store_sales" in result.output
    assert "PASSED" in result.output

    osi_files = list(output_dir.glob("*_osi.yml"))
    assert len(osi_files) >= 5  # one per table with mined evidence
    for osi_file in osi_files:
        parsed = yaml.safe_load(osi_file.read_text())
        assert "semantic_model" in parsed


def test_run_watch_polls_runs_pipeline_and_stops_on_interrupt(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--watch polls for new signals, re-runs the pipeline when it finds
    some, and exits cleanly on Ctrl-C (simulated here via a monkeypatched
    time.sleep, so the test never actually blocks for poll_interval_seconds)."""
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    def fake_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.time, "sleep", fake_sleep)

    result = runner.invoke(main, ["run", "--config", str(config_path), "--watch"])

    assert result.exit_code == 0, result.output
    assert "Watching for new signals" in result.output
    assert "new signal(s) detected" in result.output
    assert "Stopped watching" in result.output
    # the pipeline actually ran once, before the simulated Ctrl-C
    assert (output_dir / "store_sales_osi.yml").exists()


def test_eval_command_writes_accuracy_report(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "propose_semantic_model", _fake_propose)
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    run_result = runner.invoke(main, ["run", "--config", str(config_path)])
    assert run_result.exit_code == 0, run_result.output
    assert (output_dir / "store_sales_metricflow.yml").exists()

    eval_result = runner.invoke(
        main,
        [
            "eval",
            "--config",
            str(config_path),
            "--table",
            "store_sales",
            "--output",
            "eval_results.json",
        ],
    )

    assert eval_result.exit_code == 0, eval_result.output
    assert "Accuracy" in eval_result.output
    report_path = output_dir / "eval_results.json"
    assert report_path.exists()

    import json

    report = json.loads(report_path.read_text())
    assert "accuracy" in report
    assert len(report["results"]) == 10


def test_eval_command_errors_without_metricflow_yaml(
    runner: CliRunner, tmp_path: Path
) -> None:
    config_path = _write_config(tmp_path, tmp_path / "out")
    result = runner.invoke(
        main, ["eval", "--config", str(config_path), "--table", "store_sales"]
    )
    assert result.exit_code != 0
