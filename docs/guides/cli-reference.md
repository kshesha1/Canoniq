---
id: cli-reference
title: CLI reference
sidebar_position: 1
description: Every canoniq command, its flags, and what it reads/writes.
---

# CLI reference

All commands are defined in [`canoniq/cli.py`](../../canoniq/cli.py).

## Command summary

| Command | Needs LLM? | Needs warehouse? | Reads | Writes |
|---|---|---|---|---|
| `mine` | only if `document_files` set | no (DDL alone works) | all configured evidence sources | nothing (console output) |
| `propose --table T` | yes | no | mined evidence for table `T` | `<output.dir>/T.proposal.json` |
| `emit --table T` | no | no | `T.proposal.json` | `T_metricflow.yml` and/or `T_osi.yml` |
| `validate --yaml PATH` | no | no | any MetricFlow YAML file | nothing (PASS/FAIL + exit code) |
| `run [--watch]` | yes | no | all configured evidence sources | `*_metricflow.yml` and/or `*_osi.yml` per table |
| `eval --table T` | no | **yes** | `T_metricflow.yml` + live warehouse | `eval_results.json` |
| `bootstrap --catalog D --reports D --out D` | no | **yes** (Iceberg) | report PDFs, `.twb`, docs, Iceberg snapshots | MetricFlow + OSI + OpenMetadata JSON + conflict report |
| `benchmark` | no | no (self-contained) | `benchmark/brownfield/` | everything `bootstrap` writes + `benchmark_scorecard.json` |

Every mining-first command takes `--config PATH` (default
`./canoniq.yaml`) — see [Configuration](./configuration.md).

## `canoniq mine`

```bash
canoniq mine --config canoniq.yaml
```

Runs ingest → mining → ranking only — **no LLM call** (unless
`document_files` triggers document extraction) and **nothing is written to
disk**. Prints a table of candidate metrics (expression, OntoRank trust
score, execution count, source tags) plus dimension/join counts, per
table. The right first command on a new config: it shows exactly what
evidence was found before spending an LLM call on it.

## `canoniq propose --table ORDERS`

```bash
canoniq propose --config canoniq.yaml --table ORDERS
```

Runs mining + ranking, then calls Claude (via
[Instructor](https://github.com/jxnl/instructor)) to propose a complete
semantic model for **one** table. Writes
`<output.dir>/ORDERS.proposal.json`. `--table` is optional if exactly one
table has mined evidence. Requires `ANTHROPIC_API_KEY`. The proposer's own
output is re-validated against the real schema — any invented column is
dropped, never silently emitted.

## `canoniq emit --table ORDERS --format all`

```bash
canoniq emit --config canoniq.yaml --table ORDERS --format all
```

Converts the proposal JSON written by `propose` into YAML — pure format
conversion, no mining, no LLM call. `--format` is `metricflow`, `osi`, or
`all` (default). `--table` auto-detects if exactly one proposal file
exists in `output.dir`.

## `canoniq validate --yaml path/to/model.yml`

```bash
canoniq validate --yaml out/orders_metricflow.yml
```

Standalone — doesn't read `canoniq.yaml`. Uses the real `mf
validate-configs` CLI if on `PATH`, otherwise falls back to a jsonschema
structural check. Prints `PASSED`/`FAILED` with specific errors, exits
non-zero on failure — useful as a CI gate on any generated or hand-edited
YAML.

## `canoniq run [--watch]`

```bash
canoniq run --config canoniq.yaml
canoniq run --config canoniq.yaml --watch
```

The full mining-first pipeline in one command for **every** table with
mined evidence: mine → rank → propose → validate (with automatic
repair-and-retry) → emit. Requires `ANTHROPIC_API_KEY`.

`--watch`: polls the query log (and dbt manifest, if
`sources.dbt_manifest` is set) every `poll_interval_seconds`, re-running
the whole pipeline whenever a new query shape or manifest metric appears.
Runs until Ctrl-C. DDL/document/Excel files are **not** watched — they're
re-read fresh on every full run, so edits to them are picked up on the
next signal-triggered run, just not instantly.

## `canoniq eval --table ORDERS --output eval_results.json`

```bash
canoniq eval --config canoniq.yaml --table ORDERS
```

Scores an already-emitted `ORDERS_metricflow.yml` against 10 built-in gold
questions, run directly against the **live warehouse** (the one command
that needs `warehouse.path` even in DDL-only setups). No LLM call. Prints
a pass/fail table and writes full results to `eval_results.json`.

## `canoniq bootstrap`

```bash
canoniq bootstrap \
  --catalog path/to/iceberg_warehouse \
  --reports path/to/board_reports/ \
  --tableau path/to/twb_workbooks/ \
  --docs path/to/policies_and_brds/ \
  --out out/ \
  --tolerance 0.005
```

| Flag | Required | Meaning |
|---|---|---|
| `--catalog` | yes | Iceberg warehouse directory (must contain `catalog.db`, a PyIceberg SQLite catalog, plus table data) |
| `--reports` | yes | Directory of board-report PDFs — every `*.pdf` is extracted |
| `--tableau` | no | Directory of `.twb` workbooks |
| `--docs` | no | Directory of policy documents / BRDs (`.md`, `.txt`, or `.pdf`) |
| `--out` | yes | Output directory |
| `--tolerance` | no (default `0.005`) | Fingerprint tolerance: `\|computed - reported\| / \|reported\|` |

The report-first pipeline described in [Pipeline
overview](../architecture/pipeline-overview.md). Does not need
`ANTHROPIC_API_KEY` by default (report structuring is deterministic).
Writes MetricFlow YAML, OSI YAML, OpenMetadata JSON (`openmetadata/` under
`--out`), and `conflict_report.md`/`.json` — see [The brownfield
benchmark](./benchmark.md) for what these look like and [Emitters
overview](../emitters/overview.md) for the emitted shapes.

## `canoniq benchmark [--regenerate] [--out DIR]`

```bash
canoniq benchmark
canoniq benchmark --regenerate       # force-rebuild the synthetic warehouse + PDFs first
canoniq benchmark --out /tmp/canoniq-bench
```

Generates the synthetic brownfield benchmark (if `benchmark/brownfield/
gold_labels.yaml` or its warehouse is missing, or `--regenerate` is
passed), runs `bootstrap` against it, and prints the scorecard (also
written to `<out>/benchmark_scorecard.json`). **Exits non-zero if any
acceptance criterion fails** — usable as a CI regression gate for the
whole report-first pipeline. See [The brownfield
benchmark](./benchmark.md) for what the scorecard measures.
