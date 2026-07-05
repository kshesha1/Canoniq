---
id: getting-started
title: Getting started
sidebar_position: 2
description: Install Canoniq and run both the mining-first and report-first pipelines.
---

# Getting started

## Install

```bash
git clone <this-repo>
cd my-semantic-layer
pip install -e ".[dev]"
```

Canoniq has two entry points that share the same evidence/trust/emitter
machinery but start from different ends:

| Pipeline | Starts from | Command | Needs an LLM? |
|---|---|---|---|
| **Mining-first** (v0) | warehouse schema, query log, DDL, documents | `canoniq mine` / `propose` / `emit` / `run` | yes, for `propose`/`run` |
| **Report-first** (v1) | published board reports | `canoniq bootstrap` / `canoniq benchmark` | no (deterministic by default) |

If you're evaluating Canoniq for the first time, start with `canoniq
benchmark` — it's fully self-contained and needs no API key.

## Try the report-first pipeline: `canoniq benchmark`

This builds a synthetic "regulated risk data mart" (fictional Meridian
Bancorp) — Iceberg tables with real time-travel snapshots, two PDF board
reports, a Tableau workbook, contradictory policy documents — and runs the
full bootstrap pipeline against it, then scores the result against ground
truth.

```bash
canoniq benchmark
```

You should see a scorecard like:

```
| Measure                                           |    Value |
| Extraction recall (sor_2025q4)                    |   100.0% |
| Extraction recall (sor_2026q1)                    |   100.0% |
| Mapping recall (correct expr, CONFIRMED/PROBABLE) |   100.0% |
| Mapping precision @ CONFIRMED (n=10)              |   100.0% |
| Unmappable correctly escalated                    |   100.0% |
| Trap: deprecated_twin                             | REJECTED |
| Trap: decoy_column                                | REJECTED |
| Drift findings                                    |      2/2 |
| Wall-clock                                        |     0.6s |
| Overall                                           |   PASSED |
```

Artifacts land in `benchmark/brownfield/output/`:

```
crd_exp_fct_metricflow.yml       rwa_calc_fct_metricflow.yml
crd_exp_fct_osi.yml              rwa_calc_fct_osi.yml
conflict_report.md               conflict_report.json
benchmark_scorecard.json
openmetadata/
  glossary.json  glossary_terms.json  tables.json  lineage.json
```

Open `conflict_report.md` first — it's designed to be the human-readable
entry point: confirmed mappings, contradictions, an unmappable work
queue, and a drift register.

See [The brownfield benchmark](./guides/benchmark.md) for what's actually
in this benchmark and why the traps matter.

## Run report-first bootstrap on your own data

```bash
canoniq bootstrap \
  --catalog path/to/iceberg_warehouse \   # dir containing catalog.db (SQLite) + table data
  --reports path/to/board_reports/ \      # PDFs
  --tableau path/to/twb_workbooks/ \      # optional
  --docs path/to/policies_and_brds/ \     # optional: .md, .txt, .pdf
  --out out/
```

Requirements:

- The Iceberg warehouse must use PyIceberg's **SQLite catalog** convention
  (a `catalog.db` file alongside the table data directory). See
  [`canoniq/fingerprint/catalog.py`](../canoniq/fingerprint/catalog.py).
- Every table snapshot that a report figure should be checked against must
  carry a `canoniq.as_of` snapshot property (or Canoniq falls back to the
  snapshot's commit timestamp) within **3 days** of the report's `as_of`
  date — otherwise fingerprinting raises `SnapshotNotFoundError` for that
  metric rather than silently comparing against the wrong point in time.
- Reports are PDFs with an `As of <Month D, YYYY>` line and tables
  pdfplumber can extract (see [Report extraction](./architecture/report-extraction.md)).

No `ANTHROPIC_API_KEY` is required for `bootstrap` by default — report
structuring is deterministic. An LLM structuring pass exists for messier
real-world report layouts (see `canoniq/extract/report.py`,
`_structure_tables_llm`), enabled by passing a client into
`extract_report(..., client=...)` if you're calling the extraction API
directly rather than through the CLI.

## Try the mining-first pipeline

This is the original v0 pipeline: mine evidence from a live warehouse,
query log, DDL, documents, or Excel, then propose + emit + validate one
table at a time.

```bash
# Mine the bundled TPC-DS-style demo warehouse + query log
canoniq mine --config examples/tpcds_duckdb/canoniq.yaml

# Propose a semantic model for one table (requires ANTHROPIC_API_KEY)
canoniq propose --config examples/tpcds_duckdb/canoniq.yaml --table store_sales

# Emit MetricFlow + OSI YAML from that proposal
canoniq emit --config examples/tpcds_duckdb/canoniq.yaml --table store_sales

# Validate an existing YAML file
canoniq validate --yaml examples/tpcds_duckdb/canoniq_output/store_sales_metricflow.yml

# Run the full pipeline for every mined table in one shot
canoniq run --config examples/tpcds_duckdb/canoniq.yaml
```

See the full [CLI reference](./guides/cli-reference.md) and
[Configuration](./guides/configuration.md) for `canoniq.yaml` options,
including DDL-only mode (no warehouse or query log needed at all).

## Run the tests

```bash
pip install -e ".[dev]"
pytest              # 225 tests, ~10s, no real LLM calls, no network
ruff check canoniq tests benchmark
mypy canoniq
```

The report-first pipeline's tests are fully deterministic end to end: the
benchmark is regenerated once per test session
(`tests/conftest.py::brownfield_root`) and every module's acceptance
tests run against it.
