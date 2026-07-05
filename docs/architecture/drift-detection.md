---
id: drift-detection
title: Semantic drift detection (Module E)
sidebar_position: 6
description: Detecting renames, silent redefinitions, and undocumented changes across report editions.
---

# Semantic drift detection

Code: [`canoniq/drift/report_diff.py::diff_reports`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/drift/report_diff.py).

Given two report editions' extracted instances and their resolved
mappings, `diff_reports` detects four kinds of `DriftFinding`:

| Kind | Detection |
|---|---|
| `renamed` | A metric name present in the old edition disappears, but a *new* metric resolves to the **same physical expression** |
| `redefined` | Same printed name in both editions, but either (a) the resolved expressions differ, or (b) the old expression, evaluated at the new snapshot, diverges from the newly reported figure beyond tolerance |
| `appeared` | A metric name only present in the new edition (net of renames) |
| `disappeared` | A metric name only present in the old edition (net of renames) |

## Why redefinition has two detection paths

A silent redefinition can happen two ways in practice:

1. The resolved expression itself changes (e.g. a new `WHERE` filter is
   added) — detected directly by comparing `_expression(old_mapping)` vs
   `_expression(new_mapping)`.
2. The expression the solver picked **looks** unchanged, but the business
   logic actually changed in a way the solver didn't need to model
   explicitly (rare, but possible if both old and new definitions happen
   to reproduce their own quarter's constraints). Path (b) is why
   `diff_reports` also **recomputes the old expression against the new
   snapshot** and checks it against the newly reported figure — if they
   diverge beyond tolerance, that's redefinition too, even with no visible
   expression change.

This second path is why `diff_reports` takes an optional
`SnapshotExecutor` — without one, only path (a) is checked.

## The headline field: `undocumented`

For every finding, `diff_reports` searches every ingested document (policy
docs, BRDs) for a mention of either name involved. If **no document
mentions the change**, `undocumented=True` — this is the field the
[conflict report](../guides/benchmark.md) sorts on and surfaces first.
A mention doesn't prove the change was properly documented (it's a
name-match proxy, not a semantic check of whether the document actually
*explains* the change) — but an *absent* mention is a hard, useful
signal: **nothing** documents the change at all.

## Benchmark regression tests

The bundled benchmark plants exactly one of each interesting case:

- **Rename:** "Counterparty Credit Exposure" (Q4 edition) becomes
  "Adjusted Counterparty Exposure" (Q1 edition), same underlying
  `SUM(EXP_AMT_USD)` logic. Detected via method (renamed), tagged
  undocumented (no document mentions the rename).
- **Silent redefinition:** "Market Risk Sensitivities" keeps the same
  name in both editions, but the Q1 figures quietly exclude one risk
  factor (`IR_VEGA`) — with identical prose commentary in both editions.
  This is resolved via Tier 3's filter search (see [Numeric
  fingerprinting](./numeric-fingerprinting.md)) and flagged `redefined`,
  undocumented.

See `tests/test_drift.py` for the exact assertions.
