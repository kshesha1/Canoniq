---
id: intro
title: What is Canoniq?
sidebar_position: 1
slug: /
description: Canoniq is a cold-start semantic bootstrapping engine for brownfield enterprises.
---

# What is Canoniq?

Canoniq is an open-source **cold-start semantic bootstrapping engine for
brownfield enterprises** — organizations with no dbt, no semantic layer, no
documented business-to-physical mappings, only legacy artifacts: cryptic
Oracle-heritage schemas (now often sitting on Iceberg), board-level PDF
reports, Tableau workbooks, and policy documents that quietly contradict
each other.

It authors a **semantic layer specification** — [dbt
MetricFlow](https://docs.getdbt.com/docs/build/build-metrics-intro) YAML,
[OSI v1.0](https://github.com/open-semantic-interchange/OSI) YAML, and
[OpenMetadata](https://open-metadata.org/) JSON — from evidence mined out
of whatever your organization's metric definitions actually live in. It
does **not** run queries, serve a BI tool, or answer natural-language
questions. It writes the spec; something else runs it.

## The core idea: start from the report, not the schema

Most semantic-layer tooling starts at the schema and works outward,
guessing at what a column like `RWA_AMT_V3` might mean. Canoniq inverts
this for the brownfield case:

> **A quarterly board report is treated as a complete, business-approved
> specification of the semantic layer.** Every figure printed in it is a
> metric instance to be mapped backward to physical columns — and every
> figure is also a **test oracle**, used to empirically validate candidate
> mappings by recomputing the published numbers against Iceberg
> time-travel snapshots.

This has three consequences that shape everything else in Canoniq:

1. [`ReportMetricInstance`](./architecture/data-model.md) is the central
   data model. OntoRank, numeric fingerprinting, drift detection, and the
   emitters all consume or produce mappings anchored to report metric
   instances.
2. **"Unmappable — escalate to steward" is a first-class success output,
   not an error path.** Canoniq never forces a low-confidence mapping just
   to fill in a blank.
3. **Every emitted mapping carries full provenance** — which report figures
   it reproduced, at what tolerance, corroborated by which documents.

## What Canoniq actually does

```
board reports (PDF) ──▶ metric instance extraction (pdfplumber + prose mining)
Tableau .twb ─────────▶ calculated-field evidence (lxml + sqlglot)
policy docs / BRDs ───▶ definitions, glossary terms, contradictions
                                                       │
                                                       ▼
Iceberg snapshots ◀── numeric fingerprinting: 3 bounded tiers
 (PyIceberg + DuckDB)         │
                              ▼
                constraint-satisfaction solver:
                grand total + every breakdown + prior-quarter footnotes
                                                       │
                                                       ▼
       CONFIRMED / PROBABLE / WEAK / REJECTED / UNMAPPABLE
                                                       │
                                                       ▼
             drift diff across editions (renames, silent redefinitions)
                                                       │
                                                       ▼
        MetricFlow YAML + OSI YAML + OpenMetadata JSON + conflict report
```

Read the [pipeline overview](./architecture/pipeline-overview.md) for the
full walkthrough, or jump straight to [Getting
started](./getting-started.md) to run it.

## What Canoniq v1 does not do

Being explicit about scope, because "does it also do X" questions are
cheaper to answer here than in an issue tracker:

- **No chart/image OCR or vision-model extraction.** Report extraction is
  text and tables only (via `pdfplumber`). A figure that only exists in a
  chart image is invisible to Canoniq.
- **No steward interview mode.** That's a v2 idea — v1 surfaces an
  "unmappable" work queue for a human to act on, but doesn't conduct the
  interview itself.
- **No live OpenMetadata API integration.** The OpenMetadata emitter
  writes JSON files only. Canoniq is a **population engine for empty
  OpenMetadata catalogs**, not a catalog itself.
- **No email ingestion.**
- **No transformation search beyond single columns and two-term
  arithmetic.** The fingerprinting grammar is deliberately narrow — see
  [Numeric fingerprinting](./architecture/numeric-fingerprinting.md).
- **No multi-hop join synthesis.** Dimension resolution allows at most one
  join, along a declared or inferred foreign-key edge.

Canoniq v1 does something narrower and more honest than "understand your
whole data estate": it reads what the business already published,
proposes physical mappings, **proves them by recomputing the published
numbers from Iceberg snapshots**, and tells you plainly what it could not
prove.
