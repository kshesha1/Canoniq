"""Module D acceptance + trap regressions.

D5: on the benchmark, all gold non-trap metrics reach CONFIRMED or
PROBABLE with the correct expression; both traps are REJECTED; the
spreadsheet-only metric lands UNMAPPABLE; the full run stays well inside
the 5-minute budget.
"""

import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from canoniq.extract.prose import mine_column_glossary
from canoniq.extract.report import extract_report
from canoniq.extract.tableau import extract_from_twb
from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import (
    IcebergCatalogAdapter,
    SnapshotNotFoundError,
    open_catalog,
)
from canoniq.fingerprint.executor import SnapshotExecutor
from canoniq.fingerprint.solver import (
    FingerprintContext,
    group_instances,
    resolve_all,
    resolve_dimension,
)
from canoniq.models import CandidateExpr, ConfidenceBand, SimplePredicate, Term

Q4 = date(2025, 12, 31)
Q1 = date(2026, 3, 31)


@pytest.fixture(scope="module")
def adapter(brownfield_root: Path) -> IcebergCatalogAdapter:
    return IcebergCatalogAdapter(open_catalog(brownfield_root / "warehouse"))


@pytest.fixture(scope="module")
def executor(adapter) -> SnapshotExecutor:
    return SnapshotExecutor(adapter, FingerprintConfig())


@pytest.fixture(scope="module")
def resolutions(brownfield_root: Path, adapter, executor):
    """Full fingerprint run over both editions; shared across tests."""
    tableau = extract_from_twb(str(brownfield_root / "tableau" / "risk_dashboard.twb"))
    glossary = mine_column_glossary(
        (brownfield_root / "docs" / "brd_credit_exposure_mart.md").read_text()
    )
    start = time.monotonic()
    out = {}
    for report_id in ("sor_2025q4", "sor_2026q1"):
        extraction = extract_report(
            str(brownfield_root / "reports" / f"{report_id}.pdf"), report_id=report_id
        )
        ctx = FingerprintContext(
            adapter=adapter,
            executor=executor,
            config=FingerprintConfig(),
            hypotheses=extraction.formula_hypotheses,
            tableau_evidence=tableau,
            glossary=glossary,
        )
        out[report_id] = {m.metric_name: m for m in resolve_all(extraction.instances, ctx)}
    out["elapsed"] = time.monotonic() - start
    return out


# --- D0 executor ----------------------------------------------------------------


def _sum(table: str, column: str, **kwargs) -> CandidateExpr:
    return CandidateExpr(lhs=Term(agg="SUM", table=table, column=column), **kwargs)


def test_executor_targets_the_matching_snapshot(executor):
    q4_total = executor.evaluate(_sum("RWA_CALC_FCT", "RWA_AMT_V3"), Q4)
    q1_total = executor.evaluate(_sum("RWA_CALC_FCT", "RWA_AMT_V3"), Q1)
    assert isinstance(q4_total, Decimal) and isinstance(q1_total, Decimal)
    assert q4_total != q1_total  # quarter-over-quarter drift is real


def test_executor_reads_pre_rename_snapshot_under_current_name(executor):
    """EXP_AMT_USD did not exist at the Q4 snapshot (it was EXP_AMT);
    field-id normalization must make the query work anyway."""
    value = executor.evaluate(_sum("CRD_EXP_FCT", "EXP_AMT_USD"), Q4)
    assert isinstance(value, Decimal) and value > 0


def test_executor_fails_loudly_without_matching_snapshot(executor):
    with pytest.raises(SnapshotNotFoundError):
        executor.evaluate(_sum("RWA_CALC_FCT", "RWA_AMT_V3"), date(2024, 6, 30))


def test_tolerance_uses_decimal(executor):
    ok, err = executor.matches(Decimal("1004.9"), Decimal("1000"))
    assert ok and err == Decimal("4.9") / Decimal("1000")
    ok, _ = executor.matches(Decimal("1005.1"), Decimal("1000"))
    assert not ok


def test_two_term_and_filtered_evaluation(executor):
    net = executor.evaluate(
        CandidateExpr(
            lhs=Term(agg="SUM", table="CRD_EXP_FCT", column="EXP_AMT_USD"),
            op="-",
            rhs=Term(agg="SUM", table="CRD_EXP_FCT", column="COLL_HELD_AMT"),
        ),
        Q1,
    )
    gross = executor.evaluate(_sum("CRD_EXP_FCT", "EXP_AMT_USD"), Q1)
    assert Decimal("0") < net < gross

    filtered = executor.evaluate(
        _sum(
            "MKT_RSK_SNSTVTY", "SNSTVTY_AMT",
            predicate=SimplePredicate(column="RSK_FCTR_CD", op="<>", value="IR_VEGA"),
        ),
        Q1,
    )
    unfiltered = executor.evaluate(_sum("MKT_RSK_SNSTVTY", "SNSTVTY_AMT"), Q1)
    assert filtered < unfiltered


def test_result_cache_hits(executor):
    expr = _sum("OPS_LOSS_EVT", "LOSS_AMT")
    first = executor.evaluate(expr, Q1)
    assert (expr.canonical_key(), Q1, None) in executor._cache
    assert executor.evaluate(expr, Q1) == first


# --- grammar -------------------------------------------------------------------


def test_grammar_rejects_filtered_two_term():
    with pytest.raises(ValueError):
        CandidateExpr(
            lhs=Term(agg="SUM", table="T", column="A"),
            op="-",
            rhs=Term(agg="SUM", table="T", column="B"),
            predicate=SimplePredicate(column="C", op="=", value="x"),
        )


def test_grammar_rejects_star_outside_count():
    with pytest.raises(ValueError):
        CandidateExpr(lhs=Term(agg="SUM", table="T", column="*"))


# --- dimension resolution ---------------------------------------------------------


def test_dimension_resolution_via_join(adapter):
    binding = resolve_dimension(
        "legal_entity",
        ["Meridian NY", "Meridian London", "Meridian Singapore", "Meridian Frankfurt"],
        "RWA_CALC_FCT",
        adapter,
        Q1,
    )
    assert binding is not None
    assert binding.group_table == "LE_REF" and binding.group_column == "LE_NM"
    assert binding.join_from == "RWA_CALC_FCT.LE_CD"
    assert binding.label_to_value["Meridian NY"] == "Meridian NY"


def test_dimension_resolution_code_in_label(adapter):
    binding = resolve_dimension(
        "event_type",
        ["Internal Fraud (INT_FRD)", "External Fraud (EXT_FRD)",
         "System Failure (SYS_FAIL)", "Process Error (PROC_ERR)"],
        "OPS_LOSS_EVT",
        adapter,
        Q1,
    )
    assert binding is not None
    assert binding.group_table == "OPS_LOSS_EVT"
    assert binding.group_column == "EVT_TYP_CD"
    assert binding.label_to_value["Internal Fraud (INT_FRD)"] == "INT_FRD"


# --- D5 acceptance -----------------------------------------------------------------


def test_all_gold_metrics_resolve_correctly(brownfield_root: Path, resolutions):
    gold = yaml.safe_load((brownfield_root / "gold_labels.yaml").read_text())
    for report_id, report in gold["reports"].items():
        for metric in report["metrics"]:
            mapping = resolutions[report_id][metric["name"]]
            if metric["expression"] == "unmappable":
                assert mapping.band == ConfidenceBand.UNMAPPABLE, metric["name"]
                assert mapping.best is None
            else:
                assert mapping.band in (
                    ConfidenceBand.CONFIRMED, ConfidenceBand.PROBABLE
                ), f"{report_id}/{metric['name']}: {mapping.band}"
                assert mapping.best.expr.canonical_key() == metric["expression"], (
                    f"{report_id}/{metric['name']}"
                )
                assert mapping.band == ConfidenceBand[metric["expected_band"]]


def test_deprecated_twin_rejected_on_breakdowns(resolutions):
    """Trap #1 regression: the twin passes the grand total (within
    tolerance by construction) yet must be REJECTED on breakdown
    constraints, while RWA_AMT_V3 is CONFIRMED."""
    for report_id in ("sor_2025q4", "sor_2026q1"):
        mapping = resolutions[report_id]["Total Credit RWA"]
        assert mapping.band == ConfidenceBand.CONFIRMED
        assert "RWA_AMT_V3" in mapping.best.expr.canonical_key()
        twins = [
            r for r in mapping.rejected
            if "RWA_AMT_V2_DEPR" in r.expr.canonical_key()
        ]
        assert twins, f"{report_id}: twin was never evaluated"
        twin = twins[0]
        assert twin.band == ConfidenceBand.REJECTED
        grand = next(c for c in twin.constraints if c.kind == "grand_total")
        assert grand.satisfied, "twin is built to pass the grand total"
        breakdowns = [c for c in twin.constraints if c.kind == "breakdown"]
        assert breakdowns and not any(c.satisfied for c in breakdowns)


def test_decoy_column_rejected(resolutions):
    """Trap #2 regression: the decoy coincides with the Q1 ops-losses
    total but fails every dimensional breakdown."""
    mapping = resolutions["sor_2026q1"]["Operational Losses"]
    assert mapping.band == ConfidenceBand.CONFIRMED
    decoys = [
        r for r in mapping.rejected if "HDG_NTNL_AMT" in r.expr.canonical_key()
    ]
    assert decoys, "decoy was never evaluated"
    assert decoys[0].band == ConfidenceBand.REJECTED


def test_silent_redefinition_resolved_with_filter(resolutions):
    q1 = resolutions["sor_2026q1"]["Market Risk Sensitivities"]
    assert q1.band == ConfidenceBand.CONFIRMED
    assert q1.best.expr.predicate is not None
    assert q1.best.expr.predicate.value == "IR_VEGA"
    assert "tier3" in q1.tiers_run


def test_unmappable_reports_what_was_tried(resolutions):
    mapping = resolutions["sor_2026q1"]["Adjusted Stress Capital Buffer"]
    assert mapping.band == ConfidenceBand.UNMAPPABLE
    assert set(mapping.tiers_run) == {"tier1", "tier2", "tier3"}
    assert mapping.near_miss is not None
    near_err = mapping.near_miss.constraints[0].relative_error
    assert near_err is not None and near_err > Decimal("0.005")


def test_full_run_within_time_budget(resolutions):
    assert resolutions["elapsed"] < 300  # spec budget: 5 minutes


def test_group_instances_links_breakdowns_and_priors(brownfield_root: Path):
    extraction = extract_report(
        str(brownfield_root / "reports" / "sor_2026q1.pdf"), report_id="sor_2026q1"
    )
    groups = {g.metric_name: g for g in group_instances(extraction.instances)}
    rwa = groups["Total Credit RWA"]
    assert rwa.grand_total.dimension_context == {}
    assert len(rwa.breakdowns) == 7  # 4 legal entities + 3 asset classes
    assert len(rwa.priors) == 1 and rwa.priors[0].as_of_date == Q4
