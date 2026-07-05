"""Two editions of the synthetic board report, rendered to PDF.

The edition structures built here are the single source of truth for what
the report says: the PDF writer renders them and the gold-label writer
(artifacts.py) derives expected extraction results from them.

Text and tables only — no charts or images (chart OCR is out of scope).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from benchmark.brownfield.data import (
    Q1_END,
    Q1_EXCLUDED_RISK_FACTOR,
    Q4_END,
    BenchmarkData,
    _dsum,
)

# Deterministic PDFs: no timestamps / random IDs embedded.
rl_config.invariant = 1

MM = Decimal("1e6")


def fmt_mm(raw: Decimal) -> str:
    """Format a raw USD value in millions with one decimal, as printed."""
    return f"{raw / MM:,.1f}"


def fmt_pct(value: Decimal) -> str:
    return f"{value:.2f}"


@dataclass
class MetricTableSpec:
    metric_name: str                   # as printed in the table title
    dim_label: str | None              # "Legal Entity" | "Asset Class" | ... | None
    unit_label: str                    # "USD millions" | "%"
    rows: list[tuple[str, str]]        # (row label, printed value string)
    total: str | None                  # printed grand-total string ("Total" row)


@dataclass
class MetricSectionSpec:
    metric_name: str
    prose: str                         # commentary paragraph (formula hints live here)
    tables: list[MetricTableSpec]
    filler: str = ""                   # extra neutral commentary


@dataclass
class FootnoteSpec:
    metric_name: str
    prior_as_of: date
    printed_value: str
    unit_label: str


@dataclass
class ReportEdition:
    report_id: str
    as_of: date
    period_label: str
    sections: list[MetricSectionSpec]
    footnotes: list[FootnoteSpec] = field(default_factory=list)


_FILLER = (
    "Management reviewed the figures presented in this section as part of the "
    "quarterly risk governance cycle. Movements against the prior period are "
    "primarily attributable to ordinary business growth and market conditions. "
    "No remediation items were raised against the underlying data at the "
    "reporting date, and reconciliation to the general ledger was completed "
    "within the standard close timetable."
)


def build_editions(bench: BenchmarkData) -> tuple[ReportEdition, ReportEdition]:
    q4f, q1f = bench.q4_figures, bench.q1_figures

    # The Q1 footnote for Market Risk Sensitivities restates the prior
    # quarter under the (silently changed) new definition, so the correct
    # candidate mapping satisfies the prior-period constraint too.
    mrs_q4_restated = _dsum(
        [
            r
            for r in bench.q4.mkt_rsk_snstvty
            if r["RSK_FCTR_CD"] != Q1_EXCLUDED_RISK_FACTOR
        ],
        "SNSTVTY_AMT",
    )

    def usd_table(
        name: str, dim: str, by: dict[str, Decimal], total: Decimal
    ) -> MetricTableSpec:
        return MetricTableSpec(
            metric_name=name,
            dim_label=dim,
            unit_label="USD millions",
            rows=[(label, fmt_mm(v)) for label, v in by.items()],
            total=fmt_mm(total),
        )

    def sections(f, is_q1: bool) -> list[MetricSectionSpec]:
        ace_name = "Adjusted Counterparty Exposure" if is_q1 else "Counterparty Credit Exposure"
        return [
            MetricSectionSpec(
                metric_name="Total Credit RWA",
                prose=(
                    "Total Credit RWA is the sum of risk-weighted assets computed "
                    "under the current internal framework, aggregated across all "
                    "legal entities and asset classes."
                ),
                tables=[
                    usd_table("Total Credit RWA", "Legal Entity", f.rwa_by_le, f.rwa_total),
                    usd_table("Total Credit RWA", "Asset Class", f.rwa_by_cls, f.rwa_total),
                ],
                filler=_FILLER,
            ),
            MetricSectionSpec(
                metric_name="Total Credit Risk Exposure",
                prose=(
                    "Total Credit Risk Exposure is calculated as gross exposure "
                    "less collateral held, measured across the consolidated "
                    "counterparty portfolio."
                ),
                tables=[
                    usd_table(
                        "Total Credit Risk Exposure", "Legal Entity", f.cre_by_le, f.cre_total
                    )
                ],
                filler=_FILLER,
            ),
            MetricSectionSpec(
                metric_name=ace_name,
                prose=(
                    f"{ace_name} represents gross exposure measured before "
                    "collateral offsets, presented by asset class."
                ),
                tables=[usd_table(ace_name, "Asset Class", f.ace_by_cls, f.ace_total)],
                filler=_FILLER,
            ),
            MetricSectionSpec(
                metric_name="Operational Losses",
                prose=(
                    "Operational Losses represent the sum of loss amounts recorded "
                    "on operational loss events during the quarter."
                ),
                tables=[
                    usd_table("Operational Losses", "Event Type", f.ops_by_evt, f.ops_total)
                ],
                filler=_FILLER,
            ),
            MetricSectionSpec(
                metric_name="Market Risk Sensitivities",
                # Identical prose in both editions — the Q1 definitional
                # change (excluding one risk factor) is documented NOWHERE.
                prose=(
                    "Market Risk Sensitivities aggregate first-order risk factor "
                    "sensitivities across trading desks."
                ),
                tables=[
                    usd_table(
                        "Market Risk Sensitivities", "Legal Entity", f.mrs_by_le, f.mrs_total
                    )
                ],
                filler=_FILLER,
            ),
            MetricSectionSpec(
                metric_name="Adjusted Stress Capital Buffer",
                prose=(
                    "The Adjusted Stress Capital Buffer reflects supervisory stress "
                    "projections combined with management overlay adjustments."
                ),
                tables=[
                    MetricTableSpec(
                        metric_name="Capital Adequacy Metrics",
                        dim_label=None,
                        unit_label="%",
                        rows=[("Adjusted Stress Capital Buffer", fmt_pct(f.ascb_pct))],
                        total=None,
                    )
                ],
                filler=_FILLER,
            ),
        ]

    q4_edition = ReportEdition(
        report_id="sor_2025q4",
        as_of=Q4_END,
        period_label="Quarter ended December 31, 2025",
        sections=sections(q4f, is_q1=False),
        footnotes=[],  # first edition in the new format: no comparatives
    )

    q1_edition = ReportEdition(
        report_id="sor_2026q1",
        as_of=Q1_END,
        period_label="Quarter ended March 31, 2026",
        sections=sections(q1f, is_q1=True),
        footnotes=[
            FootnoteSpec("Total Credit RWA", Q4_END, fmt_mm(q4f.rwa_total), "USD millions"),
            FootnoteSpec(
                "Total Credit Risk Exposure", Q4_END, fmt_mm(q4f.cre_total), "USD millions"
            ),
            FootnoteSpec(
                "Adjusted Counterparty Exposure", Q4_END, fmt_mm(q4f.ace_total), "USD millions"
            ),
            FootnoteSpec("Operational Losses", Q4_END, fmt_mm(q4f.ops_total), "USD millions"),
            FootnoteSpec(
                "Market Risk Sensitivities", Q4_END, fmt_mm(mrs_q4_restated), "USD millions"
            ),
        ],
    )
    return q4_edition, q1_edition


# --- PDF rendering -----------------------------------------------------------

_TABLE_STYLE = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde4ee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
    ]
)


def _prior_label(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def write_report_pdf(edition: ReportEdition, path: str) -> None:
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    table_title = ParagraphStyle(
        "TableTitle", parent=styles["Heading4"], spaceBefore=10, spaceAfter=4
    )

    story: list = [
        Spacer(1, 2 * inch),
        Paragraph("State of Risk Report", title_style),
        Paragraph("Meridian Bancorp (fictional)", styles["Heading1"]),
        Spacer(1, 0.3 * inch),
        Paragraph(edition.period_label, h2),
        Paragraph(f"As of {_prior_label(edition.as_of)}", h2),
        Spacer(1, 0.5 * inch),
        Paragraph(
            "Prepared by Group Risk. This is a fully synthetic benchmark "
            "document; all names and figures are fictional.",
            body,
        ),
        PageBreak(),
        Paragraph("Executive Summary", h2),
        Paragraph(
            "The Group's risk profile remained within approved appetite during "
            "the period. The sections that follow present the Group's principal "
            "risk metrics with breakdowns by legal entity, asset class and "
            "event type, as approved by the Board Risk Committee.",
            body,
        ),
        Paragraph(_FILLER, body),
        PageBreak(),
    ]

    table_no = 0
    for section in edition.sections:
        story.append(Paragraph(section.metric_name, h2))
        story.append(Paragraph(section.prose, body))
        for spec in section.tables:
            table_no += 1
            if spec.dim_label:
                title = (
                    f"Table {table_no}: {spec.metric_name} by {spec.dim_label} "
                    f"({spec.unit_label})"
                )
                header = [spec.dim_label, f"Amount ({spec.unit_label})"]
            else:
                title = f"Table {table_no}: {spec.metric_name} ({spec.unit_label})"
                header = ["Metric", f"Value ({spec.unit_label})"]
            cells = [header] + [[label, value] for label, value in spec.rows]
            if spec.total is not None:
                cells.append(["Total", spec.total])
            table = Table(cells, colWidths=[3.2 * inch, 2.0 * inch])
            table.setStyle(_TABLE_STYLE)
            story.append(KeepTogether([Paragraph(title, table_title), table]))
        if section.filler:
            story.append(Paragraph(section.filler, body))
        story.append(PageBreak())

    if edition.footnotes:
        story.append(Paragraph("Prior-Quarter Comparatives", h2))
        story.append(
            Paragraph(
                "The following comparatives are presented for the major metrics "
                "reported above.",
                body,
            )
        )
        for fn in edition.footnotes:
            story.append(
                Paragraph(
                    f"{fn.metric_name} — prior quarter (as of "
                    f"{_prior_label(fn.prior_as_of)}): {fn.printed_value} "
                    f"{fn.unit_label}",
                    body,
                )
            )
        story.append(PageBreak())

    story.append(Paragraph("Methodology Notes", h2))
    for _ in range(4):
        story.append(Paragraph(_FILLER, body))
    story.append(PageBreak())
    story.append(Paragraph("Governance and Data Quality", h2))
    for _ in range(4):
        story.append(Paragraph(_FILLER, body))
    story.append(PageBreak())
    story.append(Paragraph("Regulatory Developments", h2))
    for _ in range(4):
        story.append(Paragraph(_FILLER, body))
    story.append(PageBreak())
    story.append(Paragraph("Outlook", h2))
    for _ in range(3):
        story.append(Paragraph(_FILLER, body))

    doc = SimpleDocTemplate(
        path,
        pagesize=letter,
        title=f"State of Risk Report — {edition.period_label}",
        author="Group Risk (fictional)",
    )
    doc.build(story)
