---
id: benchmark
title: The brownfield benchmark
sidebar_position: 2
description: What's inside benchmark/brownfield, why the traps matter, and how the scorecard is computed.
---

# The brownfield benchmark

Code: [`benchmark/brownfield/`](https://github.com/kshesha1/Canoniq/blob/main/benchmark/brownfield/generate.py).
Regenerate with `python -m benchmark.brownfield.generate` (deterministic,
seeded, idempotent) or `canoniq benchmark --regenerate`.

## What it is

A fully synthetic "regulated risk data mart" for the fictional **Meridian
Bancorp**. Every name, figure, and document is invented from public
Basel/regulatory vocabulary — nothing resembles any real institution.

```
benchmark/brownfield/
  warehouse/          Iceberg tables (SQLite catalog), two quarter-end snapshots
  reports/            sor_2025q4.pdf, sor_2026q1.pdf — two board-report editions
  tableau/             risk_dashboard.twb
  docs/                policy_credit_risk_2019.{md,pdf}
                       policy_rda_2024.{md,pdf}
                       brd_credit_exposure_mart.{md,pdf}
  gold_labels.yaml     ground truth for every report metric instance
```

### The six tables

| Table | Meaning | Key columns |
|---|---|---|
| `CRD_EXP_FCT` | credit exposure fact | `EXP_AMT_USD`, `COLL_HELD_AMT`, `LE_CD`, `ASST_CLS_CD`, `AS_OF_DT`, `CPTY_ID` |
| `RWA_CALC_FCT` | RWA calculation fact | `RWA_AMT_V3`, `RWA_AMT_V2_DEPR` (deprecated twin — trap), `LE_CD`, `ASST_CLS_CD`, `AS_OF_DT` |
| `LE_REF` | legal entity reference | `LE_CD`, `LE_NM`, `RGN_CD` |
| `ASST_CLS_REF` | asset class reference | `ASST_CLS_CD`, `ASST_CLS_DESC` |
| `OPS_LOSS_EVT` | operational loss events | `LOSS_AMT`, `EVT_TYP_CD`, `LE_CD`, `EVT_DT` |
| `MKT_RSK_SNSTVTY` | market risk sensitivities | `SNSTVTY_AMT`, `HDG_NTNL_AMT` (decoy — trap), `RSK_FCTR_CD`, `LE_CD`, `AS_OF_DT` |

Two quarter-end snapshots (2025-12-31, 2026-03-31) with realistic
quarter-over-quarter drift, built with **real PyIceberg snapshots** — the
fingerprinting demo genuinely depends on `as-of` reads, not a mocked
substitute.

## The four planted traps

### Trap 1: the deprecated twin

`RWA_CALC_FCT.RWA_AMT_V2_DEPR` is constructed (via iterative proportional
fitting, see `benchmark/brownfield/data.py::_ipf_targets`) so that:

- its **grand total** lands 0.3% off the true `RWA_AMT_V3` total — inside
  the 0.5% fingerprint tolerance, so it survives Tier 1/2's grand-total
  check;
- but every **legal-entity breakdown** is 5–7% off, and every
  **asset-class breakdown** is ≥1% off — so the [constraint
  solver](../architecture/numeric-fingerprinting.md#d4--the-constraint-satisfaction-solver)
  rejects it once it checks the breakdowns.

Expected: `RWA_AMT_V3` → `CONFIRMED`, `RWA_AMT_V2_DEPR` → `REJECTED`.

### Trap 2: the decoy column

`MKT_RSK_SNSTVTY.HDG_NTNL_AMT` is constructed so its Q1 grand total lands
within 0.2% of the Q1 Operational Losses figure — a genuine numeric
coincidence a value-space (Tier 2) search would otherwise trust — but it
fails every dimensional breakdown.

Expected: `SUM(OPS_LOSS_EVT.LOSS_AMT)` → `CONFIRMED`,
`SUM(MKT_RSK_SNSTVTY.HDG_NTNL_AMT)` → `REJECTED`.

### Trap 3: the spreadsheet-only metric

"Adjusted Stress Capital Buffer" is presented normally in both report
editions but exists in no table or simple combination of tables (in
reality, computed in a spreadsheet outside the warehouse entirely).

Expected: `UNMAPPABLE` in both editions, with a near-miss recorded and all
three tiers logged as attempted.

### Trap 4: the silent redefinition

"Market Risk Sensitivities" keeps the same name and the same commentary
prose in both editions, but the Q1 figures quietly exclude one risk factor
(`IR_VEGA`) — **no document anywhere mentions this.**

Expected: Q4 → `SUM(MKT_RSK_SNSTVTY.SNSTVTY_AMT)`; Q1 →
`SUM(MKT_RSK_SNSTVTY.SNSTVTY_AMT) WHERE RSK_FCTR_CD<>'IR_VEGA'` (found via
Tier 3's filter search); the [drift diff](../architecture/drift-detection.md)
flags this as `redefined`, `undocumented=True`.

## Other planted signals

- **Schema evolution:** `CRD_EXP_FCT.EXP_AMT` is renamed to `EXP_AMT_USD`
  between the Q4 and Q1 snapshots (a genuine Iceberg schema-evolution
  event, not a workaround) — exercises the executor's field-ID column
  normalization.
- **A genuine rename:** "Counterparty Credit Exposure" (Q4) becomes
  "Adjusted Counterparty Exposure" (Q1), same underlying logic — the
  other half of the drift-detection acceptance test.
- **Contradictory policy documents:** the 2019 policy says Total Credit
  Risk Exposure does *not* net collateral; the 2024 policy says it *does*;
  the Tableau workbook's "Total Credit Risk Exposure" field also doesn't
  net, while its "Net Credit Exposure" field does. This is the [conflict
  report](#the-conflict-report)'s contradiction-detection acceptance test.
- **A sparse BRD** covering only 2 of the 6 tables (`CRD_EXP_FCT`,
  `LE_REF`) — feeds the column glossary used to boost term-resolution
  scores in Tier 3.

## Running it

```bash
canoniq benchmark
```

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

Scoring logic lives in
[`canoniq/evals/brownfield.py::score_benchmark`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/evals/brownfield.py),
consuming `gold_labels.yaml` and the `BootstrapResult` from
`canoniq/pipeline.py::run_bootstrap`. `canoniq benchmark` exits non-zero
if `Scorecard.passed` is `False` — wire this into CI to catch a regression
in extraction, fingerprinting, drift detection, or trap handling.

## The conflict report

Every `canoniq benchmark` / `canoniq bootstrap` run writes
`conflict_report.md` (+ `.json` twin) with four sections — see
[`canoniq/report/conflict.py`](https://github.com/kshesha1/Canoniq/blob/main/canoniq/report/conflict.py):

1. **Confirmed mappings** — table of metric → physical expression →
   confidence band → constraint summary ("7/7 figures reproduced, incl. 4
   legal-entity breakdowns") → corroborating sources with locators.
2. **Contradictions** — candidate mappings where evidence disagrees
   (e.g. the 2019/2024 policy conflict above), with verbatim snippets,
   source, date, and trust score per side, plus an **empirical
   adjudication note** ("fingerprinting reproduced the published figures
   with ..."). Contradictions between high-trust sources are **never
   auto-resolved** — they're surfaced for a human to read.
3. **Unmappable — escalate to steward** — every `UNMAPPABLE` metric,
   framed as a work queue: which tiers were tried, and the best near-miss
   and its distance from the target.
4. **Drift register** — every `DriftFinding`, undocumented findings first.

This is designed to be the first thing a human opens after a bootstrap run.
