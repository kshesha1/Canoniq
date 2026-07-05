"""Module H: bootstrap pipeline + CLI + benchmark scorecard."""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from canoniq.cli import main as cli_main
from canoniq.evals.brownfield import score_benchmark
from canoniq.pipeline import run_bootstrap


@pytest.fixture(scope="module")
def bootstrap_result(brownfield_root: Path, tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("bootstrap_out")
    result = run_bootstrap(
        catalog_dir=str(brownfield_root / "warehouse"),
        reports_dir=str(brownfield_root / "reports"),
        tableau_dir=str(brownfield_root / "tableau"),
        docs_dir=str(brownfield_root / "docs"),
        out_dir=str(out_dir),
    )
    return result, out_dir


def test_pipeline_emits_all_three_formats_plus_report(bootstrap_result):
    result, out_dir = bootstrap_result
    kinds = {key.split(":")[0] for key in result.emitted}
    assert kinds == {"metricflow", "osi", "openmetadata", "conflict_report"}
    for path in result.emitted.values():
        assert Path(path).exists()


def test_metricflow_output_passes_validation_loop(bootstrap_result):
    result, out_dir = bootstrap_result
    assert result.validation and all(result.validation.values())
    yml = (out_dir / "rwa_calc_fct_metricflow.yml").read_text()
    parsed_docs = yaml.safe_load(yml)
    assert parsed_docs["semantic_models"][0]["model"] == "ref('RWA_CALC_FCT')"
    assert any(m["name"] == "total_credit_rwa" for m in parsed_docs["metrics"])


def test_osi_output_contains_confirmed_metric(bootstrap_result):
    result, out_dir = bootstrap_result
    osi = yaml.safe_load((out_dir / "crd_exp_fct_osi.yml").read_text())
    model = osi["semantic_model"][0]
    metric_names = {m["name"] for m in model["metrics"]}
    assert "total_credit_risk_exposure" in metric_names
    # the dimension join discovered by the solver becomes a relationship
    assert any(
        rel["from"] == "CRD_EXP_FCT.LE_CD" and rel["to"] == "LE_REF.LE_CD"
        for rel in model["relationships"]
    )


def test_scorecard_is_fully_green(brownfield_root: Path, bootstrap_result):
    result, _ = bootstrap_result
    gold = yaml.safe_load((brownfield_root / "gold_labels.yaml").read_text())
    card = score_benchmark(gold, result)
    assert card.extraction_recall == {"sor_2025q4": 1.0, "sor_2026q1": 1.0}
    assert card.mapping_recall == 1.0
    assert card.band_precision.get("CONFIRMED") == 1.0
    assert card.unmappable_accuracy == 1.0
    assert all(t.ok for t in card.traps)
    assert card.drift_found == card.drift_expected == 2
    assert card.passed
    assert card.elapsed_seconds < 300  # spec: < 5 minutes end to end
    payload = card.to_dict()
    assert payload["passed"] is True


def test_cli_bootstrap_command(brownfield_root: Path, tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "bootstrap",
            "--catalog", str(brownfield_root / "warehouse"),
            "--reports", str(brownfield_root / "reports"),
            "--tableau", str(brownfield_root / "tableau"),
            "--docs", str(brownfield_root / "docs"),
            "--out", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "CONFIRMED" in result.output
    assert "UNMAPPABLE" in result.output
    assert "drift finding" in result.output
    assert (tmp_path / "out" / "conflict_report.md").exists()


def test_prose_hint_has_no_duplicated_heading(bootstrap_result):
    result, _ = bootstrap_result
    mapping = result.mappings_by_report["sor_2026q1"]["Total Credit RWA"]
    hint = mapping.prose_formula_hint
    assert hint.startswith("Total Credit RWA is the sum")
