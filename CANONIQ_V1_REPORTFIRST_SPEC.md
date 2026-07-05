# CANONIQ V1 ENHANCEMENT SPEC — Report-First Bootstrapping & Numeric Fingerprinting

**Audience:** Claude Code, operating on the existing `canoniq/` repository (all 13 original spec steps complete, plus `LAYER1_UPDATE_INSTRUCTIONS.md` applied).

**Nature of this spec:** ADDITIVE + one architectural inversion. Do not rewrite existing layers. Existing ingest, OntoRank, proposer, emitters, and validation loop remain; this spec adds new modules and rewires the pipeline entry point.

---

## 0. Repositioning context (read first — it changes design decisions)

Canoniq is no longer "a semantic layer YAML generator." It is a **cold-start semantic bootstrapping engine for brownfield enterprises**: organizations with no dbt, no semantic layer, no documented business-to-physical mappings — only legacy artifacts (cryptic Oracle-heritage schemas now on Iceberg, board-level PDF reports, Tableau workbooks, contradictory policy documents).

The architectural inversion: **the pipeline now starts from the consumption end (board reports), not the schema end.** A quarterly board report is treated as a complete, business-approved specification of the semantic layer. Every figure in it is a metric instance to be mapped backward to physical columns — and every figure is also a **test oracle** used to empirically validate candidate mappings by recomputing published numbers against Iceberg time-travel snapshots.

Three consequences for implementation:

1. `ReportMetricInstance` becomes the central data model. Everything else (OntoRank, fingerprinting, emitters) consumes or produces mappings anchored to report metric instances.
2. "Unmappable — escalate to steward" is a **first-class success output**, not an error path. The system must never force a low-confidence mapping.
3. Every emitted mapping carries full provenance (which report figures it reproduced, at what tolerance, corroborated by which documents).

---

## 1. Scope guardrails (enforce strictly)

**IN SCOPE for this spec:**
- Synthetic brownfield benchmark (Module A)
- Report-first metric extraction from text + tables of PDF reports (Module B)
- Tableau `.twb` extractor (Module C)
- Numeric fingerprinting engine, 3 tiers, bounded (Module D)
- Semantic drift diff across report editions (Module E)
- Conflict report enhancement + unmappable output (Module F)
- OpenMetadata JSON emitter (Module G)
- Pipeline rewiring + CLI (Module H)

**EXPLICITLY OUT OF SCOPE (do not build, do not stub beyond a TODO comment):**
- Chart/image OCR or vision-model extraction from PDFs (text and tables only)
- Steward interview mode (v2)
- Live OpenMetadata API integration (v1 emits JSON files only)
- Email ingestion
- Transformation search beyond single columns and two-term arithmetic
- Multi-hop join synthesis in fingerprinting (max ONE join, and only along declared/inferred FK edges)

If any module tempts expansion beyond these bounds, add a `# V2:` comment and stop.

---

## 2. Module A — Synthetic brownfield benchmark (`benchmark/brownfield/`)

Purpose: a self-contained, fictional "regulated risk data mart" that exercises every hard case. All names, figures, and document text must be invented from public Basel/regulatory vocabulary. Nothing may resemble any real institution's internals.

### A1. Physical layer: Iceberg tables with real time-travel

Use **PyIceberg with a local SQLite catalog and filesystem warehouse** (`benchmark/brownfield/warehouse/`). Real Iceberg snapshots are non-negotiable — the fingerprinting demo depends on `as-of` reads.

Create a generator script `benchmark/brownfield/generate.py` that builds:

**Tables (deliberately cryptic Oracle-heritage names):**

| Table | Meaning (internal comment only) | Key columns |
|---|---|---|
| `CRD_EXP_FCT` | credit exposure fact | `EXP_AMT_USD`, `COLL_HELD_AMT`, `LE_CD`, `ASST_CLS_CD`, `AS_OF_DT`, `CPTY_ID` |
| `RWA_CALC_FCT` | RWA calculation fact | `RWA_AMT_V3`, `RWA_AMT_V2_DEPR` (deprecated twin — trap), `LE_CD`, `ASST_CLS_CD`, `AS_OF_DT` |
| `LE_REF` | legal entity reference | `LE_CD`, `LE_NM`, `RGN_CD` |
| `ASST_CLS_REF` | asset class reference | `ASST_CLS_CD`, `ASST_CLS_DESC` |
| `OPS_LOSS_EVT` | operational loss events | `LOSS_AMT`, `EVT_TYP_CD`, `LE_CD`, `EVT_DT` |
| `MKT_RSK_SNSTVTY` | market risk sensitivities | `SNSTVTY_AMT`, `RSK_FCTR_CD`, `LE_CD`, `AS_OF_DT` |

**Deliberate traps the generator must plant:**
1. `RWA_AMT_V2_DEPR` sums to a plausible-but-wrong total (~7% off the report figure) — fingerprinting must reject it on breakdown constraints.
2. A decoy column in `MKT_RSK_SNSTVTY` whose total coincidentally lands within 2% of one report figure but fails all dimensional breakdowns.
3. One report metric ("Adjusted Stress Capital Buffer") computed in a spreadsheet — it does NOT exist in any table or simple combination. Gold label: `unmappable`.
4. Schema evolution: rename one column between snapshots (e.g. `EXP_AMT` → `EXP_AMT_USD`) so Iceberg schema-evolution history is a usable signal.

**Snapshots:** write data as of two quarter-ends (2025-12-31 and 2026-03-31) as distinct Iceberg snapshots, with realistic quarter-over-quarter drift in the figures.

### A2. Report layer: two editions of a synthetic board report

Generate two PDF editions (Q4-2025, Q1-2026) of a fictional **"State of Risk Report — Meridian Bancorp (fictional)"**, ~12–15 pages each, text and tables only. Use reportlab or weasyprint.

Required content structure (both editions):
- Executive commentary paragraphs that state metric logic in prose, e.g. *"Total Credit Risk Exposure is calculated as gross exposure less collateral held."* (This feeds formula-template extraction.)
- Table: Total Credit RWA — total + breakdown by legal entity (4 entities) + breakdown by asset class (3 classes). **All figures must be exactly reproducible from the Iceberg snapshot for that quarter-end** (the generator computes them from the data, not vice versa).
- Table: Total Credit Risk Exposure (the two-term derived metric: `SUM(EXP_AMT_USD) - SUM(COLL_HELD_AMT)`), total + one breakdown.
- Table: Operational losses by event type.
- One figure that is `unmappable` (trap #3), presented normally.
- Footnotes with one prior-quarter comparative figure per major metric (extra constraints for the solver).

**Drift between editions (feeds Module E):**
- One metric renamed: "Counterparty Credit Exposure" (Q4) → "Adjusted Counterparty Exposure" (Q1), same underlying logic.
- One metric silently redefined: same name, formula changes (e.g. a filter added), no documentation anywhere. Figures differ accordingly.

### A3. Supporting artifacts

- `benchmark/brownfield/tableau/risk_dashboard.twb` — hand-written XML with 3 calculated fields referencing physical columns, at least one matching a report metric (e.g. `SUM([EXP_AMT_USD]) - SUM([COLL_HELD_AMT])`) and one conflicting with a policy document's definition.
- Two synthetic policy documents (markdown → PDF), deliberately contradictory on one metric definition (e.g. whether Total CRE nets collateral), one dated 2019, one 2024.
- One sparse synthetic BRD covering only 2 of the 6 tables.
- `benchmark/brownfield/gold_labels.yaml` — ground truth: for every report metric instance, the correct physical expression (or `unmappable`), used by the eval harness.

### A4. Acceptance criteria for Module A
- `python -m benchmark.brownfield.generate` is fully deterministic (seeded) and idempotent.
- A round-trip test proves every non-trap report figure is reproducible from the correct Iceberg snapshot via PyIceberg + DuckDB within 0.5% tolerance.

---

## 3. Module B — Report-first metric extraction (`canoniq/extract/report.py`)

### B1. Data model (add to `canoniq/models.py`)

```python
class ReportMetricInstance(BaseModel):
    instance_id: str                   # deterministic hash
    report_id: str                     # e.g. "sor_2026q1"
    metric_name_verbatim: str          # exactly as printed
    value: Decimal
    unit: str                          # "USD_bn", "count", "pct"
    scale_factor: Decimal              # e.g. 1e9 for figures printed in $bn
    as_of_date: date                   # report period end
    dimension_context: dict[str, str]  # {"legal_entity": "Meridian NY", ...} — empty dict = grand total
    source_locator: str                # "page 7, table 3, row 2"
    prose_formula_hint: str | None     # extracted commentary logic, verbatim
    parent_total_id: str | None        # links breakdown rows to their total
```

`parent_total_id` is critical: the fingerprinting solver needs to know which figures are breakdowns of which totals to apply them as joint constraints.

### B2. Extraction pipeline

1. **Table extraction:** pdfplumber for table structure. Do NOT use vision models.
2. **LLM structuring pass** (Instructor + Pydantic, consistent with existing proposer patterns): convert each extracted table + surrounding text into `list[ReportMetricInstance]`. The LLM's job is structuring and unit/scale detection, not invention — every `value` must literally appear in the extracted text (post-validate this: reject any instance whose value string is absent from the source page text).
3. **Prose formula mining:** second LLM pass over commentary paragraphs producing `FormulaHypothesis` objects: `{metric_name, structure: "A - B" | "A / B" | "SUM(A)", term_descriptions: ["gross exposure", "collateral held"]}`. These seed Tier-3 fingerprinting.
4. **Consistency validation (deterministic, no LLM):** for every parent/child group, check `sum(children) ≈ parent` within tolerance; flag violations into the conflict report as `internal_report_inconsistency`.

### B3. Acceptance
- On the synthetic Q1 report: ≥ 95% of gold-labeled metric instances extracted with correct value, scale, as-of date, and dimension context.

---

## 4. Module C — Tableau `.twb` extractor (`canoniq/extract/tableau.py`)

`.twb` is XML. No LLM needed — pure `lxml`.

Extract:
- Datasource connections → physical table references
- `<column>` elements with `<calculation formula="...">` → parse formula with a small translator: Tableau `[COL]` refs → physical column identifiers; map Tableau aggregates (SUM/AVG/COUNTD) to SQL equivalents. Use sqlglot to normalize the resulting expression.
- Worksheet-level shelf usage → which columns act as dimensions vs measures in practice.

Output: `TableauEvidence(source_file, caption, physical_expr_sql, referenced_columns, worksheet_names)`.

**Trust prior:** register in the existing evidence-type registry (from `LAYER1_UPDATE_INSTRUCTIONS.md`) at just below steward-confirmed — e.g. 0.85 if stewards are 1.0. Tableau calculated fields are near-executable ground truth. Malformed/unparseable formulas: log and skip, never crash.

---

## 5. Module D — Numeric fingerprinting engine (`canoniq/fingerprint/`)

The centerpiece. Three tiers, run in order, each strictly bounded. All execution via DuckDB reading PyIceberg snapshot scans; every query MUST target the snapshot whose timestamp matches the report's `as_of_date` (fail loudly if no snapshot within ±3 days exists).

### D0. Shared machinery (`fingerprint/executor.py`)
- `evaluate(expr_sql, snapshot_ts, group_by: list[str] | None, filters: dict) -> Decimal | dict`
- Tolerance model: `match if |computed - reported| / |reported| <= tol`, default `tol = 0.005`, configurable. Handle scale factors from `ReportMetricInstance.scale_factor` BEFORE comparison. Use `Decimal` throughout — no float comparison on money.
- Result cache keyed on `(expr_hash, snapshot_ts, groupby, filters)` — the solver re-evaluates aggressively.

### D1. Tier 1 — Verification of name-based candidates
Input: OntoRank's top-k shortlist (k=10) per report metric. For each candidate expression, evaluate against the grand-total constraint. Survivors proceed to constraint scoring (D4). Cheap; run first, always.

### D2. Tier 2 — Value-space search (name-independent)
For report metrics where Tier 1 produced zero survivors:
- Enumerate all numeric columns across the catalog (from existing schema ingest).
- For each: evaluate `SUM(col)` and `COUNT(*)`-style aggregates at the snapshot, compare to the grand total. This is a full scan of single-column hypotheses — with ~6 tables it is trivially cheap; add a `max_columns` config (default 500) so it stays bounded on real catalogs.
- Any hit within tolerance becomes a candidate **regardless of name similarity**. This is the channel that finds `AGG_EXP_ADJ_V3`-style mappings with zero lexical overlap.

### D3. Tier 3 — Bounded composition search
Only for metrics still unresolved AND only when structure hints exist:

Hypothesis generators, in priority order:
1. **Prose-formula seeded:** for each `FormulaHypothesis` (e.g. "A − B"), resolve each term description independently via OntoRank + embedding match against column names/descriptions (each term is an easier sub-problem than the whole), take top-3 candidates per term, enumerate combinations, evaluate.
2. **Tableau seeded:** any `TableauEvidence.physical_expr_sql` whose caption embeds near the metric name.
3. **Locality-bounded blind search (last resort):** two-term `A − B`, `A + B`, `A / B` where BOTH columns come from tables already containing a resolved metric, or one FK-hop away in the join graph. Hard cap: `max_tier3_hypotheses = 2000` per metric; if exceeded, truncate by OntoRank score and log.

Allowed expression grammar for v1 (enforce with a validator):
```
expr := AGG(col) | AGG(col) OP AGG(col) | AGG(col) WHERE simple_predicate
AGG  := SUM | COUNT | AVG
OP   := + | - | /
join := at most one, only along join-graph edges
```
Anything outside this grammar is out of scope. Do not add depth.

### D4. Constraint-satisfaction scoring (`fingerprint/solver.py`)
This is what separates real mappings from coincidences. For each surviving candidate:

1. Assemble the metric's full constraint set from `ReportMetricInstance` rows: grand total + every dimensional breakdown row (via `parent_total_id`) + prior-quarter footnote figures (evaluated against the EARLIER snapshot).
2. Breakdown constraints require dimension resolution: map the report's dimension labels ("Meridian NY") to reference-table values (`LE_REF.LE_NM`) via exact/fuzzy match; the implied `GROUP BY` column becomes part of the candidate mapping (so a confirmed mapping resolves the measure AND its dimension joins simultaneously).
3. Score: `constraints_satisfied / constraints_total`, with per-constraint tolerance pass/fail. Record each constraint's outcome in the evidence bundle.
4. Confidence bands: `CONFIRMED` (≥ 4 constraints, ≥ 90% satisfied), `PROBABLE` (≥ 2, ≥ 75%), `WEAK` (grand total only), `REJECTED`, `UNMAPPABLE` (no candidate reaches WEAK after all tiers).
5. Feed the score into OntoRank as a new evidence type `numeric_fingerprint` with the highest non-steward trust prior — empirical reproduction outranks any document.

**The deprecated-twin trap (A1 #1) is the regression test for this module:** `RWA_AMT_V2_DEPR` must be REJECTED on breakdown constraints even though (configure the synthetic data so) it may pass no worse than near the grand total; `RWA_AMT_V3` must be CONFIRMED.

### D5. Acceptance
- On the benchmark: all gold non-trap metrics reach CONFIRMED or PROBABLE with the correct expression; both traps REJECTED; the spreadsheet-only metric lands UNMAPPABLE.
- Full pipeline run on the benchmark completes in < 5 minutes on a MacBook Pro.

---

## 6. Module E — Semantic drift diff (`canoniq/drift/report_diff.py`)

Input: two sets of `ReportMetricInstance` + their resolved mappings, from two report editions.

Detect and emit `DriftFinding` records:
1. **Rename:** metric present in edition A, absent in B, but a B metric resolves to the SAME physical expression → `renamed(old, new)`.
2. **Silent redefinition:** same `metric_name_verbatim` in both, but resolved expressions differ, OR the old expression evaluated at the new snapshot diverges from the new reported figure beyond tolerance → `redefined`, with both expressions and both figures attached.
3. **Appeared / disappeared:** trivial set difference.
4. For every finding: search ingested documents (policy docs, BRDs) for any mention explaining the change; if none, tag `undocumented=True` — that tag is the headline field.

The benchmark's planted rename and silent redefinition (A2) are the acceptance tests.

---

## 7. Module F — Conflict report enhancement (`canoniq/report/conflict.py`)

Extend the existing conflict report to a single markdown (+ JSON) artifact with four sections:

1. **Confirmed mappings** — table: metric | physical expression | confidence band | constraints satisfied (e.g. "7/7 figures reproduced, incl. 4 legal-entity breakdowns") | corroborating sources with locators.
2. **Contradictions** — candidate mappings where evidence disagrees (e.g. Tableau formula vs. policy doc definition; 2019 policy vs. 2024 policy), with verbatim snippets, source, date, and trust score for each side. Never auto-resolve a contradiction between high-trust sources — surface it.
3. **Unmappable — escalate to steward** — every UNMAPPABLE metric with a summary of what was tried (tiers run, best near-miss and its distance). Frame as a work queue, not an error log.
4. **Drift register** — Module E findings, `undocumented=True` items first.

JSON twin of the same content for programmatic consumption.

---

## 8. Module G — OpenMetadata emitter (`canoniq/emit/openmetadata.py`)

Third emitter alongside MetricFlow and OSI. **Files only, no API client.**

Emit OpenMetadata-compatible JSON for:
- **Glossary + GlossaryTerms:** one term per confirmed/probable metric; description from prose hints; `synonyms` from drift-detected renames.
- **Table/column entities** with `tags` linking columns to glossary terms.
- **Lineage edges:** report metric instance → physical columns (represent the report as a `Dashboard`-like entity or a custom entity; keep it simple and valid against OM's published JSON schemas — pin an OM schema version in a constant and validate emitted JSON against it in tests).
- Custom extension block per term: `canoniq_provenance` — confidence band, constraints satisfied, source locators. OM allows custom properties; do not silently drop provenance to fit the schema.

Positioning note for README (write it): Canoniq is a **population engine for empty OpenMetadata catalogs**, not a catalog.

---

## 9. Module H — Pipeline rewiring + CLI

New orchestration entry point (LangGraph, consistent with existing validation-loop style):

```
ingest artifacts (existing Layer 1 + Modules B, C)
  → OntoRank scoring (existing, + tableau & fingerprint evidence types)
  → fingerprint tiers 1→2→3 + constraint solver (Module D)
  → drift diff if ≥ 2 report editions (Module E)
  → emit: MetricFlow YAML + OSI YAML + OM JSON (existing + Module G)
  → validation loop (existing mf/dbt parse self-correction — unchanged)
  → conflict report (Module F)
```

CLI (extend existing):
```
canoniq bootstrap --catalog <iceberg_catalog_uri> --reports <dir> --tableau <dir> --docs <dir> --out <dir>
canoniq benchmark   # runs the full pipeline on benchmark/brownfield and scores against gold_labels.yaml
```

`canoniq benchmark` must print a scorecard: extraction recall, mapping precision/recall by confidence band, trap outcomes, drift findings, wall-clock time. This scorecard is the number that goes in the README and the LinkedIn post.

---

## 10. Build order (do not reorder)

1. Module A (benchmark) — everything else tests against it.
2. Module B (report extraction) — then run extraction acceptance test.
3. Module C (twb) — small, independent.
4. Module D0 + D1 (executor + verification tier) — prove snapshot-correct evaluation on gold labels FIRST.
5. Module D2, then D3, then D4 (solver) — run trap regression tests after each.
6. Module E (drift).
7. Modules F, G, H.
8. Full `canoniq benchmark` green run; update README with scorecard.

Write tests as you go (pytest, consistent with existing test layout). Every module has its acceptance criteria above — treat them as required tests, not documentation.

## 11. Non-goals reminder (paste into README)

Canoniq v1 does not: parse charts/images, interview stewards, call live catalogs, ingest email, or synthesize transformations deeper than two terms / one join. It does something narrower and more honest: it reads what the business already published, proposes physical mappings, **proves them by recomputing the published numbers from Iceberg snapshots**, and tells you plainly what it could not prove.
