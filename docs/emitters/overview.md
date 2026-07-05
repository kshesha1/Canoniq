---
id: overview
title: Emitters — MetricFlow, OSI, OpenMetadata
sidebar_position: 1
description: The three output formats canoniq writes, and what each carries.
---

# Emitters

Canoniq emits three formats from the same `SemanticModelProposal`
([`canoniq/proposer/models.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/proposer/models.py)), whether
that proposal came from the mining-first LLM proposer or was assembled
from report-first resolved mappings
(`canoniq/pipeline.py::_proposals_from_mappings`).

## dbt MetricFlow YAML

Code: [`canoniq/emitters/metricflow.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/emitters/metricflow.py).

Emits a `semantic_models` block (entities, dimensions, measures) plus a
top-level `metrics` block. Every metric carries a `meta` audit trail:

```yaml
meta:
  canoniq_trust_score: 0.97
  canoniq_evidence: "numeric_fingerprint (9/9 published figures reproduced (CONFIRMED))"
  canoniq_synonyms: ["Counterparty Credit Exposure"]
```

Metrics below `ontorank.thresholds.auto_merge` get a `# REVIEW REQUIRED`
comment. A clean single `AGG(column)` expression becomes a proper
`type: simple` metric backed by a measure; anything more complex
(ratios, two-term arithmetic) falls back to `type: derived` with a
best-effort `expr`.

This goes through the **same validation loop**
([`canoniq/validation/loop.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/validation/loop.py)) for
both pipelines: emit → validate (`mf validate-configs` if on `PATH`,
else a jsonschema structural check) → repair (feed errors back to the
LLM proposer) → retry, capped at `llm.max_retries`. Report-first
proposals go through the same loop even though they were never LLM-
proposed in the first place — the repair step is simply less likely to be
needed, since fingerprint-confirmed expressions are already valid SQL by
construction.

## OSI v1.0 YAML

Code: [`canoniq/emitters/osi.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/emitters/osi.py).

Emits the [OSI v1.0 spec](https://github.com/open-semantic-interchange/OSI)
shape: `datasets` (fields = entities + dimensions), `relationships` (from
joins), `metrics` (each with an `ai_context.synonyms` block). Report-first
dimension bindings resolved by the fingerprint solver
(`DimensionBinding.join_from`/`join_to`) become OSI relationships
automatically.

## OpenMetadata JSON

Code: [`canoniq/emit/openmetadata.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/emit/openmetadata.py).
**Report-first only** — there's no mining-first equivalent yet.

**Files only — no live API client.** Canoniq is a **population engine for
empty OpenMetadata catalogs**, not a catalog itself; live API integration
is a v2 idea. Four JSON files, each validated against a pinned subset of
the OpenMetadata 1.5.0 JSON schema before being written
(`canoniq/emit/openmetadata.py::validate_payloads`):

| File | Contents |
|---|---|
| `glossary.json` | One `Glossary` entity: "Canoniq bootstrapped business glossary" |
| `glossary_terms.json` | One `GlossaryTerm` per confirmed/probable metric — description from the report's own prose hint when available, `synonyms` populated from drift-detected renames |
| `tables.json` | Table/column entities, with `tags` linking measure and dimension columns to their glossary term |
| `lineage.json` | Edges from physical column → the report (modeled as a `Dashboard`-like entity), one per term of the resolved expression |

Every term carries a **`canoniq_provenance`** extension block — this is
never dropped to fit the schema, since OpenMetadata explicitly supports
custom extension properties:

```json
{
  "extension": {
    "canoniq_provenance": {
      "confidence_band": "CONFIRMED",
      "constraints_satisfied": 9,
      "constraints_total": 9,
      "physical_expression": "SUM(RWA_CALC_FCT.RWA_AMT_V3)",
      "report_id": "sor_2026q1",
      "tiers_run": ["tier1"],
      "corroborations": ["report commentary: \"Total Credit RWA is the sum of...\""],
      "source_locators": ["sor_2026q1: page 3, table 1, row 1", "..."]
    }
  }
}
```

`UNMAPPABLE` metrics never get a glossary term at all — Canoniq doesn't
populate a catalog with unproven guesses.
