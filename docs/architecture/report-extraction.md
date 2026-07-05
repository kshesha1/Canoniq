---
id: report-extraction
title: Report extraction (Module B)
sidebar_position: 4
description: How PDF board reports become ReportMetricInstance objects.
---

# Report extraction

Code: [`canoniq/extract/report.py`](../../canoniq/extract/report.py),
[`canoniq/extract/prose.py`](../../canoniq/extract/prose.py).

## Pipeline

1. **Table + text extraction** — `pdfplumber` pulls per-page text and
   table grids. No vision models, no OCR: this is explicitly text-and-
   tables only ([non-goals](../reference/non-goals.md)).
2. **Structuring** — by default, a deterministic parser
   (`_structure_tables_deterministic`) matches table titles against the
   pattern `Table N: <Metric> [by <Dimension>] (<Unit>)`, and turns each
   row into a `ReportMetricInstance`. A total row becomes the grand-total
   instance; every other row becomes a breakdown instance linked to it via
   `parent_total_id`.

   An **optional LLM structuring pass** (`_structure_tables_llm`) exists
   for messier real-world layouts that don't fit the strict title pattern
   — pass a client into `extract_report(..., client=...)`. Either way, the
   structuring step's only job is formatting what's already printed, never
   inventing it.
3. **Post-validation** (`_post_validate`) — every instance's printed value
   is checked against the literal page text (a few numeric formattings are
   tried: `1234`, `1,234`, `1,234.5`, `1,234.56`). Any instance whose value
   can't be found verbatim is dropped and logged. This is the guardrail
   against LLM hallucination in the structuring pass, and it's covered by
   a dedicated test
   (`tests/test_report_extract.py::test_post_validation_rejects_invented_values`).
4. **Prose formula mining** — a second pass over page text
   (`canoniq/extract/prose.py::mine_formula_hypotheses`) looks for
   commentary sentences like *"X is calculated as A less B"* and produces
   `FormulaHypothesis` objects. These seed Tier 3 of the fingerprinting
   engine.
5. **Consistency validation** (`_validate_consistency`) — deterministic,
   no LLM: for every parent/child group, checks `sum(children) ≈ parent`
   within tolerance (default 0.5%) and records violations as
   `ReportInconsistency("internal_report_inconsistency", ...)`. A clean
   report should produce zero of these; when it doesn't, that's a signal
   the report itself has an error, independent of anything Canoniq is
   trying to map.

## Footnote extraction

Prior-quarter comparative figures ("Total Credit RWA — prior quarter (as
of December 31, 2025): 67,892.1 USD millions") are extracted separately by
`_extract_footnotes` and attached to the metric group as an additional
constraint the fingerprinting solver checks against the **earlier**
snapshot. This is what lets Canoniq catch a
[silent redefinition](./drift-detection.md): if the old expression, run
against the old snapshot, doesn't reproduce the restated prior-quarter
figure either, that's real signal the metric's logic changed.

## Deterministic vs. LLM structuring — when to use which

| | Deterministic (default) | LLM-assisted |
|---|---|---|
| Requires an API key | No | Yes |
| Requires strict table titles | Yes (`Table N: <Metric> ...`) | No |
| Used by `canoniq benchmark` | Yes | No |
| Best for | Clean, template-generated reports | Real-world reports with inconsistent formatting |

Both paths produce the same `ReportMetricInstance` shape and both go
through the same post-validation step, so nothing downstream needs to know
which one ran.

## `ReportExtraction` — what you get back

```python
@dataclass
class ReportExtraction:
    report_id: str
    as_of_date: date
    instances: list[ReportMetricInstance]
    formula_hypotheses: list[FormulaHypothesis]
    inconsistencies: list[ReportInconsistency]
    full_text: str
```

Acceptance target (see `tests/test_report_extract.py`): **≥95% of
gold-labeled metric instances extracted with correct value, scale, as-of
date, and dimension context.** The bundled benchmark reports currently
score 100%.
