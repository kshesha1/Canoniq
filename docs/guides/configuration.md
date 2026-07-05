---
id: configuration
title: Configuration (canoniq.yaml)
sidebar_position: 3
description: Every canoniq.yaml option, for the mining-first pipeline.
---

# Configuration (`canoniq.yaml`)

This applies to the **mining-first** commands (`mine`, `propose`, `emit`,
`run`, `eval`, `validate`). The report-first `bootstrap`/`benchmark`
commands take CLI flags directly instead — see [CLI
reference](./cli-reference.md).

Loaded and validated by
[`canoniq/config.py::load_config`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/config.py) into a
`Config` dataclass. A full annotated example lives at
[`examples/canoniq.yaml.example`](https://github.com/kshesha1/Canoniq/blob/main/examples/canoniq.yaml.example).

```yaml
project_name: my_semantic_model

warehouse:
  type: duckdb                     # duckdb | snowflake | bigquery | trino
  path: ./warehouse.db             # DuckDB only; omit for DDL-only mode

# DDL files — used when no live warehouse connection, or to supplement one.
ddl_files:
  - ./schema/store_sales.sql
  - ./schema/customer.sql

# Business documents — BRDs, PDFs, Word docs, plain text.
document_files:
  - ./docs/revenue_definitions_brd.pdf
  - ./docs/kpi_glossary.docx

# Excel reports — named ranges and formula cells are mined for candidates.
excel_files:
  - ./reports/monthly_kpi_report.xlsx

# SQL query log — OPTIONAL. Adds usage-frequency signal, not required.
query_log:
  type: file                       # file | snowflake_history | trino_history
  path: ./queries.sql

require_query_log: false           # set true to make query_log mandatory

sources:
  dbt_manifest: ./target/manifest.json   # optional

output:
  formats: [metricflow, osi]       # which specs to emit
  dir: ./canoniq_output/

ontorank:
  weights:                          # must sum to 1.0
    source_authority: 0.30
    usage_frequency: 0.25
    cross_source_agreement: 0.20
    recency: 0.15
    certification_status: 0.10
  thresholds:
    auto_merge: 0.85               # above this -> write without human review
    review: 0.50                   # between review and auto_merge -> queue for review
    drop: 0.50                     # below this -> discard silently

llm:
  model: claude-sonnet-4-6
  max_retries: 3                   # validation loop max attempts

continuous:
  enabled: false                   # set true to run event-driven watcher
  poll_interval_seconds: 300
```

## Field reference

| Key | Required | Default | Notes |
|---|---|---|---|
| `project_name` | yes | — | |
| `warehouse.type` | yes | — | `duckdb` \| `snowflake` \| `bigquery` \| `trino` (only `duckdb` is actually wired up in v0) |
| `warehouse.path` | no | — | Required at runtime if you want a live warehouse connection; omit for DDL-only mode |
| `ddl_files` | no | `[]` | Parsed even alongside a live warehouse — also feeds per-table measure/dimension inference |
| `document_files` | no | `[]` | Triggers an LLM call (Claude as information extractor only) |
| `excel_files` | no | `[]` | No LLM — named ranges + formula cells |
| `query_log.path` | no | — | Required only if `require_query_log: true` |
| `require_query_log` | no | `false` | |
| `sources.dbt_manifest` | no | — | Feeds `is_certified` detection in the evidence bundle |
| `output.formats` | yes | — | Non-empty list, e.g. `[metricflow, osi]` |
| `output.dir` | yes | — | |
| `ontorank.weights.*` | no | see example | Must sum to 1.0 (validated, raises `ConfigError` otherwise) |
| `ontorank.thresholds.*` | no | `auto_merge=0.85, review=0.50, drop=0.50` | Must satisfy `0 <= drop <= review <= auto_merge <= 1` |
| `llm.model` | no | `claude-sonnet-4-6` | |
| `llm.max_retries` | no | `3` | Validation-loop repair attempts |
| `continuous.enabled` | no | `false` | Only affects `canoniq run --watch` |
| `continuous.poll_interval_seconds` | no | `300` | |

## DDL-only mode (no warehouse, no query log)

The minimum viable config — no live database connection or query history
needed at all:

```yaml
project_name: ddl_only_demo
warehouse:
  type: duckdb        # warehouse.path intentionally omitted
ddl_files:
  - ./schema/orders.sql
output:
  formats: [metricflow, osi]
  dir: ./canoniq_output/
```

```bash
canoniq mine --config canoniq.yaml
```

This parses the DDL, infers each column's role (measure / dimension /
identifier / flag) from naming convention and constraints
(`canoniq/ingest/ddl_extractor.py`), and mines candidate metrics from it.
Layer in `document_files`/`excel_files` for more evidence in the same
config.

## What needs an API key

| Command | Needs `ANTHROPIC_API_KEY`? |
|---|---|
| `validate` | never |
| `mine` | only if `document_files` is set |
| `propose`, `run`, `emit`/`eval` after a `run` | yes |
| `bootstrap`, `benchmark` | no, by default (deterministic report structuring) |
