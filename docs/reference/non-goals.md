---
id: non-goals
title: Non-goals & current limitations
sidebar_position: 1
description: What canoniq v1 deliberately does not do, and where the seams are.
---

# Non-goals & current limitations

## Report-first pipeline (v1 scope)

Explicitly out of scope for v1 — not built, not stubbed beyond a `# V2:`
comment in the code:

- **Chart/image OCR or vision-model extraction.** Text and tables only.
  A figure that only appears in a chart image is invisible.
- **Steward interview mode.** `UNMAPPABLE` produces a work-queue entry in
  the conflict report; conducting the actual interview is a v2 idea.
- **Live OpenMetadata API integration.** The emitter writes JSON files
  only.
- **Email ingestion.**
- **Transformation search beyond single columns and two-term
  arithmetic.** See the [fingerprint expression
  grammar](../architecture/numeric-fingerprinting.md#the-expression-grammar).
- **Multi-hop join synthesis.** Dimension resolution allows at most one
  join, along a declared or inferred FK edge.

## Mining-first pipeline (v0, still true)

- **Warehouse connectors:** only DuckDB is implemented. Snowflake/
  BigQuery/Trino are in the config schema but not wired up.
- **`filter:` on MetricFlow metrics:** `MetricProposal` has no structured
  filter field, so the emitter can't reconstruct a MetricFlow
  `filter: "{{ Dimension(...) }}..."` block from prose — this is omitted
  rather than guessed.
- **Ratio/derived metrics:** the mining-first pipeline only ever extracts
  single-aggregation candidates, so `type: derived` fallback in the
  MetricFlow emitter is best-effort and doesn't populate
  `type_params.metrics` cross-references.
- **Eval harness SQL fallback:** without the real `mf query` CLI, the
  harness translates a metric back to raw SQL against its own table — it
  does not join in dimensions living on a different table, so gold
  questions needing a cross-table `GROUP BY` fail with a clear error
  rather than a wrong answer.
- **Continuous watcher:** `canoniq run --watch` polls the query log (and
  dbt manifest) only — it does not watch DDL/document/Excel files for
  changes, and Looker/Tableau/Confluence connectors for the mining-first
  path are not implemented (Tableau **is** implemented for the
  report-first path — see [Report extraction](../architecture/report-extraction.md)).
- **`mf` validate/query:** both the validation loop and the eval harness
  prefer the real dbt MetricFlow CLI when on `PATH`, but neither
  scaffolds a full dbt project around a bare YAML file, so in practice
  both fall back to their own structural-check / direct-SQL
  implementations.
- **Cross-source merging is representation-based, not semantic:** a DDL
  column inferred as `SUM(revenue)` and a document mentioning "sum of
  revenue" only merge into one metric if they produce the *identical*
  resolved SQL expression. Different phrasing or aliasing that resolves
  to the same metric surfaces as a separate candidate, not an
  automatically-merged one.
- **Document/Excel grounding is a lightweight heuristic, not the LLM:**
  `document_extractor.py` uses Claude only to *extract* what's explicitly
  written; mapping that to a real SQL expression is deterministic
  token-overlap matching with keyword-based aggregation inference. It
  doesn't understand synonyms, business jargon, or multi-table joins.
- **DDL role inference is naming/type/constraint heuristics**, not
  learned — see `_infer_role` in
  [`canoniq/ingest/ddl_extractor.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/ingest/ddl_extractor.py).

## The closing line from the spec

> Canoniq v1 does not: parse charts/images, interview stewards, call live
> catalogs, ingest email, or synthesize transformations deeper than two
> terms / one join. It does something narrower and more honest: it reads
> what the business already published, proposes physical mappings,
> **proves them by recomputing the published numbers from Iceberg
> snapshots**, and tells you plainly what it could not prove.
