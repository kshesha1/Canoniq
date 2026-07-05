---
id: data-model
title: Core data model
sidebar_position: 2
description: ReportMetricInstance, CandidateExpr, ResolvedMapping and the rest of canoniq/models.py.
---

# Core data model

All of these live in [`canoniq/models.py`](../../canoniq/models.py) as
Pydantic models. This page explains *why* each one exists and how they
connect — the docstrings in the code cover field-by-field detail.

## `ReportMetricInstance` — the anchor of everything

```python
class ReportMetricInstance(BaseModel):
    instance_id: str                   # deterministic hash
    report_id: str                     # e.g. "sor_2026q1"
    metric_name_verbatim: str          # exactly as printed
    value: Decimal
    unit: str                          # "USD_mm", "USD_bn", "count", "pct"
    scale_factor: Decimal              # e.g. 1e6 for figures printed in $mm
    as_of_date: date                   # report period end
    dimension_context: dict[str, str]  # {} = grand total, else a breakdown row
    source_locator: str                # "page 7, table 3, row 2"
    prose_formula_hint: str | None     # extracted commentary logic, verbatim
    parent_total_id: str | None        # links breakdown rows to their total
```

Every figure printed in a board report — a grand total, a legal-entity
breakdown row, a prior-quarter footnote — becomes one instance. The
`parent_total_id` field is what lets the constraint solver treat a set of
breakdown rows as **joint constraints on the same candidate expression**,
not independent facts: a candidate that reproduces the grand total but not
its own breakdown is rejected (see [Numeric
fingerprinting](./numeric-fingerprinting.md)).

`raw_value` (a computed property) applies `scale_factor` to `value`, so
comparisons downstream are always in base units (raw USD, raw counts)
regardless of how the number was printed ("$67,892.1mm" vs "$67.9bn").

## `FormulaHypothesis` — mined commentary logic

```python
class FormulaHypothesis(BaseModel):
    metric_name: str
    structure: Literal["A - B", "A + B", "A / B", "SUM(A)"]
    term_descriptions: list[str]   # ["gross exposure", "collateral held"]
    source_locator: str
    verbatim: str                  # the sentence it came from
```

Mined by [`canoniq/extract/prose.py::mine_formula_hypotheses`](../../canoniq/extract/prose.py)
from sentences like *"Total Credit Risk Exposure is calculated as gross
exposure less collateral held."* These seed Tier 3 of the fingerprinting
engine — each term description is resolved to a physical column
**independently**, which is a much easier sub-problem than resolving the
whole formula at once.

## `TableauEvidence` — near-executable ground truth

```python
class TableauEvidence(BaseModel):
    source_file: str
    caption: str
    physical_expr_sql: str          # sqlglot-normalized SQL
    referenced_columns: list[str]
    worksheet_names: list[str]
    role_hints: dict[str, str]      # column -> "dimension" | "measure"
```

Produced by [`canoniq/extract/tableau.py`](../../canoniq/extract/tableau.py).
Tableau calculated fields are almost literally the SQL you're looking for
— they get a trust prior of 0.85, just below steward-confirmed.

## The fingerprint expression grammar

```python
class Term(BaseModel):        # AGG(table.column)
    agg: Literal["SUM", "COUNT", "AVG"]
    table: str
    column: str

class SimplePredicate(BaseModel):   # column op 'value'
    column: str
    op: Literal["=", "<>"]
    value: str

class CandidateExpr(BaseModel):
    lhs: Term
    op: Literal["+", "-", "/"] | None = None
    rhs: Term | None = None
    predicate: SimplePredicate | None = None   # single-term expressions only
```

This is the **entire** expression grammar Canoniq v1 will ever propose:

```
expr := AGG(col) | AGG(col) OP AGG(col) | AGG(col) WHERE simple_predicate
```

A Pydantic validator (`CandidateExpr._grammar`) enforces this at
construction time — `op`/`rhs` must come together, a predicate can't
combine with a two-term expression, and `*` is only valid with `COUNT`.
This is deliberately narrow (see [Non-goals](../reference/non-goals.md));
widening it is a `# V2:` comment in the code, not a silent feature creep.

`canonical_key()` gives a normalized string identity used for dedup and
for comparing against `gold_labels.yaml` in tests.

## `DimensionBinding` — how a report label becomes a `GROUP BY`

```python
class DimensionBinding(BaseModel):
    dimension_key: str              # "legal_entity"
    group_column: str               # "LE_NM"
    group_table: str                # "LE_REF"
    join_from: str | None           # "RWA_CALC_FCT.LE_CD"
    join_to: str | None             # "LE_REF.LE_CD"
    label_to_value: dict[str, str]  # "Meridian NY" -> "Meridian NY"
```

Resolved by [`canoniq/fingerprint/solver.py::resolve_dimension`](../../canoniq/fingerprint/solver.py) —
maps a report's dimension labels ("Meridian NY", "Internal Fraud
(INT_FRD)") to the column and (at most one) join whose values, joined or
not, account for every label the report printed. A confirmed mapping
therefore resolves the measure **and** its dimension join simultaneously.

## `ConstraintResult`, `ConfidenceBand`, `CandidateEvaluation`, `ResolvedMapping`

These four types carry the actual verdict for one report metric, from raw
constraint-by-constraint results up to the final answer:

```
ConstraintResult   — one constraint's pass/fail (grand total | breakdown | prior period)
CandidateEvaluation — one candidate expression's full scorecard (list of ConstraintResults, satisfied/total)
ConfidenceBand      — CONFIRMED | PROBABLE | WEAK | REJECTED | UNMAPPABLE
ResolvedMapping     — the metric's final verdict: best CandidateEvaluation (or None), rejected candidates, best near-miss, tiers run
```

See [Numeric fingerprinting](./numeric-fingerprinting.md) for how the band
is computed and why `UNMAPPABLE` is a deliberate, first-class outcome
rather than an absence of data.

## `DriftFinding`

```python
class DriftFinding(BaseModel):
    kind: Literal["renamed", "redefined", "appeared", "disappeared"]
    old_name: str | None
    new_name: str | None
    old_expression: str | None
    new_expression: str | None
    old_value: Decimal | None
    new_value: Decimal | None
    detail: str
    undocumented: bool              # headline field
    documentation_hits: list[str]
```

See [Drift detection](./drift-detection.md).
