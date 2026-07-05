"""Supporting benchmark artifacts: Tableau workbook, policy documents,
sparse BRD, and the gold-labels file used by the eval harness.

Gold-label instances are derived from the SAME edition structures the PDF
writer renders, so the expected extraction output can never drift from
what the PDFs actually say.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from reportlab import rl_config
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from benchmark.brownfield.report_pdf import ReportEdition

rl_config.invariant = 1

# --- Tableau workbook --------------------------------------------------------

# Three calculated fields:
#   - "Net Credit Exposure" matches the report's Total Credit Risk Exposure
#     logic (SUM(EXP_AMT_USD) - SUM(COLL_HELD_AMT)).
#   - "Total Credit Risk Exposure" is defined WITHOUT collateral netting —
#     deliberately conflicting with the 2024 policy document.
#   - "Collateral Coverage Ratio" is a ratio field.
# Plus one malformed formula the extractor must log-and-skip, never crash on.
_TWB_XML = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1.0' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <datasources>
    <datasource caption='Risk Mart' inline='true' name='federated.riskmart' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='risk_mart' name='iceberg.riskmart'>
            <connection class='iceberg' dbname='risk_mart' schema='risk_mart' server='localhost' />
          </named-connection>
        </named-connections>
        <relation connection='iceberg.riskmart' name='CRD_EXP_FCT' table='[risk_mart].[CRD_EXP_FCT]' type='table' />
      </connection>
      <column caption='Net Credit Exposure' datatype='real' name='[Calculation_NCE]' role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([EXP_AMT_USD]) - SUM([COLL_HELD_AMT])' />
      </column>
      <column caption='Total Credit Risk Exposure' datatype='real' name='[Calculation_TCRE]' role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([EXP_AMT_USD])' />
      </column>
      <column caption='Collateral Coverage Ratio' datatype='real' name='[Calculation_CCR]' role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([COLL_HELD_AMT]) / SUM([EXP_AMT_USD])' />
      </column>
      <column caption='Broken Window Calc' datatype='real' name='[Calculation_BROKEN]' role='measure' type='quantitative'>
        <calculation class='tableau' formula='WINDOW_SUM(SUM([EXP_AMT_USD]), FIRST(), LAST(' />
      </column>
      <column caption='Legal Entity' datatype='string' name='[LE_CD]' role='dimension' type='nominal' />
      <column caption='Asset Class' datatype='string' name='[ASST_CLS_CD]' role='dimension' type='nominal' />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Exposure Overview'>
      <table>
        <view>
          <datasource-dependencies datasource='federated.riskmart'>
            <column datatype='string' name='[LE_CD]' role='dimension' type='nominal' />
            <column datatype='real' name='[Calculation_NCE]' role='measure' type='quantitative' />
            <column datatype='real' name='[Calculation_TCRE]' role='measure' type='quantitative' />
          </datasource-dependencies>
        </view>
      </table>
    </worksheet>
    <worksheet name='Coverage by Asset Class'>
      <table>
        <view>
          <datasource-dependencies datasource='federated.riskmart'>
            <column datatype='string' name='[ASST_CLS_CD]' role='dimension' type='nominal' />
            <column datatype='real' name='[Calculation_CCR]' role='measure' type='quantitative' />
          </datasource-dependencies>
        </view>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def write_tableau_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TWB_XML)


# --- policy documents + BRD --------------------------------------------------

POLICY_2019_MD = """\
# Credit Risk Measurement Policy

Version 3.2 — Effective June 1, 2019
Document owner: Group Credit Risk

## Exposure Measurement

Total Credit Risk Exposure is defined as the aggregate gross exposure
amount across all counterparties. Collateral held is reported separately
and is not netted against exposure for reporting purposes.

Exposure amounts are sourced from the credit exposure fact table at each
reporting date and aggregated across legal entities.

## Review

This policy is reviewed annually by the Credit Risk Committee.
"""

POLICY_2024_MD = """\
# Risk Data Aggregation Policy

Version 1.4 — Effective March 1, 2024
Approved by: Chief Risk Officer
Document owner: Risk Data Office

## Metric Definitions

Total Credit Risk Exposure is calculated as gross exposure less eligible
collateral held. Netting of collateral is mandatory for all board-level
reporting from Q2 2024 onward.

Operational Losses represent the sum of loss amounts recorded on
operational loss events during the reporting quarter.

## Review

This policy supersedes conflicting definitions in earlier policies.
"""

BRD_MD = """\
# Business Requirements Document — Credit Exposure Mart (Draft)

Status: Draft for review
Scope note: this document covers the credit exposure fact table and the
legal entity reference table only. Other risk mart tables are out of
scope for this phase.

## CRD_EXP_FCT — credit exposure fact

- `CRD_EXP_FCT.EXP_AMT_USD`: gross exposure amount in USD for the counterparty facility
- `CRD_EXP_FCT.COLL_HELD_AMT`: collateral held against the exposure, in USD
- `CRD_EXP_FCT.LE_CD`: legal entity code; joins to LE_REF
- `CRD_EXP_FCT.ASST_CLS_CD`: asset class code; joins to ASST_CLS_REF
- `CRD_EXP_FCT.AS_OF_DT`: reporting as-of date
- `CRD_EXP_FCT.CPTY_ID`: counterparty identifier

## LE_REF — legal entity reference

- `LE_REF.LE_CD`: legal entity code (primary key)
- `LE_REF.LE_NM`: legal entity name as used in board reporting
- `LE_REF.RGN_CD`: region code
"""

DOC_FILES: dict[str, str] = {
    "policy_credit_risk_2019": POLICY_2019_MD,
    "policy_rda_2024": POLICY_2024_MD,
    "brd_credit_exposure_mart": BRD_MD,
}


def _markdown_to_pdf(markdown_text: str, path: Path) -> None:
    """Minimal markdown -> PDF rendering (headings + paragraphs + bullets).
    Enough for pdfplumber/pypdf to recover the text verbatim."""
    styles = getSampleStyleSheet()
    story: list = []
    for block in markdown_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("# "):
            story.append(Paragraph(block[2:], styles["Title"]))
        elif block.startswith("## "):
            story.append(Paragraph(block[3:], styles["Heading2"]))
        elif block.startswith("- "):
            for line in block.splitlines():
                story.append(
                    Paragraph(line[2:].replace("`", ""), styles["BodyText"])
                )
        else:
            story.append(Paragraph(block.replace("\n", " "), styles["BodyText"]))
        story.append(Spacer(1, 6))
    SimpleDocTemplate(str(path), pagesize=letter).build(story)


def write_documents(docs_dir: Path) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    for stem, text in DOC_FILES.items():
        (docs_dir / f"{stem}.md").write_text(text)
        _markdown_to_pdf(text, docs_dir / f"{stem}.pdf")


# --- gold labels -------------------------------------------------------------

GOLD_EXPRESSIONS_Q4: dict[str, str] = {
    "Total Credit RWA": "SUM(RWA_CALC_FCT.RWA_AMT_V3)",
    "Total Credit Risk Exposure": (
        "SUM(CRD_EXP_FCT.EXP_AMT_USD) - SUM(CRD_EXP_FCT.COLL_HELD_AMT)"
    ),
    "Counterparty Credit Exposure": "SUM(CRD_EXP_FCT.EXP_AMT_USD)",
    "Operational Losses": "SUM(OPS_LOSS_EVT.LOSS_AMT)",
    "Market Risk Sensitivities": "SUM(MKT_RSK_SNSTVTY.SNSTVTY_AMT)",
    "Adjusted Stress Capital Buffer": "unmappable",
}

GOLD_EXPRESSIONS_Q1: dict[str, str] = {
    "Total Credit RWA": "SUM(RWA_CALC_FCT.RWA_AMT_V3)",
    "Total Credit Risk Exposure": (
        "SUM(CRD_EXP_FCT.EXP_AMT_USD) - SUM(CRD_EXP_FCT.COLL_HELD_AMT)"
    ),
    "Adjusted Counterparty Exposure": "SUM(CRD_EXP_FCT.EXP_AMT_USD)",
    "Operational Losses": "SUM(OPS_LOSS_EVT.LOSS_AMT)",
    "Market Risk Sensitivities": (
        "SUM(MKT_RSK_SNSTVTY.SNSTVTY_AMT) WHERE RSK_FCTR_CD<>'IR_VEGA'"
    ),
    "Adjusted Stress Capital Buffer": "unmappable",
}


def dim_key_from_label(dim_label: str) -> str:
    return dim_label.lower().replace(" ", "_")


def _unit_and_scale(unit_label: str) -> tuple[str, str]:
    if unit_label == "USD millions":
        return "USD_mm", "1000000"
    if unit_label == "%":
        return "pct", "1"
    raise ValueError(f"unknown unit label: {unit_label}")


def _parse_printed(printed: str) -> Decimal:
    return Decimal(printed.replace(",", ""))


def edition_instances(edition: ReportEdition) -> dict[str, list[dict]]:
    """Expected extraction output per metric, derived from the edition spec.
    Values are what a correct extractor should produce: the printed number
    times its scale factor."""
    per_metric: dict[str, dict[tuple, dict]] = {}

    def add(metric: str, dims: dict[str, str], as_of: date, printed: str, unit_label: str):
        unit, scale = _unit_and_scale(unit_label)
        key = (tuple(sorted(dims.items())), as_of.isoformat(), printed)
        per_metric.setdefault(metric, {})[key] = {
            "dims": dims,
            "as_of": as_of.isoformat(),
            "printed": printed,
            "raw": str(_parse_printed(printed) * Decimal(scale)),
            "unit": unit,
        }

    for section in edition.sections:
        for spec in section.tables:
            if spec.dim_label:
                dim_key = dim_key_from_label(spec.dim_label)
                for label, printed in spec.rows:
                    add(
                        spec.metric_name, {dim_key: label}, edition.as_of, printed,
                        spec.unit_label,
                    )
                if spec.total is not None:
                    add(spec.metric_name, {}, edition.as_of, spec.total, spec.unit_label)
            else:
                # rows are themselves grand-total metric instances
                for label, printed in spec.rows:
                    add(label, {}, edition.as_of, printed, spec.unit_label)

    for fn in edition.footnotes:
        add(fn.metric_name, {}, fn.prior_as_of, fn.printed_value, fn.unit_label)

    return {m: list(instances.values()) for m, instances in per_metric.items()}


def build_gold_labels(
    q4_edition: ReportEdition, q1_edition: ReportEdition
) -> dict:
    def report_block(edition: ReportEdition, expressions: dict[str, str]) -> dict:
        instances = edition_instances(edition)
        metrics = []
        for name, expr in expressions.items():
            metrics.append(
                {
                    "name": name,
                    "expression": expr,
                    "expected_band": "UNMAPPABLE" if expr == "unmappable" else "CONFIRMED",
                    "instances": instances[name],
                }
            )
        return {"as_of": edition.as_of.isoformat(), "metrics": metrics}

    return {
        "tolerance": "0.005",
        "reports": {
            "sor_2025q4": report_block(q4_edition, GOLD_EXPRESSIONS_Q4),
            "sor_2026q1": report_block(q1_edition, GOLD_EXPRESSIONS_Q1),
        },
        "traps": {
            "deprecated_twin": {
                "metric": "Total Credit RWA",
                "expression": "SUM(RWA_CALC_FCT.RWA_AMT_V2_DEPR)",
                "expected_band": "REJECTED",
            },
            "decoy_column": {
                "metric": "Operational Losses",
                "expression": "SUM(MKT_RSK_SNSTVTY.HDG_NTNL_AMT)",
                "expected_band": "REJECTED",
            },
        },
        "drift": [
            {
                "kind": "renamed",
                "old_name": "Counterparty Credit Exposure",
                "new_name": "Adjusted Counterparty Exposure",
            },
            {"kind": "redefined", "name": "Market Risk Sensitivities"},
        ],
        "schema_evolution": [
            {"table": "CRD_EXP_FCT", "renamed": {"from": "EXP_AMT", "to": "EXP_AMT_USD"}}
        ],
    }


def write_gold_labels(path: Path, q4_edition: ReportEdition, q1_edition: ReportEdition) -> None:
    labels = build_gold_labels(q4_edition, q1_edition)
    path.write_text(yaml.safe_dump(labels, sort_keys=False, allow_unicode=True))
