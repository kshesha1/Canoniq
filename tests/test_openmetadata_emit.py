"""Module G: OpenMetadata emitter — schema-valid JSON files with full
canoniq_provenance, glossary synonyms from drift renames, column tags and
lineage edges."""

import json
from pathlib import Path

import pytest

from canoniq.drift.report_diff import diff_reports
from canoniq.emit.openmetadata import (
    OM_SCHEMA_VERSION,
    build_payloads,
    emit_openmetadata,
    term_name,
    validate_payloads,
)
from canoniq.extract.prose import mine_column_glossary
from canoniq.extract.report import extract_report
from canoniq.extract.tableau import extract_from_twb
from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter, open_catalog
from canoniq.fingerprint.executor import SnapshotExecutor
from canoniq.fingerprint.solver import FingerprintContext, resolve_all


@pytest.fixture(scope="module")
def om_setup(brownfield_root: Path):
    adapter = IcebergCatalogAdapter(open_catalog(brownfield_root / "warehouse"))
    executor = SnapshotExecutor(adapter, FingerprintConfig())
    tableau = extract_from_twb(str(brownfield_root / "tableau" / "risk_dashboard.twb"))
    glossary = mine_column_glossary(
        (brownfield_root / "docs" / "brd_credit_exposure_mart.md").read_text()
    )
    editions, locators = {}, {}
    for report_id in ("sor_2025q4", "sor_2026q1"):
        extraction = extract_report(
            str(brownfield_root / "reports" / f"{report_id}.pdf"), report_id=report_id
        )
        locators.update({i.instance_id: i.source_locator for i in extraction.instances})
        ctx = FingerprintContext(
            adapter=adapter, executor=executor, config=FingerprintConfig(),
            hypotheses=extraction.formula_hypotheses,
            tableau_evidence=tableau, glossary=glossary,
        )
        editions[report_id] = (
            extraction, {m.metric_name: m for m in resolve_all(extraction.instances, ctx)}
        )
    old_ex, old_map = editions["sor_2025q4"]
    new_ex, new_map = editions["sor_2026q1"]
    drift = diff_reports(
        old_ex.instances, new_ex.instances, old_map, new_map, executor=executor
    )
    mappings = list(old_map.values()) + list(new_map.values())
    tables = {t: adapter.columns(t) for t in adapter.table_names()}
    payloads = build_payloads(
        mappings, tables, drift, instance_locators=locators
    )
    return payloads, mappings, tables, drift, locators


def test_payloads_validate_against_pinned_schemas(om_setup):
    payloads, *_ = om_setup
    assert OM_SCHEMA_VERSION == "1.5.0"
    validate_payloads(payloads)  # raises on violation


def test_one_term_per_metric_with_provenance(om_setup):
    payloads, *_ = om_setup
    terms = {t["displayName"]: t for t in payloads["glossary_terms"]}
    # 6 distinct confirmed metric names across both editions (the rename
    # keeps both names since each edition's mapping is confirmed)
    assert "Total Credit RWA" in terms
    prov = terms["Total Credit RWA"]["extension"]["canoniq_provenance"]
    assert prov["confidence_band"] == "CONFIRMED"
    assert prov["physical_expression"] == "SUM(RWA_CALC_FCT.RWA_AMT_V3)"
    assert prov["constraints_satisfied"] == prov["constraints_total"] == 9
    assert prov["source_locators"], "provenance must carry report locators"
    # description prefers the prose hint from report commentary
    assert "sum of risk-weighted assets" in terms["Total Credit RWA"]["description"]


def test_unmappable_metric_gets_no_term(om_setup):
    payloads, *_ = om_setup
    names = {t["displayName"] for t in payloads["glossary_terms"]}
    assert "Adjusted Stress Capital Buffer" not in names


def test_drift_rename_becomes_synonym(om_setup):
    payloads, *_ = om_setup
    terms = {t["displayName"]: t for t in payloads["glossary_terms"]}
    ace = terms["Adjusted Counterparty Exposure"]
    assert ace["synonyms"] == ["Counterparty Credit Exposure"]


def test_column_tags_link_to_terms(om_setup):
    payloads, *_ = om_setup
    tables = {t["name"]: t for t in payloads["tables"]}
    rwa_cols = {c["name"]: c for c in tables["RWA_CALC_FCT"]["columns"]}
    tag_fqns = [t["tagFQN"] for t in rwa_cols["RWA_AMT_V3"]["tags"]]
    assert any(term_name("Total Credit RWA") in fqn for fqn in tag_fqns)
    # the rejected deprecated twin is never tagged
    assert rwa_cols["RWA_AMT_V2_DEPR"]["tags"] == []
    # dimension binding columns are tagged too
    le_cols = {c["name"]: c for c in tables["LE_REF"]["columns"]}
    assert le_cols["LE_NM"]["tags"]


def test_lineage_edges_report_to_columns(om_setup):
    payloads, *_ = om_setup
    edges = payloads["lineage"]["edges"]
    assert edges
    cre_edges = [
        e for e in edges
        if "Total Credit Risk Exposure" in e["lineageDetails"]["description"]
    ]
    froms = {e["fromEntity"]["fullyQualifiedName"] for e in cre_edges}
    assert "canoniq.CRD_EXP_FCT.EXP_AMT_USD" in froms
    assert "canoniq.CRD_EXP_FCT.COLL_HELD_AMT" in froms
    assert all(
        e["toEntity"]["fullyQualifiedName"].startswith("canoniq.reports.")
        for e in cre_edges
    )


def test_emit_writes_four_files(om_setup, tmp_path: Path):
    _, mappings, tables, drift, locators = om_setup
    written = emit_openmetadata(tmp_path, mappings, tables, drift, locators)
    assert set(written) == {"glossary", "glossary_terms", "tables", "lineage"}
    for path in written.values():
        payload = json.loads(Path(path).read_text())
        assert payload["omSchemaVersion"] == OM_SCHEMA_VERSION
