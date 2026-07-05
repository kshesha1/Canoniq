"""Report-first metric extraction (Module B).

Turns a board-report PDF into `ReportMetricInstance` rows plus
`FormulaHypothesis` seeds for Tier-3 fingerprinting.

Pipeline:
  1. pdfplumber extracts table structure + page text (no vision models).
  2. Structuring: deterministic table parser by default; an optional LLM
     pass (injectable client, same pattern as the existing proposer) for
     messier real-world layouts. Either way the LLM/parser only structures
     what is printed — every value is post-validated to literally appear
     in the source page text, and instances failing that check are dropped.
  3. Prose formula mining over commentary paragraphs.
  4. Deterministic consistency validation: sum(children) ~ parent.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pdfplumber

from canoniq.extract.prose import mine_formula_hypotheses
from canoniq.models import FormulaHypothesis, ReportMetricInstance

logger = logging.getLogger(__name__)

CONSISTENCY_TOLERANCE = Decimal("0.005")

_TABLE_TITLE_RE = re.compile(
    r"^Table\s+\d+:\s+(?P<metric>.+?)(?:\s+by\s+(?P<dim>[A-Za-z ]+?))?\s*"
    r"\((?P<unit>[^)]+)\)\s*$"
)
_AS_OF_RE = re.compile(r"As of\s+(?P<date>\w+\s+\d{1,2},\s+\d{4})")
_FOOTNOTE_RE = re.compile(
    r"(?P<name>[A-Z][A-Za-z0-9 /&-]+?)\s+[—–-]+\s+prior quarter\s+"
    r"\(as of\s+(?P<date>[^)]+)\):\s+(?P<value>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<unit>USD millions|USD billions|%)"
)
_NUMBER_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")

_UNIT_SCALES: dict[str, tuple[str, Decimal]] = {
    "USD millions": ("USD_mm", Decimal("1e6")),
    "USD billions": ("USD_bn", Decimal("1e9")),
    "%": ("pct", Decimal("1")),
    "count": ("count", Decimal("1")),
}


@dataclass
class ReportInconsistency:
    """A parent total that its own printed breakdown rows do not sum to."""

    kind: str
    metric_name: str
    parent_instance_id: str
    parent_value: Decimal
    children_sum: Decimal
    relative_error: Decimal
    source_locator: str


@dataclass
class ReportExtraction:
    report_id: str
    as_of_date: date
    instances: list[ReportMetricInstance] = field(default_factory=list)
    formula_hypotheses: list[FormulaHypothesis] = field(default_factory=list)
    inconsistencies: list[ReportInconsistency] = field(default_factory=list)
    full_text: str = ""


def _parse_date(text: str) -> date:
    return datetime.strptime(" ".join(text.split()), "%B %d, %Y").date()


def _parse_number(text: str) -> Decimal | None:
    text = text.strip()
    if not _NUMBER_RE.match(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _unit_and_scale(unit_label: str) -> tuple[str, Decimal]:
    return _UNIT_SCALES.get(unit_label.strip(), ("unknown", Decimal("1")))


def _dim_key(dim_label: str) -> str:
    return dim_label.strip().lower().replace(" ", "_")


@dataclass
class _PageContent:
    number: int                       # 1-based
    text: str
    tables: list[tuple[str | None, list[list[str]]]]  # (title line, grid)


def _read_pages(pdf_path: str) -> list[_PageContent]:
    pages: list[_PageContent] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            lines = page.extract_text_lines() or []
            tables = []
            for found in page.find_tables():
                grid = [
                    [(cell or "").strip() for cell in row] for row in found.extract()
                ]
                table_top = found.bbox[1]
                title = None
                best_bottom = None
                for line in lines:
                    if line["bottom"] <= table_top + 2 and re.match(
                        r"^Table\s+\d+:", line["text"].strip()
                    ):
                        if best_bottom is None or line["bottom"] > best_bottom:
                            best_bottom = line["bottom"]
                            title = line["text"].strip()
                tables.append((title, grid))
            pages.append(_PageContent(number=page_no, text=text, tables=tables))
    return pages


def _detect_as_of(pages: list[_PageContent]) -> date:
    for page in pages:
        m = _AS_OF_RE.search(page.text)
        if m:
            return _parse_date(m.group("date"))
    raise ValueError("could not detect report as-of date ('As of <date>' not found)")


def _make_instance(
    report_id: str,
    metric_name: str,
    value: Decimal,
    unit_label: str,
    as_of: date,
    dims: dict[str, str],
    locator: str,
    parent_total_id: str | None = None,
) -> ReportMetricInstance:
    unit, scale = _unit_and_scale(unit_label)
    return ReportMetricInstance(
        instance_id=ReportMetricInstance.make_instance_id(
            report_id, metric_name, as_of, dims, value
        ),
        report_id=report_id,
        metric_name_verbatim=metric_name,
        value=value,
        unit=unit,
        scale_factor=scale,
        as_of_date=as_of,
        dimension_context=dims,
        source_locator=locator,
        parent_total_id=parent_total_id,
    )


def _structure_tables_deterministic(
    report_id: str, as_of: date, pages: list[_PageContent]
) -> list[ReportMetricInstance]:
    instances: list[ReportMetricInstance] = []
    for page in pages:
        for table_idx, (title, grid) in enumerate(page.tables, start=1):
            if not title or len(grid) < 2:
                continue
            m = _TABLE_TITLE_RE.match(title)
            if not m:
                continue
            metric_name = m.group("metric").strip()
            dim_label = m.group("dim")
            unit_label = m.group("unit")
            body = grid[1:]  # drop header row

            def locator(row_idx: int, ti=table_idx, pn=page.number) -> str:
                return f"page {pn}, table {ti}, row {row_idx}"

            if dim_label:
                dim = _dim_key(dim_label)
                total_row = next(
                    (r for r in body if r and r[0].strip().lower() == "total"), None
                )
                parent = None
                if total_row is not None:
                    total_value = _parse_number(total_row[-1])
                    if total_value is not None:
                        parent = _make_instance(
                            report_id, metric_name, total_value, unit_label, as_of,
                            {}, locator(body.index(total_row) + 1),
                        )
                for row_idx, row in enumerate(body, start=1):
                    if row is total_row or not row or not row[0].strip():
                        continue
                    value = _parse_number(row[-1])
                    if value is None:
                        continue
                    instances.append(
                        _make_instance(
                            report_id, metric_name, value, unit_label, as_of,
                            {dim: row[0].strip()}, locator(row_idx),
                            parent_total_id=parent.instance_id if parent else None,
                        )
                    )
                if parent is not None:
                    instances.append(parent)
            else:
                # rows are themselves grand-total metrics (e.g. capital table)
                for row_idx, row in enumerate(body, start=1):
                    value = _parse_number(row[-1])
                    if value is None or not row[0].strip():
                        continue
                    instances.append(
                        _make_instance(
                            report_id, row[0].strip(), value, unit_label, as_of,
                            {}, locator(row_idx),
                        )
                    )
    return instances


def _extract_footnotes(
    report_id: str, pages: list[_PageContent]
) -> list[ReportMetricInstance]:
    instances = []
    for page in pages:
        for m in _FOOTNOTE_RE.finditer(" ".join(page.text.split())):
            value = _parse_number(m.group("value"))
            if value is None:
                continue
            instances.append(
                _make_instance(
                    report_id,
                    m.group("name").strip(),
                    value,
                    m.group("unit"),
                    _parse_date(m.group("date")),
                    {},
                    f"page {page.number}, prior-quarter comparatives",
                )
            )
    return instances


# --- optional LLM structuring pass -------------------------------------------

_LLM_SYSTEM = """\
You are a data structuring assistant. You convert extracted report tables
into structured metric instances. You NEVER invent values: every value you
output must appear verbatim in the provided source text. Return ONLY a JSON
array, no markdown."""

_LLM_USER_TEMPLATE = """\
Structure every figure in these extracted report tables into JSON objects:
  metric_name: metric name exactly as printed in the table title
  value: the printed number, as a string, exactly as printed
  unit_label: e.g. "USD millions" or "%"
  dims: object of dimension key -> row label ({{}} for totals)
  is_total: true for grand-total rows

Table titles and grids (page {page_no}):
{tables}

Page text:
---
{page_text}
---
Return ONLY a JSON array."""


def _structure_tables_llm(
    report_id: str,
    as_of: date,
    pages: list[_PageContent],
    client: Any,
    model: str,
) -> list[ReportMetricInstance]:
    """LLM structuring pass (Instructor-style JSON, injectable client).
    Values are post-validated against page text by the caller."""
    instances: list[ReportMetricInstance] = []
    for page in pages:
        if not page.tables:
            continue
        prompt = _LLM_USER_TEMPLATE.format(
            page_no=page.number,
            tables=json.dumps(
                [{"title": t, "grid": g} for t, g in page.tables], indent=1
            ),
            page_text=page.text[:8000],
        )
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                temperature=0,
                system=_LLM_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", response.content[0].text.strip())
            items = json.loads(raw)
        except Exception as exc:  # LLM structuring is best-effort
            logger.warning("LLM structuring failed on page %d: %s", page.number, exc)
            continue
        for item in items:
            value = _parse_number(str(item.get("value", "")))
            if value is None:
                continue
            instances.append(
                _make_instance(
                    report_id,
                    str(item.get("metric_name", "")).strip(),
                    value,
                    str(item.get("unit_label", "")),
                    as_of,
                    {str(k): str(v) for k, v in (item.get("dims") or {}).items()},
                    f"page {page.number} (llm-structured)",
                )
            )
    return instances


def _post_validate(
    instances: list[ReportMetricInstance], pages: list[_PageContent]
) -> list[ReportMetricInstance]:
    """Reject any instance whose printed value string is absent from the
    source text — the structuring pass may format, never invent."""
    all_text = "\n".join(p.text for p in pages)
    validated = []
    for inst in instances:
        printed_variants = {
            str(inst.value),
            f"{inst.value:,}",
            f"{inst.value:,.1f}",
            f"{inst.value:,.2f}",
        }
        if any(v in all_text for v in printed_variants):
            validated.append(inst)
        else:
            logger.warning(
                "dropping instance %s (%s): value %s not found in source text",
                inst.metric_name_verbatim, inst.source_locator, inst.value,
            )
    return validated


def _validate_consistency(
    instances: list[ReportMetricInstance],
) -> list[ReportInconsistency]:
    by_id = {i.instance_id: i for i in instances}
    children_by_parent: dict[str, list[ReportMetricInstance]] = {}
    for inst in instances:
        if inst.parent_total_id:
            children_by_parent.setdefault(inst.parent_total_id, []).append(inst)

    problems = []
    for parent_id, children in children_by_parent.items():
        parent = by_id.get(parent_id)
        if parent is None:
            continue
        # group children by dimension key: each dimension's rows must
        # independently sum to the parent
        by_dim: dict[str, list[ReportMetricInstance]] = {}
        for child in children:
            for key in child.dimension_context:
                by_dim.setdefault(key, []).append(child)
        for dim, rows in by_dim.items():
            total = sum((r.value for r in rows), Decimal("0"))
            rel = abs(total - parent.value) / abs(parent.value)
            if rel > CONSISTENCY_TOLERANCE:
                problems.append(
                    ReportInconsistency(
                        kind="internal_report_inconsistency",
                        metric_name=parent.metric_name_verbatim,
                        parent_instance_id=parent_id,
                        parent_value=parent.value,
                        children_sum=total,
                        relative_error=rel,
                        source_locator=f"{parent.source_locator} ({dim})",
                    )
                )
    return problems


def extract_report(
    pdf_path: str,
    report_id: str | None = None,
    client: Any = None,
    llm_model: str = "claude-sonnet-4-6",
) -> ReportExtraction:
    """Extract all metric instances + formula hypotheses from one report
    PDF. Passing an Anthropic-compatible `client` enables the LLM
    structuring pass; without it the deterministic table parser runs."""
    report_id = report_id or Path(pdf_path).stem
    pages = _read_pages(pdf_path)
    as_of = _detect_as_of(pages)

    if client is not None:
        instances = _structure_tables_llm(report_id, as_of, pages, client, llm_model)
        if not instances:  # LLM produced nothing usable — fall back
            instances = _structure_tables_deterministic(report_id, as_of, pages)
    else:
        instances = _structure_tables_deterministic(report_id, as_of, pages)

    instances.extend(_extract_footnotes(report_id, pages))
    instances = _post_validate(instances, pages)

    deduped: dict[str, ReportMetricInstance] = {}
    for inst in instances:
        deduped.setdefault(inst.instance_id, inst)
    instances = list(deduped.values())

    hypotheses: list[FormulaHypothesis] = []
    for page in pages:
        hypotheses.extend(
            mine_formula_hypotheses(page.text, source_locator=f"page {page.number}")
        )
    # attach prose hints to matching grand-total instances
    hints = {h.metric_name.lower(): h.verbatim for h in hypotheses}
    for inst in instances:
        if not inst.dimension_context and inst.metric_name_verbatim.lower() in hints:
            inst.prose_formula_hint = hints[inst.metric_name_verbatim.lower()]

    return ReportExtraction(
        report_id=report_id,
        as_of_date=as_of,
        instances=instances,
        formula_hypotheses=hypotheses,
        inconsistencies=_validate_consistency(instances),
        full_text="\n".join(p.text for p in pages),
    )
