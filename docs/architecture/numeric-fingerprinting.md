---
id: numeric-fingerprinting
title: Numeric fingerprinting engine (Module D)
sidebar_position: 5
description: The three-tier candidate search plus constraint-satisfaction solver — the centerpiece of report-first bootstrapping.
---

# Numeric fingerprinting engine

This is the centerpiece of report-first bootstrapping. Code:
[`canoniq/fingerprint/`](../../canoniq/fingerprint/).

The idea: **a candidate SQL expression is only trusted once it empirically
reproduces the published figures** — the grand total, every dimensional
breakdown row, and the prior-quarter footnote. Name similarity and single
lucky totals are cheap to fake; reproducing seven independent numbers from
one candidate expression is not.

## D0 — the shared executor

[`canoniq/fingerprint/executor.py::SnapshotExecutor`](../../canoniq/fingerprint/executor.py)

- Every query runs through **DuckDB reading a PyIceberg snapshot scan**
  registered as an Arrow view.
- Every query targets the snapshot whose `canoniq.as_of` snapshot property
  (or, if absent, commit timestamp) is within **3 days** of the report
  figure's `as_of_date`. If none exists, `SnapshotNotFoundError` is raised
  — Canoniq never silently compares against the wrong point in time.
- Historical column names are normalized to current names via Iceberg
  **field IDs**, so a query written against `EXP_AMT_USD` correctly reads
  a pre-rename snapshot that only had `EXP_AMT` (see
  [`canoniq/fingerprint/catalog.py::IcebergCatalogAdapter.arrow_for`](../../canoniq/fingerprint/catalog.py)).
- Comparisons use `Decimal` throughout — never float tolerance on money.
  `tolerance` defaults to 0.5%: `|computed - reported| / |reported| <= tol`.
- Results are cached on `(expr_canonical_key, snapshot_as_of, binding)` —
  the solver re-evaluates the same candidate against many constraints, so
  this matters for wall-clock time.

## The expression grammar

```
expr := AGG(col) | AGG(col) OP AGG(col) | AGG(col) WHERE simple_predicate
AGG  := SUM | COUNT | AVG
OP   := + | - | /
join := at most one, only along join-graph edges
```

Enforced by `CandidateExpr`'s Pydantic validator (see [Core data
model](./data-model.md)). Nothing deeper is in scope for v1 — see
[non-goals](../reference/non-goals.md).

## Three tiers, run in strict order

Code: [`canoniq/fingerprint/tiers.py`](../../canoniq/fingerprint/tiers.py).
Each tier only runs if the previous one produced zero survivors, **except
Tier 1, which always runs** (it's cheap).

### Tier 1 — name-based verification

Shortlist candidates whose Tableau caption or column name resembles the
metric name (`canoniq/fingerprint/naming.py::similarity`, an abbreviation-
expanding token-overlap + sequence-ratio scorer — `RWA` expands to "risk
weighted assets", `EXP` to "exposure", etc.). Evaluate each against the
grand-total constraint only; survivors proceed to full constraint scoring.

**Version-suffix noise (`_V2`, `_DEPR`, `_LEGACY`) is deliberately
stripped during name expansion**, so a deprecated twin column scores just
as name-plausible as its successor and enters this shortlist — the
constraint solver, not the name, is what decides between them. This is
exactly the [deprecated-twin trap](../guides/benchmark.md#trap-1-the-deprecated-twin)
regression test.

### Tier 2 — value-space search (name-independent)

Only runs if Tier 1 produced zero survivors. Enumerates `SUM(col)` /
`COUNT(*)` over **every numeric column in the catalog** (bounded by
`max_columns`, default 500) and checks each against the grand total,
regardless of name similarity. This is the channel that finds a mapping
like `AGG_EXP_ADJ_V3` with zero lexical overlap with the metric name.

### Tier 3 — bounded composition search

Only runs if Tier 2 also produced zero survivors, and only when structure
hints exist (a `FormulaHypothesis`, Tableau evidence, or a table already
holding a resolved metric / a near-miss). Three hypothesis generators, in
priority order:

1. **Prose-formula seeded** — for each `FormulaHypothesis` (e.g. `"A - B"`),
   resolve each term description independently via name similarity against
   column names/descriptions (`canoniq/fingerprint/naming.py::resolve_term`),
   take the top 3 candidates per term, enumerate combinations.
2. **Tableau seeded** — any Tableau formula whose caption is even loosely
   similar to the metric name (a lower bar than Tier 1).
3. **Locality-bounded blind search (last resort)** — two-term `A - B`,
   `A + B`, `A / B` where both columns come from tables already holding a
   resolved metric or one FK-hop away, **plus** single-aggregate
   expressions filtered by low-cardinality string columns (`WHERE col =
   'X'` / `WHERE col <> 'X'`, capped at `max_filter_cardinality` distinct
   values). Hard cap: `max_tier3_hypotheses` (default 2000) per metric,
   truncated by name-similarity score if exceeded.

This filter search is what resolves a **silently redefined** metric — see
the [Market Risk Sensitivities trap](../guides/benchmark.md#trap-4-the-silent-redefinition)
in the benchmark walkthrough.

## D4 — the constraint-satisfaction solver

Code: [`canoniq/fingerprint/solver.py`](../../canoniq/fingerprint/solver.py).

This is what separates real mappings from coincidences. For every
candidate that survives its tier's grand-total check:

1. **Assemble the full constraint set** for the metric: the grand total +
   every dimensional breakdown row (linked via `parent_total_id`) + any
   prior-quarter footnote figures (evaluated against the **earlier**
   snapshot).
2. **Resolve each dimension.** `resolve_dimension` maps report labels
   ("Meridian NY", "Internal Fraud (INT_FRD)") to a physical column —
   either directly on the measure's table, or one join-hop away — whose
   distinct values account for every label. A confirmed mapping therefore
   resolves the measure **and** its join simultaneously.
3. **Score:** `constraints_satisfied / constraints_total`, with a
   pass/fail + relative error recorded per constraint.
4. **Assign a confidence band:**

   | Band | Criterion |
   |---|---|
   | `CONFIRMED` | ≥4 constraints, ≥90% satisfied |
   | `PROBABLE` | ≥2 constraints, ≥75% satisfied |
   | `WEAK` | grand total is the only available constraint |
   | `REJECTED` | fails the grand total, or fails the CONFIRMED/PROBABLE/WEAK thresholds |
   | `UNMAPPABLE` | no candidate reaches WEAK after all three tiers |

5. **Feed back into OntoRank** as the `numeric_fingerprint` evidence type
   — see [OntoRank & trust tiers](./ontorank-trust.md).

`UNMAPPABLE` is a **first-class success output**: it means Canoniq tried
every tier and is telling you honestly that it couldn't prove a mapping,
rather than forcing a low-confidence guess. The
[conflict report](../guides/benchmark.md) turns every `UNMAPPABLE` metric
into a steward work queue entry with the tiers run and the best near-miss.

## Regression tests: the traps

The benchmark plants two traps specifically to stress-test the solver
(not just the tiers):

- **Deprecated twin** (`RWA_AMT_V2_DEPR`) — passes the grand total (built
  to land 0.3% off, inside tolerance) but fails every legal-entity and
  asset-class breakdown by 5–7%. Must be `REJECTED`.
- **Decoy column** (`HDG_NTNL_AMT`) — coincidentally lands within 0.2% of
  the Q1 Operational Losses grand total, but fails every dimensional
  breakdown. Must be `REJECTED`.

Both are exercised in `tests/test_fingerprint.py` and are the acceptance
gate for this module — see [The brownfield benchmark](../guides/benchmark.md).
