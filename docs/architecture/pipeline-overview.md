---
id: pipeline-overview
title: Pipeline overview
sidebar_position: 1
description: How report-first bootstrap and mining-first proposal fit together.
---

# Pipeline overview

Canoniq has two pipelines that share evidence, trust-scoring, and emitter
code but start from opposite ends of the problem.

## Report-first bootstrap (`canoniq bootstrap` / `canoniq benchmark`)

This is the pipeline described in the project's `CANONIQ_V1_REPORTFIRST_SPEC.md`
build spec (repo root, not part of this docs site) and implemented in
[`canoniq/pipeline.py`](../../canoniq/pipeline.py) as a LangGraph state
machine with five nodes:

```
 ingest ──▶ fingerprint ──▶ drift ──▶ emit ──▶ report
```

| Node | What it does | Code |
|---|---|---|
| `ingest` | Extracts `ReportMetricInstance`s from every PDF, `TableauEvidence` from every `.twb`, and mines a column glossary + evidence statements from policy docs/BRDs | `canoniq/pipeline.py::ingest_node` |
| `fingerprint` | Runs the 3-tier numeric fingerprinting engine + constraint solver per report, per metric | `canoniq/pipeline.py::fingerprint_node` → `canoniq/fingerprint/solver.py::resolve_all` |
| `drift` | Diffs the two most recent report editions (if ≥2 exist) for renames/redefinitions | `canoniq/pipeline.py::drift_node` → `canoniq/drift/report_diff.py::diff_reports` |
| `emit` | Converts accepted mappings into `SemanticModelProposal`s and runs them through the **existing** MetricFlow/OSI emitters and validation loop, plus the OpenMetadata emitter | `canoniq/pipeline.py::emit_node` |
| `report` | Detects cross-source contradictions and writes the conflict report (markdown + JSON) | `canoniq/pipeline.py::report_node` → `canoniq/report/conflict.py` |

This reuses the pre-existing `canoniq/validation/loop.py` (emit → validate
→ repair → retry against `mf validate-configs` or a structural jsonschema
fallback) completely unchanged — report-first mappings just become another
kind of `SemanticModelProposal` input to it.

## Mining-first proposal (`canoniq mine` / `propose` / `emit` / `run`)

The original v0 pipeline, still fully supported:

```
warehouse schema ─┐
query log ─────────┤
DDL files ─────────┼─▶ mining (sqlglot) ─▶ evidence bundle ─▶ OntoRank ─▶ LLM proposer ─▶ validation loop ─▶ MetricFlow / OSI YAML
documents (BRD/PDF/Word) ┤                                          ▲                  │
Excel reports ─────┘                                                └── repair on fail ─┘
```

See [`canoniq/ingest/`](../../canoniq/ingest/),
[`canoniq/mining/`](../../canoniq/mining/), and
[`canoniq/proposer/`](../../canoniq/proposer/) for the five evidence
sources, the sqlglot-based candidate extractor, and the Claude-via-
Instructor proposer respectively.

## Where they meet

Both pipelines converge on the same three concepts:

1. **OntoRank** ([`canoniq/ranking/ontorank.py`](../../canoniq/ranking/ontorank.py)) —
   a fixed per-source-type trust prior (see
   [OntoRank & trust tiers](./ontorank-trust.md)), whether the evidence came
   from a query log, a DDL constraint, a Tableau calculated field, or —
   report-first only — an empirical numeric fingerprint (the highest
   non-steward prior at 0.98).
2. **`SemanticModelProposal`** ([`canoniq/proposer/models.py`](../../canoniq/proposer/models.py)) —
   the one shape both pipelines produce before handing off to the emitters
   and validation loop. Report-first mappings are converted into this
   shape in `canoniq/pipeline.py::_proposals_from_mappings`.
3. **The three emitters** — MetricFlow, OSI, and (report-first only)
   OpenMetadata. See [Emitters](../emitters/overview.md).

If you're only interested in the report-first bootstrap capability, you
can safely ignore `canoniq/ingest/`, `canoniq/mining/`, and
`canoniq/proposer/` — report-first evidence never touches an LLM proposer
call, only the shared emitter/validation code downstream.
