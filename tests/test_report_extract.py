"""Module B acceptance: >=95% of gold-labeled metric instances extracted
with correct value, scale, as-of date, and dimension context."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from canoniq.extract.prose import (
    mine_column_glossary,
    mine_formula_hypotheses,
    mine_metric_statements,
)
from canoniq.extract.report import extract_report


def _extract(brownfield_root: Path, report_id: str):
    return extract_report(
        str(brownfield_root / "reports" / f"{report_id}.pdf"), report_id=report_id
    )


def test_extraction_recall_meets_95_percent(brownfield_root: Path):
    gold = yaml.safe_load((brownfield_root / "gold_labels.yaml").read_text())
    for report_id, report in gold["reports"].items():
        extraction = _extract(brownfield_root, report_id)
        extracted = {
            (
                inst.metric_name_verbatim,
                tuple(sorted(inst.dimension_context.items())),
                inst.as_of_date.isoformat(),
            ): inst.raw_value
            for inst in extraction.instances
        }

        gold_instances = [
            (metric["name"], inst)
            for metric in report["metrics"]
            for inst in metric["instances"]
        ]
        hits = 0
        for name, inst in gold_instances:
            key = (name, tuple(sorted(inst["dims"].items())), inst["as_of"])
            raw = extracted.get(key)
            if raw is not None and abs(raw - Decimal(inst["raw"])) <= Decimal("0.01") * abs(
                Decimal(inst["raw"])
            ):
                hits += 1
        recall = hits / len(gold_instances)
        assert recall >= 0.95, f"{report_id}: extraction recall {recall:.2%}"


def test_breakdowns_link_to_their_totals(brownfield_root: Path):
    extraction = _extract(brownfield_root, "sor_2026q1")
    by_id = {i.instance_id: i for i in extraction.instances}
    children = [i for i in extraction.instances if i.parent_total_id]
    assert len(children) >= 15
    for child in children:
        parent = by_id[child.parent_total_id]
        assert parent.metric_name_verbatim == child.metric_name_verbatim
        assert parent.dimension_context == {}


def test_no_internal_inconsistencies_in_clean_report(brownfield_root: Path):
    extraction = _extract(brownfield_root, "sor_2026q1")
    assert extraction.inconsistencies == []


def test_unit_scale_detection(brownfield_root: Path):
    extraction = _extract(brownfield_root, "sor_2026q1")
    units = {i.metric_name_verbatim: (i.unit, i.scale_factor) for i in extraction.instances}
    assert units["Total Credit RWA"] == ("USD_mm", Decimal("1e6"))
    assert units["Adjusted Stress Capital Buffer"] == ("pct", Decimal("1"))


def test_footnote_instances_carry_prior_as_of(brownfield_root: Path):
    extraction = _extract(brownfield_root, "sor_2026q1")
    priors = [i for i in extraction.instances if i.as_of_date == date(2025, 12, 31)]
    assert len(priors) == 5
    assert all(i.dimension_context == {} for i in priors)


def test_prose_formula_mining():
    hyps = mine_formula_hypotheses(
        "Total Credit Risk Exposure is calculated as gross exposure less "
        "collateral held, measured across the portfolio."
    )
    assert len(hyps) == 1
    assert hyps[0].structure == "A - B"
    assert hyps[0].term_descriptions == ["gross exposure", "collateral held"]

    hyps = mine_formula_hypotheses(
        "Funding Ratio is calculated as stable funding divided by required "
        "funding, per the policy."
    )
    assert hyps[0].structure == "A / B"


def test_metric_statement_and_glossary_mining():
    statements = mine_metric_statements(
        "Total Credit Risk Exposure is defined as the aggregate gross "
        "exposure amount across all counterparties."
    )
    assert statements and statements[0][0] == "Total Credit Risk Exposure"

    glossary = mine_column_glossary(
        "- CRD_EXP_FCT.EXP_AMT_USD: gross exposure amount in USD\n"
        "- CRD_EXP_FCT.COLL_HELD_AMT: collateral held against the exposure\n"
    )
    assert glossary[("CRD_EXP_FCT", "EXP_AMT_USD")].startswith("gross exposure")


def test_post_validation_rejects_invented_values(brownfield_root: Path):
    """An LLM structuring pass that hallucinates a value must be caught by
    the literal-presence check."""
    from canoniq.extract import report as report_mod

    pages = report_mod._read_pages(
        str(brownfield_root / "reports" / "sor_2026q1.pdf")
    )
    fake = report_mod._make_instance(
        "sor_2026q1", "Total Credit RWA", Decimal("99999.9"), "USD millions",
        date(2026, 3, 31), {}, "page 3 (llm-structured)",
    )
    real = report_mod._structure_tables_deterministic("sor_2026q1", date(2026, 3, 31), pages)
    kept = report_mod._post_validate(real + [fake], pages)
    assert fake not in kept
    assert len(kept) == len(real)
