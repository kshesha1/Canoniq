---
id: glossary
title: Glossary
sidebar_position: 2
description: Every term Canoniq's own documentation and code use, defined once.
---

# Glossary

| Term | Meaning |
|---|---|
| **ReportMetricInstance** | One figure printed in a board report: a value, its unit/scale, the as-of date, its dimension breakdown (or `{}` for a grand total), and where it came from. See [Core data model](../architecture/data-model.md). |
| **Grand total** | A `ReportMetricInstance` with `dimension_context == {}` — the top-line figure a metric's breakdown rows sum to. |
| **Breakdown row** | A `ReportMetricInstance` with a non-empty `dimension_context`, linked to its grand total via `parent_total_id`. |
| **FormulaHypothesis** | A metric-logic hypothesis mined from report commentary prose (e.g. "A − B"), used to seed Tier 3. |
| **TableauEvidence** | A Tableau calculated field's formula, normalized to SQL via sqlglot, with worksheet shelf-usage role hints. |
| **CandidateExpr** | One candidate SQL expression in the fingerprint grammar: `AGG(col)`, `AGG(col) OP AGG(col)`, or `AGG(col) WHERE predicate`. |
| **Snapshot** | An Iceberg table snapshot, tagged with a `canoniq.as_of` property (or falling back to its commit timestamp) so fingerprinting can target the exact point in time a report figure describes. |
| **Tier 1 / 2 / 3** | The three bounded candidate-generation passes in the fingerprinting engine — name-based, value-space, and bounded composition search respectively. See [Numeric fingerprinting](../architecture/numeric-fingerprinting.md). |
| **Constraint** | One thing a candidate expression must reproduce to be trusted: the grand total, one breakdown row, or a prior-quarter footnote figure. |
| **ConfidenceBand** | The verdict for a metric: `CONFIRMED` \| `PROBABLE` \| `WEAK` \| `REJECTED` \| `UNMAPPABLE`. |
| **UNMAPPABLE** | A deliberate, first-class outcome: no candidate reached even `WEAK` confidence after all three tiers. Not an error — a steward work-queue entry. |
| **DimensionBinding** | How a report's dimension labels ("Meridian NY") map to a physical column, and — if needed — the single join used to reach it. |
| **DriftFinding** | A detected change between two report editions: `renamed`, `redefined`, `appeared`, or `disappeared`. |
| **undocumented** | The headline field on a `DriftFinding`: `True` if no ingested document mentions either name involved in the change. |
| **OntoRank** | The 5-signal trust scorer applied to every metric candidate in both pipelines. See [OntoRank & trust tiers](../architecture/ontorank-trust.md). |
| **SourceType** | A fixed tag (`dbt_metric`, `numeric_fingerprint`, `tableau_calc`, …) with a fixed OntoRank authority prior. |
| **numeric_fingerprint** | The highest non-steward OntoRank evidence type (0.98): empirical reproduction of a published figure against an Iceberg snapshot. |
| **SemanticModelProposal** | The shared shape both pipelines converge on before emission — entities, dimensions, metrics, joins, one per source table. |
| **Report-first pipeline** | Canoniq's v1 pipeline: starts from board reports, maps figures backward to physical columns, proves mappings against Iceberg snapshots. `canoniq bootstrap` / `canoniq benchmark`. |
| **Mining-first pipeline** | Canoniq's original v0 pipeline: starts from the warehouse schema/query log/DDL/documents, proposes metrics via an LLM. `canoniq mine` / `propose` / `emit` / `run`. |
| **Conflict report** | The four-section markdown + JSON artifact summarizing confirmed mappings, contradictions, the unmappable work queue, and the drift register. |
| **Brownfield benchmark** | The synthetic "Meridian Bancorp" risk mart used to test the whole report-first pipeline end to end. See [The brownfield benchmark](../guides/benchmark.md). |
