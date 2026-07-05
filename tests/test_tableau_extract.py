"""Module C: Tableau .twb extraction."""

from pathlib import Path

import pytest

from canoniq.extract.tableau import (
    TableauFormulaError,
    extract_from_twb,
    translate_formula,
)
from canoniq.ingest.base import SourceType
from canoniq.ranking.ontorank import SOURCE_AUTHORITY


def test_extracts_calculated_fields(brownfield_root: Path):
    evidence = extract_from_twb(str(brownfield_root / "tableau" / "risk_dashboard.twb"))
    by_caption = {e.caption: e for e in evidence}

    # the malformed WINDOW_SUM field is skipped, never a crash
    assert "Broken Window Calc" not in by_caption
    assert set(by_caption) == {
        "Net Credit Exposure",
        "Total Credit Risk Exposure",
        "Collateral Coverage Ratio",
    }

    nce = by_caption["Net Credit Exposure"]
    assert nce.physical_expr_sql == "SUM(EXP_AMT_USD) - SUM(COLL_HELD_AMT)"
    assert nce.referenced_columns == ["EXP_AMT_USD", "COLL_HELD_AMT"]
    assert "Exposure Overview" in nce.worksheet_names

    ccr = by_caption["Collateral Coverage Ratio"]
    assert ccr.physical_expr_sql == "SUM(COLL_HELD_AMT) / SUM(EXP_AMT_USD)"


def test_shelf_usage_role_hints(brownfield_root: Path):
    evidence = extract_from_twb(str(brownfield_root / "tableau" / "risk_dashboard.twb"))
    nce = next(e for e in evidence if e.caption == "Net Credit Exposure")
    assert nce.role_hints.get("LE_CD") == "dimension"
    assert nce.role_hints.get("Calculation_NCE") == "measure"


def test_translate_countd():
    sql, cols = translate_formula("COUNTD([CPTY_ID])")
    assert sql == "COUNT(DISTINCT CPTY_ID)"
    assert cols == ["CPTY_ID"]


@pytest.mark.parametrize(
    "formula",
    ["WINDOW_SUM(SUM([X]))", "RUNNING_SUM(SUM([X]))", "1 + 2", "SUM([A]) +"],
)
def test_malformed_or_unsupported_raises(formula: str):
    with pytest.raises(TableauFormulaError):
        translate_formula(formula)


def test_unparseable_file_returns_empty(tmp_path: Path):
    bad = tmp_path / "bad.twb"
    bad.write_text("<workbook><unclosed>")
    assert extract_from_twb(str(bad)) == []


def test_trust_prior_registered_below_steward():
    assert SourceType.TABLEAU_CALC == "tableau_calc"
    assert SOURCE_AUTHORITY["tableau_calc"] == 0.85
    assert SOURCE_AUTHORITY["tableau_calc"] < SOURCE_AUTHORITY["dbt_metric"]
    # empirical fingerprint evidence outranks every document source
    assert SOURCE_AUTHORITY["numeric_fingerprint"] == 0.98
    assert SOURCE_AUTHORITY["numeric_fingerprint"] > SOURCE_AUTHORITY["brd_approved"]
