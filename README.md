# canoniq

An open-source **cold-start semantic bootstrapping engine for brownfield
enterprises**: organizations with no dbt, no semantic layer, no documented
business-to-physical mappings — only legacy artifacts (cryptic
Oracle-heritage schemas now on Iceberg, board-level PDF reports, Tableau
workbooks, contradictory policy documents).

The architectural inversion: **the pipeline starts from the consumption end
(board reports), not the schema end.** A quarterly board report is treated
as a complete, business-approved specification of the semantic layer. Every
figure in it is a metric instance to be mapped backward to physical columns
— and every figure is also a **test oracle**: candidate mappings are
validated empirically by recomputing the published numbers against Iceberg
time-travel snapshots.

Canoniq mines evidence from wherever metric definitions actually live —
published reports, Tableau workbooks, warehouse schemas, SQL query logs,
raw DDL, business documents, Excel reports — scores every candidate by
trust (OntoRank) and by **numeric fingerprinting** (empirical reproduction
outranks any document), and emits validated [dbt
MetricFlow](https://docs.getdbt.com/docs/build/build-metrics-intro) YAML,
[OSI v1.0](https://github.com/open-semantic-interchange/OSI) YAML, and
[OpenMetadata](https://open-metadata.org/) JSON. Canoniq is a **population
engine for empty OpenMetadata catalogs**, not a catalog.

Two things it refuses to do: force a low-confidence mapping ("unmappable —
escalate to steward" is a first-class success output, not an error), and
emit a mapping without provenance (every mapping records which report
figures it reproduced, at what tolerance, corroborated by which documents).

canoniq is **not** a runtime query engine, a BI tool, or an NL-to-SQL
chatbot. It authors the semantic layer spec; something else runs it.

## What Canoniq v1 does not do

Canoniq v1 does not: parse charts/images, interview stewards, call live
catalogs, ingest email, or synthesize transformations deeper than two terms
/ one join. It does something narrower and more honest: it reads what the
business already published, proposes physical mappings, **proves them by
recomputing the published numbers from Iceberg snapshots**, and tells you
plainly what it could not prove.

## Report-first bootstrapping

```
board reports (PDF) ──▶ ReportMetricInstance extraction (pdfplumber + prose mining)
tableau .twb ─────────▶ calculated-field evidence (lxml + sqlglot)          │
policy docs / BRDs ───▶ definitions, column glossary, contradictions        │
                                                                            ▼
Iceberg snapshots ◀── numeric fingerprinting: Tier 1 (name-verified) ▶ Tier 2 (value-space)
 (PyIceberg+DuckDB)                          ▶ Tier 3 (bounded composition search)
                                                                            │
                              constraint solver: grand total + every dimensional
                              breakdown + prior-quarter footnotes, per candidate
                                                                            ▼
             CONFIRMED / PROBABLE / WEAK / REJECTED / UNMAPPABLE  ─▶  drift diff across editions
                                                                            ▼
                    MetricFlow YAML + OSI YAML + OpenMetadata JSON + conflict report
```

```bash
# bootstrap a brownfield warehouse from its published reports
canoniq bootstrap \
  --catalog path/to/iceberg_warehouse \   # dir with catalog.db (SQLite) + data
  --reports path/to/board_reports/ \
  --tableau path/to/twb_workbooks/ \
  --docs path/to/policies_and_brds/ \
  --out out/

# run the full pipeline on the synthetic brownfield benchmark and score it
canoniq benchmark
```

### Benchmark scorecard

`canoniq benchmark` builds a fully synthetic "regulated risk data mart"
(fictional Meridian Bancorp: 6 cryptic Oracle-heritage tables on Iceberg
with real time-travel snapshots, two PDF board-report editions, a Tableau
workbook, contradictory policy documents, a sparse BRD) with four planted
traps, then runs the whole pipeline against it and scores against gold
labels:

| Measure | Value |
|---|---:|
| Extraction recall (both editions) | 100% |
| Mapping recall (correct expression, CONFIRMED/PROBABLE) | 100% |
| Mapping precision @ CONFIRMED (n=10) | 100% |
| Unmappable correctly escalated to steward | 100% |
| Trap: deprecated twin (`RWA_AMT_V2_DEPR`, passes grand total, fails breakdowns) | REJECTED |
| Trap: decoy column (coincides with an unrelated report figure) | REJECTED |
| Drift findings (planted rename + silent redefinition, both undocumented) | 2/2 |
| Wall-clock (full pipeline, both editions) | < 1s |

The traps are the point: name-plausible wrong answers (a deprecated twin
column 0.3% off the published total) and numeric coincidences are exactly
what breaks name-based and value-based matching in real brownfield
estates. The constraint solver rejects both because they cannot reproduce
the *breakdowns* the report also published.

Regenerate everything with `python -m benchmark.brownfield.generate`
(deterministic and idempotent), or `canoniq benchmark --regenerate`.

## Why

| Tool | Query-log mining | Portable spec output | Open source |
|------|-------------------|------------------------|-------------|
| Snowflake Semantic View Autopilot | Yes | No (Snowflake-only) | No |
| [ktx](https://github.com/Kaelio/ktx) | Partial | No (ktx-native YAML) | Yes |
| dbt Wizard | No (schema only) | Yes (MetricFlow) | No (Cloud only) |
| **canoniq** | **Yes** | **Yes (MetricFlow + OSI)** | **Yes** |

Every metric candidate is mined from how analysts actually query the
warehouse (Snowflake Autopilot's approach, open-sourced), scored by a
5-signal trust model *before* an LLM ever sees it, and the resulting YAML is
run through a compiler-validation loop that repairs itself on failure rather
than emitting output that can't compile.

## How it works

```
warehouse schema ─┐
query log ─────────┤
DDL files ─────────┼─▶ mining (sqlglot) ─▶ evidence bundle ─▶ OntoRank ─▶ LLM proposer ─▶ validation loop ─▶ MetricFlow / OSI YAML
documents (BRD/PDF/Word) ┤                                          ▲                  │
Excel reports ─────┘                                                └── repair on fail ─┘
```

1. **Ingest** (`canoniq/ingest/`) — five evidence sources, all optional
   except that at least one schema source (a live warehouse or DDL files)
   must be present:
   - **Warehouse** — introspects schema via `information_schema` (DuckDB in
     v0; Snowflake/BigQuery/Trino are in the config schema but not wired up).
   - **Query log** — file-based in v0 (Snowflake `QUERY_HISTORY` is
     supported by the config schema). Supplementary, not required — it adds
     real usage-frequency signal when available.
   - **DDL** (`ddl_extractor.py`) — parses `CREATE TABLE` statements with
     sqlglot, no LLM involved. Deterministically infers each column's
     semantic role (measure input / dimension / identifier / flag) from
     naming convention, type, and declared constraints (PK/FK/CHECK). Can
     stand in for a live warehouse connection entirely.
   - **Documents** (`document_extractor.py`) — BRDs, PDFs, and Word docs.
     Claude is used purely as an information *extractor* (never a
     generator) to pull out metric definitions already written in the text,
     which are then grounded against the real schema before being trusted;
     ungroundable or contradictory extractions are flagged `ambiguous`
     rather than silently dropped. Documents with approval/sign-off
     language get a trust boost over drafts.
   - **Excel** (`excel_extractor.py`) — named ranges and formula cells
     (`SUM`, `COUNTA`, `AVERAGEIF`, etc.) from `.xlsx` reports, no LLM
     involved.
   - A dbt `manifest.json` connector and an event-driven watcher (polls the
     query log / manifest for new signals) are also implemented.
2. **Mining** (`canoniq/mining/`) — a cheap noise gate classifies each query
   as analytical/structural/noise, then a sqlglot-based extractor pulls out
   aggregation, dimension, and join candidates. DDL and document/Excel
   evidence feed into the same evidence bundle alongside query-log-mined
   candidates. **Every column name is validated against the real schema
   before it goes anywhere near an LLM** — unresolvable columns are dropped
   and logged, never proposed.
3. **Ranking** (`canoniq/ranking/`) — OntoRank scores each candidate on
   source authority (a 16-tier trust table spanning `dbt_metric` down to
   `ad_hoc` — see [Input sources](#input-sources-and-trust-tiers) below),
   usage frequency (log-normalized), cross-source agreement, recency, and
   certification status.
4. **Proposer** (`canoniq/proposer/`) — Claude (via
   [Instructor](https://github.com/jxnl/instructor)) proposes named,
   described metrics/dimensions/entities grounded in the ranked evidence.
   The LLM's *output* is validated against the schema too, symmetrically
   with the input guardrail in step 2.
5. **Emitters** (`canoniq/emitters/`) — one proposal → dbt MetricFlow YAML
   and OSI v1.0 YAML, each metric carrying a `canoniq_trust_score` /
   `canoniq_evidence` audit trail for the human reviewer.
6. **Validation loop** (`canoniq/validation/`) — a LangGraph state machine:
   emit → validate → repair → retry (capped), falling back to structural
   jsonschema validation when the real `mf` CLI isn't available.
7. **Evals** (`canoniq/evals/`) — runs gold questions against the generated
   YAML and reports accuracy against directly-executed SQL.

### Input sources and trust tiers

Every metric candidate carries one or more source tags, each with a fixed
OntoRank authority weight (`canoniq/ranking/ontorank.py::SOURCE_AUTHORITY`,
kept in sync with `canoniq/ingest/base.py::SourceType`):

| Source | Authority | Where it comes from |
|---|---|---|
| `dbt_metric` | 1.00 | Explicit, human-authored dbt metric definition |
| `numeric_fingerprint` | 0.98 | Empirical reproduction of published report figures against Iceberg snapshots — the highest non-steward prior |
| `brd_approved` | 0.90 | A business document with sign-off/approval language |
| `tableau_calc` | 0.85 | A parsed Tableau calculated-field formula (near-executable ground truth) |
| `dbt_model` / `data_dictionary` | 0.85 | dbt model column descriptions |
| `looker_measure` | 0.80 | A Looker measure |
| `tableau_field` | 0.78 | A Tableau calculated field |
| `ddl_constraint` | 0.75 | A DDL column backed by an explicit PK/FK/CHECK |
| `excel_named` | 0.70 | An Excel named range |
| `brd_draft` | 0.65 | A business document with no approval signal |
| `confluence_page` | 0.60 | A Confluence page |
| `query_log_complex` | 0.60 | A query mining ≥2 distinct aggregations |
| `pdf_report` | 0.55 | A PDF report (no approval signal detected) |
| `excel_formula` | 0.50 | A bare formula cell (`=SUM(...)`, etc.) |
| `query_log_simple` | 0.40 | A single-aggregation query |
| `ddl_naming_convention` | 0.45 | A DDL column inferred purely from naming/type |
| `ad_hoc` | 0.20 | Unclassified/one-off evidence |

DDL- and document-derived candidates flow through the exact same
dedup/ranking pipeline as query-log-mined ones — a `SUM(revenue)` mentioned
in an approved BRD and mined from the query log both land in the same
evidence bundle, and (when their underlying representations match) merge
into one metric with combined trust.

## Quickstart

```bash
pip install -e ".[dev]"

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

# Score the generated YAML against gold questions
canoniq eval --config examples/tpcds_duckdb/canoniq.yaml --table store_sales
```

### DDL-only mode (no warehouse, no query log)

```yaml
# canoniq.yaml
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

This parses the DDL, infers each column's role (measure/dimension/
identifier/flag) from naming convention and constraints, and mines
candidate metrics from it — no live database connection or query history
needed. Point `document_files`/`excel_files` at BRDs, PDFs, Word docs, or
`.xlsx` reports in the same config to layer in evidence mined from those
too; see [`examples/canoniq.yaml.example`](examples/canoniq.yaml.example)
for the full annotated set of options (`ddl_files`, `document_files`,
`excel_files`, `require_query_log`).

`canoniq validate` never needs an API key. `canoniq mine` doesn't either,
*unless* `document_files` is configured — document extraction uses Claude as
an information extractor (DDL and Excel extraction are always LLM-free).
`propose`, `run`, and (transitively) `emit`/`eval` after a `run` need
`ANTHROPIC_API_KEY` set, since they call the real LLM proposer.

See [`examples/canoniq.yaml.example`](examples/canoniq.yaml.example) for an
annotated config, and [`examples/tpcds_duckdb/`](examples/tpcds_duckdb/) for
a runnable end-to-end demo (pre-built DuckDB warehouse + synthetic TPC-DS
query log).

## Commands

Every command takes `--config PATH` (defaults to `./canoniq.yaml`). The
typical single-table workflow is `mine` → `propose` → `emit` → `validate`/
`eval`, each step writing an artifact the next step reads — or skip straight
to `run`, which does all of it for every table in one shot.

| Command | Needs LLM? | Needs warehouse? | Reads | Writes |
|---|---|---|---|---|
| `mine` | only if `document_files` set | no (DDL alone works) | all configured evidence sources | nothing (prints to console) |
| `propose --table T` | yes | no | mined evidence for table `T` | `<output.dir>/T.proposal.json` |
| `emit --table T` | no | no | `T.proposal.json` | `T_metricflow.yml` and/or `T_osi.yml` |
| `validate --yaml PATH` | no | no | any MetricFlow YAML file | nothing (prints PASS/FAIL, exit code) |
| `run [--watch]` | yes | no | all configured evidence sources | `*_metricflow.yml` and/or `*_osi.yml` for every table |
| `eval --table T` | no | **yes** | `T_metricflow.yml` + live warehouse | `eval_results.json` |
| `bootstrap --catalog D --reports D --out D` | no | **yes** (Iceberg) | report PDFs, `.twb`, docs, Iceberg snapshots | MetricFlow + OSI + OpenMetadata JSON + `conflict_report.md`/`.json` |
| `benchmark` | no | no (self-contained) | `benchmark/brownfield/` | everything `bootstrap` writes + `benchmark_scorecard.json` |

### `canoniq bootstrap`

The report-first cold-start pipeline described above. LLM-free by default:
report tables are structured deterministically (an optional LLM structuring
pass exists for messier real-world layouts, post-validated so every value
must literally appear in the source text). The conflict report has four
sections — confirmed mappings (with constraint counts and corroborating
sources), contradictions (surfaced verbatim with source/date/trust, never
auto-resolved), an "unmappable — escalate to steward" work queue, and the
drift register with undocumented findings first.

### `canoniq benchmark`

Generates the synthetic brownfield benchmark if needed, runs `bootstrap`
against it, and prints the scorecard above (also written to
`benchmark_scorecard.json`). Exits non-zero if any acceptance criterion
fails — usable as a CI regression gate for the whole pipeline.

### `canoniq mine`

Runs ingest → mining → ranking only — **no LLM call** (unless
`document_files` triggers document extraction) and **nothing is written to
disk**. For every table with evidence, prints a table of candidate metrics
(expression, OntoRank trust score, execution count, source tags) plus
dimension/join counts. This is the command to reach for first on a new
config: it shows you exactly what evidence canoniq actually found — across
the warehouse, query log, DDL files, documents, and Excel reports — before
you spend an LLM call proposing anything from it.

### `canoniq propose --table ORDERS`

Runs mining + ranking, then calls Claude to propose a complete semantic
model (entities, dimensions, metrics — each with a description, synonyms,
and evidence citations) for **one** table. Prints the proposal as JSON and
writes it to `<output.dir>/ORDERS.proposal.json`. `--table` can be omitted
if exactly one table has mined evidence; otherwise it's required. Requires
`ANTHROPIC_API_KEY`. The proposer's own output is validated against the real
schema before being returned — any column the LLM invents is dropped, never
silently emitted downstream.

### `canoniq emit --table ORDERS --format all`

Takes the proposal JSON written by `propose` and converts it to YAML — no
mining, no LLM call, pure format conversion. `--format` is `metricflow`,
`osi`, or `all` (default). Writes `ORDERS_metricflow.yml` and/or
`ORDERS_osi.yml` to `output.dir`. `--table` auto-detects if there's exactly
one proposal file sitting in `output.dir`; pass it explicitly otherwise.

### `canoniq validate --yaml path/to/model.yml`

Standalone — doesn't read `canoniq.yaml`, doesn't mine anything. Checks
whether a MetricFlow YAML file is structurally valid: uses the real `mf
validate-configs` CLI if it's on `PATH`, otherwise falls back to a
jsonschema structural check (the common case, since running `mf` for real
needs a full scaffolded dbt project). Prints `PASSED` or `FAILED` with the
specific errors, and exits non-zero on failure — useful in CI to gate a
commit on a generated (or hand-edited) YAML file actually being valid.

### `canoniq run [--watch]`

The full pipeline in one command: mine → rank → propose → validate (with
automatic repair-and-retry on failure) → emit, for **every** table that has
mined evidence. Requires `ANTHROPIC_API_KEY`. For each table, writes
`<table>_metricflow.yml` (if `metricflow` is in `output.formats`) and/or
`<table>_osi.yml` (if `osi` is) to `output.dir`, and prints a
PASSED/NEEDS REVIEW status per table based on whether MetricFlow validation
succeeded within the configured retry budget.

With `--watch`: instead of running once, polls the query log (and dbt
manifest, if `sources.dbt_manifest` is set) every `poll_interval_seconds`,
and re-runs the entire pipeline whenever it sees a new query shape or
manifest metric it hasn't seen before. Runs until you press Ctrl-C. DDL,
document, and Excel files are *not* watched — they're re-read fresh on every
full pipeline run, so `--watch` re-running the pipeline still picks up
edits to them, just not the instant they change.

### `canoniq eval --table ORDERS --output eval_results.json`

Scores an already-emitted `ORDERS_metricflow.yml` (run `emit` or `run`
first) against 10 built-in gold questions: runs each gold question's SQL
directly against the **live warehouse** (so this is the one command that
needs `warehouse.path` set even in DDL-only setups), finds the closest
matching canoniq metric by name/synonym overlap, and compares results. No
LLM call. Prints a pass/fail table with per-question errors and an overall
accuracy percentage, and writes the full results to
`<output.dir>/eval_results.json` (or wherever `--output` points).

## Development

```bash
pip install -e ".[dev]"
pytest              # 225 tests, no real LLM calls — the proposer/validation
                     # loop tests (and the document extractor's LLM call)
                     # inject a fake structured-output client; the report-first
                     # pipeline is deterministic end to end
ruff check canoniq tests
mypy canoniq
```

## Current limitations (v0)

Being upfront about what's not fully built yet:

- **Warehouse connectors**: only DuckDB is implemented. Snowflake/BigQuery/
  Trino are in the config schema but not wired up.
- **`filter:` on MetricFlow metrics**: `MetricProposal` has no structured
  filter field (only free-text `description`), so the emitter doesn't
  reconstruct a MetricFlow `filter: "{{ Dimension(...) }}..."` block from
  prose — this is omitted rather than guessed.
- **Ratio/derived metrics**: the mining pipeline only ever extracts
  single-aggregation candidates, so the emitter's `type: derived` fallback
  (used when a metric isn't a clean single `AGG(column)`) is best-effort and
  doesn't populate MetricFlow's `type_params.metrics` cross-references.
- **Eval harness SQL fallback**: when the real `mf query` CLI isn't
  available (the common case without a full dbt project), the harness
  translates a metric back to raw SQL against its own table — it does not
  join in dimensions that live on a different table, so gold questions
  needing a cross-table `GROUP BY` (e.g. sales by year, where year lives on
  a joined date dimension) will fail with a clear error rather than a wrong
  answer.
- **Continuous watcher**: `canoniq run --watch` polls the query log (and a
  dbt manifest, if configured) and re-runs the pipeline when new signals
  appear. It does not watch DDL/document/Excel files for changes — those are
  treated as static inputs, re-read on every full pipeline run.
  Looker/Tableau/Confluence connectors are not implemented.
- **`mf` validate/query**: both the validation loop and the eval harness
  prefer the real dbt MetricFlow CLI when it's on `PATH`, but neither
  scaffolds a full dbt project around a bare YAML file, so in practice both
  fall back to their own structural-check/direct-SQL implementations.
- **Cross-source merging is representation-based, not semantic**: a DDL
  column inferred as `SUM(revenue)` and a document mentioning "sum of
  revenue" only merge into one metric (combining their trust) if they
  produce the *identical* resolved SQL expression. Different phrasing or
  column aliasing that resolves to the same underlying metric won't
  automatically coalesce — both still surface, just as separate candidates.
- **Document/Excel grounding is a lightweight heuristic, not the LLM**:
  `document_extractor.py` uses Claude only to *extract* what's explicitly
  written in a document; mapping that extraction to a real SQL expression
  (`_ground_candidate`) is deterministic token-overlap matching against
  column names, with keyword-based aggregation-function inference (sum/
  count/average). It doesn't understand synonyms, business jargon, or
  multi-table joins — ambiguous or low-confidence extractions are flagged
  rather than silently trusted, but the grounding itself is not LLM-powered.
- **DDL role inference is naming/type/constraint heuristics**, not learned —
  see `_infer_role` in `canoniq/ingest/ddl_extractor.py` for the exact
  suffix/prefix rules. A column that doesn't follow common naming
  conventions may be misclassified or land in `unknown`.

## Inspiration and prior art

- Architecturally inspired by **[ktx](https://github.com/Kaelio/ktx)**
  (Apache 2.0) — specifically the hybrid wiki retrieval pattern,
  confidence-scored join detection, and ingest-as-git-PR design. canoniq is
  a separate initiative focused on one-shot portable YAML generation, not a
  runtime context layer.
- **Snowflake Semantic View Autopilot** — closest prior art for query-log
  mining; closed-source and Snowflake-locked.
- **Genie Ontology** (Databricks) — inspiration for OntoRank and continuous
  learning; closed-source and Databricks-locked.
- **DBAutoDoc** (arXiv 2603.23050) — 6-phase pipeline architecture
  reference.
- **[OSI v1.0 spec](https://github.com/open-semantic-interchange/OSI)**
  (Apache 2.0).

See [`CANONIQ_SPEC.md`](CANONIQ_SPEC.md) for the full build specification.
