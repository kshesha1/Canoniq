---
id: ontorank-trust
title: OntoRank & trust tiers
sidebar_position: 3
description: The 5-signal trust scorer and the fixed source-authority table.
---

# OntoRank & trust tiers

OntoRank ([`canoniq/ranking/ontorank.py`](../../canoniq/ranking/ontorank.py))
is a 5-signal scorer applied to every metric candidate, in both pipelines:

| Signal | What it measures | Weight (default) |
|---|---|---|
| `source_authority` | The highest-trust source type backing this candidate | 0.30 |
| `usage_frequency` | Log-normalized query-log execution count | 0.25 |
| `cross_source_agreement` | How many distinct source types agree on this expression | 0.20 |
| `recency` | How recently the evidence was last seen | 0.15 |
| `certification_status` | Whether it's already a certified dbt metric | 0.10 |

Weights are configurable per-project in `canoniq.yaml`
(`ontorank.weights`, validated to sum to 1.0 — see
[Configuration](../guides/configuration.md)).

## The source-authority table

Every candidate carries one or more `SourceType` tags
([`canoniq/ingest/base.py::SourceType`](../../canoniq/ingest/base.py)),
each with a fixed authority prior
(`canoniq/ranking/ontorank.py::SOURCE_AUTHORITY`, kept in sync with the
enum by convention, not by code — if you add a `SourceType` you must add
its authority here too):

| Source | Authority | Where it comes from |
|---|---:|---|
| `dbt_metric` | 1.00 | Explicit, human-authored dbt metric definition |
| `numeric_fingerprint` | 0.98 | **Report-first only.** Empirical reproduction of a published figure against an Iceberg snapshot — see [Numeric fingerprinting](./numeric-fingerprinting.md) |
| `brd_approved` | 0.90 | A business document with sign-off/approval language |
| `dbt_model` / `data_dictionary` | 0.85 | dbt model column descriptions |
| `tableau_calc` | 0.85 | **Report-first only.** A parsed Tableau calculated-field formula |
| `looker_measure` | 0.80 | A Looker measure |
| `tableau_field` | 0.78 | A Tableau calculated field (mining-first path) |
| `ddl_constraint` | 0.75 | A DDL column backed by an explicit PK/FK/CHECK |
| `excel_named` | 0.70 | An Excel named range |
| `brd_draft` | 0.65 | A business document with no approval signal |
| `confluence_page` | 0.60 | A Confluence page |
| `query_log_complex` | 0.60 | A query mining ≥2 distinct aggregations |
| `pdf_report` | 0.55 | A PDF report (no approval signal detected) |
| `excel_formula` | 0.50 | A bare formula cell (`=SUM(...)`, etc.) |
| `ddl_naming_convention` | 0.45 | A DDL column inferred purely from naming/type |
| `query_log_simple` | 0.40 | A single-aggregation query |
| `ad_hoc` | 0.20 | Unclassified/one-off evidence |

## Why `numeric_fingerprint` is second only to `dbt_metric`

This is the key design decision in the report-first spec: **empirical
reproduction of a published figure outranks any document, including an
approved BRD.** A Tableau field or policy document can be stale, aspirational,
or simply wrong; a candidate expression that reproduces the grand total
*and every dimensional breakdown* the board actually approved is about as
close to ground truth as brownfield evidence gets.

This is also why the numeric fingerprint score feeds back into OntoRank as
a new evidence type, rather than living only inside the fingerprinting
module — see `ResolvedMapping` in
[the data model](./data-model.md#constraintresult-confidenceband-candidateevaluation-resolvedmapping).

## Cross-pipeline evidence merging

DDL-, document-, and (mining-first) Tableau-derived candidates all flow
through the same dedup/ranking pipeline as query-log-mined candidates —
`canoniq/mining/evidence_bundle.py::build_evidence_bundle` merges them by
canonical expression before OntoRank ever scores anything. A `SUM(revenue)`
mentioned in an approved BRD and mined from the query log land in the same
bucket and combine their trust, **only if they resolve to the identical
SQL expression** — different phrasing that resolves to the same
underlying metric is not automatically coalesced (see [current
limitations](../reference/non-goals.md)).
