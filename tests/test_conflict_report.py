"""Module F: conflict report — four sections, markdown + JSON twin,
contradictions surfaced (never auto-resolved), unmappable as a work queue."""

import json
from pathlib import Path

import pytest

from canoniq.drift.report_diff import diff_reports
from canoniq.extract.prose import mine_column_glossary, mine_document_date
from canoniq.extract.report import extract_report
from canoniq.extract.tableau import extract_from_twb
from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter, open_catalog
from canoniq.fingerprint.executor import SnapshotExecutor
from canoniq.fingerprint.solver import FingerprintContext, resolve_all
from canoniq.report.conflict import (
    detect_contradictions,
    statements_from_document,
    statements_from_tableau,
    write_conflict_report,
)


@pytest.fixture(scope="module")
def report_artifacts(brownfield_root: Path, tmp_path_factory):
    adapter = IcebergCatalogAdapter(open_catalog(brownfield_root / "warehouse"))
    executor = SnapshotExecutor(adapter, FingerprintConfig())
    tableau = extract_from_twb(str(brownfield_root / "tableau" / "risk_dashboard.twb"))
    glossary = mine_column_glossary(
        (brownfield_root / "docs" / "brd_credit_exposure_mart.md").read_text()
    )

    editions = {}
    for report_id in ("sor_2025q4", "sor_2026q1"):
        extraction = extract_report(
            str(brownfield_root / "reports" / f"{report_id}.pdf"), report_id=report_id
        )
        ctx = FingerprintContext(
            adapter=adapter, executor=executor, config=FingerprintConfig(),
            hypotheses=extraction.formula_hypotheses,
            tableau_evidence=tableau, glossary=glossary,
        )
        mappings = {m.metric_name: m for m in resolve_all(extraction.instances, ctx)}
        editions[report_id] = (extraction, mappings)

    statements = list(statements_from_tableau(tableau))
    documents = {}
    for doc in sorted((brownfield_root / "docs").glob("*.md")):
        text = doc.read_text()
        documents[doc.name] = text
        statements += statements_from_document(
            doc.name, text, "brd_approved", mine_document_date(text)
        )

    old_ex, old_map = editions["sor_2025q4"]
    new_ex, new_map = editions["sor_2026q1"]
    contradictions = detect_contradictions(statements, new_map)
    drift = diff_reports(
        old_ex.instances, new_ex.instances, old_map, new_map,
        executor=executor, documents=documents,
    )
    mappings = list(old_map.values()) + list(new_map.values())

    out_dir = tmp_path_factory.mktemp("conflict")
    md_path, json_path = write_conflict_report(out_dir, mappings, contradictions, drift)
    return {
        "markdown": Path(md_path).read_text(),
        "json": json.loads(Path(json_path).read_text()),
        "contradictions": contradictions,
    }


def test_four_sections_present(report_artifacts):
    md = report_artifacts["markdown"]
    for header in (
        "## 1. Confirmed mappings",
        "## 2. Contradictions",
        "## 3. Unmappable — escalate to steward",
        "## 4. Drift register",
    ):
        assert header in md


def test_confirmed_section_details(report_artifacts):
    md = report_artifacts["markdown"]
    assert "SUM(RWA_CALC_FCT.RWA_AMT_V3)" in md
    assert "figures reproduced" in md
    twin = report_artifacts["json"]
    confirmed = {m["metric"] for m in twin["confirmed_mappings"]}
    assert "Total Credit RWA" in confirmed
    assert len(twin["confirmed_mappings"]) == 10  # 5 per edition


def test_policy_vs_tableau_contradiction_surfaced(report_artifacts):
    """The 2019 policy (no netting), the 2024 policy (nets collateral) and
    the Tableau field (no netting) disagree on Total Credit Risk Exposure."""
    contradictions = report_artifacts["contradictions"]
    cre = [c for c in contradictions if "Credit Risk Exposure" in c.metric_name]
    assert cre, "the planted contradiction was not detected"
    sources = {s.source for s in cre[0].sides}
    assert any("2019" in s for s in sources)
    assert any("2024" in s for s in sources)
    assert any(s.endswith(".twb") for s in sources)
    # empirical adjudication is noted, but sides are all preserved verbatim
    assert cre[0].empirical_note is not None
    assert "EXP_AMT_USD" in cre[0].empirical_note


def test_unmappable_is_a_work_queue(report_artifacts):
    md = report_artifacts["markdown"]
    assert "Adjusted Stress Capital Buffer" in md
    assert "tiers run: tier1, tier2, tier3" in md
    assert "best near-miss" in md
    assert "steward" in md
    twin = report_artifacts["json"]
    assert len(twin["unmappable"]) == 2  # one per edition


def test_drift_register_undocumented_first(report_artifacts):
    twin = report_artifacts["json"]
    register = twin["drift_register"]
    assert len(register) == 2
    assert all(f["undocumented"] for f in register)
    kinds = {f["kind"] for f in register}
    assert kinds == {"renamed", "redefined"}


def test_json_twin_mirrors_markdown(report_artifacts):
    twin = report_artifacts["json"]
    assert set(twin) >= {
        "confirmed_mappings", "contradictions", "unmappable", "drift_register",
    }
