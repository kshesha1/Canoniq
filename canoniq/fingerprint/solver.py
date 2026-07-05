"""D4 — constraint-satisfaction scoring: what separates real mappings from
coincidences.

Every candidate that survives a tier's grand-total check is scored against
the metric's FULL constraint set: grand total + every dimensional breakdown
row + prior-period footnote figures (evaluated against the earlier
snapshot). Dimension labels are resolved to physical columns/joins, so a
confirmed mapping resolves the measure AND its dimension joins at once.

"Unmappable — escalate to steward" is a first-class success output: the
solver never forces a low-confidence mapping.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter, SnapshotNotFoundError
from canoniq.fingerprint.executor import SnapshotExecutor
from canoniq.fingerprint.tiers import (
    NearMiss,
    ScoredCandidate,
    tier1_candidates,
    tier2_candidates,
    tier3_candidates,
)
from canoniq.models import (
    CandidateEvaluation,
    ConfidenceBand,
    ConstraintKind,
    ConstraintResult,
    DimensionBinding,
    FormulaHypothesis,
    ReportMetricInstance,
    ResolvedMapping,
    TableauEvidence,
)

logger = logging.getLogger(__name__)


@dataclass
class MetricGroup:
    """One report metric: its grand total plus all linked constraints."""

    metric_name: str
    grand_total: ReportMetricInstance
    breakdowns: list[ReportMetricInstance] = field(default_factory=list)
    priors: list[ReportMetricInstance] = field(default_factory=list)

    @property
    def as_of(self) -> date:
        return self.grand_total.as_of_date


def group_instances(instances: list[ReportMetricInstance]) -> list[MetricGroup]:
    by_name: dict[str, list[ReportMetricInstance]] = {}
    for inst in instances:
        by_name.setdefault(inst.metric_name_verbatim, []).append(inst)

    groups = []
    for name, insts in by_name.items():
        totals = [i for i in insts if not i.dimension_context]
        if not totals:
            continue
        current_as_of = max(i.as_of_date for i in totals)
        grand = next(i for i in totals if i.as_of_date == current_as_of)
        groups.append(
            MetricGroup(
                metric_name=name,
                grand_total=grand,
                breakdowns=[i for i in insts if i.dimension_context],
                priors=[
                    i for i in totals
                    if i.as_of_date < current_as_of and i.instance_id != grand.instance_id
                ],
            )
        )
    return groups


@dataclass
class FingerprintContext:
    adapter: IcebergCatalogAdapter
    executor: SnapshotExecutor
    config: FingerprintConfig
    hypotheses: list[FormulaHypothesis] = field(default_factory=list)
    tableau_evidence: list[TableauEvidence] = field(default_factory=list)
    glossary: dict[tuple[str, str], str] = field(default_factory=dict)
    resolved_tables: set[str] = field(default_factory=set)


# --- dimension resolution -----------------------------------------------------


def _match_label(label: str, values: list[str]) -> str | None:
    """Map a report dimension label to a physical column value: exact,
    code-embedded-in-label ('Internal Fraud (INT_FRD)'), or fuzzy."""
    for value in values:
        if label.lower() == value.lower():
            return value
    for value in values:
        if len(value) >= 3 and value.lower() in label.lower():
            return value
    best, best_ratio = None, 0.85
    for value in values:
        ratio = SequenceMatcher(None, label.lower(), value.lower()).ratio()
        if ratio >= best_ratio:
            best, best_ratio = value, ratio
    return best


def resolve_dimension(
    dimension_key: str,
    labels: list[str],
    measure_table: str,
    adapter: IcebergCatalogAdapter,
    as_of: date,
) -> DimensionBinding | None:
    """Find the column (in the measure table, or one join hop away) whose
    distinct values account for every report label."""

    def try_column(table: str, column: str) -> dict[str, str] | None:
        values = adapter.distinct_values(table, column, as_of)
        if not values:
            return None
        mapping = {}
        for label in labels:
            match = _match_label(label, values)
            if match is None:
                return None
            mapping[label] = match
        if len(set(mapping.values())) != len(labels):
            return None  # two labels collapsing to one value is not a match
        return mapping

    for column in adapter.string_columns(measure_table):
        mapping = try_column(measure_table, column)
        if mapping:
            return DimensionBinding(
                dimension_key=dimension_key,
                group_column=column,
                group_table=measure_table,
                label_to_value=mapping,
            )

    for ref_table, local_col, ref_col in adapter.joined_tables(measure_table):
        for column in adapter.string_columns(ref_table):
            if column == ref_col:
                continue
            mapping = try_column(ref_table, column)
            if mapping:
                return DimensionBinding(
                    dimension_key=dimension_key,
                    group_column=column,
                    group_table=ref_table,
                    join_from=f"{measure_table}.{local_col}",
                    join_to=f"{ref_table}.{ref_col}",
                    label_to_value=mapping,
                )
    return None


# --- constraint scoring ---------------------------------------------------------


def score_candidate(
    candidate: ScoredCandidate, group: MetricGroup, ctx: FingerprintContext
) -> CandidateEvaluation:
    """Assemble the full constraint set and record each constraint's outcome
    in the evidence bundle."""
    expr = candidate.expr
    executor = ctx.executor
    constraints: list[ConstraintResult] = []
    bindings: list[DimensionBinding] = []

    grand_reported = group.grand_total.raw_value
    computed = executor.evaluate(expr, group.as_of)
    ok, err = executor.matches(computed, grand_reported)
    constraints.append(
        ConstraintResult(
            kind=ConstraintKind.GRAND_TOTAL,
            instance_id=group.grand_total.instance_id,
            description=f"grand total @ {group.as_of}",
            reported=grand_reported,
            computed=computed,
            satisfied=ok,
            relative_error=err,
        )
    )
    grand_total_ok = ok

    # breakdowns, grouped per dimension key
    by_dim: dict[str, list[ReportMetricInstance]] = {}
    for inst in group.breakdowns:
        for key in inst.dimension_context:
            by_dim.setdefault(key, []).append(inst)

    for dim_key, rows in sorted(by_dim.items()):
        labels = [r.dimension_context[dim_key] for r in rows]
        binding = resolve_dimension(
            dim_key, labels, expr.lhs.table, ctx.adapter, group.as_of
        )
        grouped = executor.evaluate(expr, group.as_of, binding) if binding else None
        if binding:
            bindings.append(binding)
        for row in rows:
            label = row.dimension_context[dim_key]
            row_computed = None
            if isinstance(grouped, dict):
                row_computed = grouped.get(binding.label_to_value.get(label, ""))
            ok, err = executor.matches(row_computed, row.raw_value)
            constraints.append(
                ConstraintResult(
                    kind=ConstraintKind.BREAKDOWN,
                    instance_id=row.instance_id,
                    description=f"{dim_key}={label} @ {row.as_of_date}",
                    reported=row.raw_value,
                    computed=row_computed,
                    satisfied=ok,
                    relative_error=err,
                )
            )

    for prior in group.priors:
        try:
            prior_computed = executor.evaluate(expr, prior.as_of_date)
        except SnapshotNotFoundError as exc:
            # loud skip: the report references a period the warehouse
            # cannot reproduce — surface it, don't fail the candidate
            logger.error("prior-period constraint skipped: %s", exc)
            continue
        ok, err = executor.matches(prior_computed, prior.raw_value)
        constraints.append(
            ConstraintResult(
                kind=ConstraintKind.PRIOR_PERIOD,
                instance_id=prior.instance_id,
                description=f"prior period @ {prior.as_of_date}",
                reported=prior.raw_value,
                computed=prior_computed,
                satisfied=ok,
                relative_error=err,
            )
        )

    satisfied = sum(1 for c in constraints if c.satisfied)
    total = len(constraints)
    band = _band(satisfied, total, grand_total_ok)
    return CandidateEvaluation(
        expr=expr,
        band=band,
        constraints=constraints,
        dimension_bindings=bindings,
        satisfied=satisfied,
        total=total,
    )


def _band(satisfied: int, total: int, grand_total_ok: bool) -> ConfidenceBand:
    """Confidence bands per spec: CONFIRMED (>=4 constraints, >=90%
    satisfied), PROBABLE (>=2, >=75%), WEAK (grand total is the only
    available constraint), else REJECTED."""
    if not grand_total_ok:
        return ConfidenceBand.REJECTED
    if total == 1:
        return ConfidenceBand.WEAK
    ratio = satisfied / total
    if satisfied >= 4 and ratio >= 0.90:
        return ConfidenceBand.CONFIRMED
    if satisfied >= 2 and ratio >= 0.75:
        return ConfidenceBand.PROBABLE
    return ConfidenceBand.REJECTED


_BAND_RANK = {
    ConfidenceBand.CONFIRMED: 4,
    ConfidenceBand.PROBABLE: 3,
    ConfidenceBand.WEAK: 2,
    ConfidenceBand.REJECTED: 1,
    ConfidenceBand.UNMAPPABLE: 0,
}


def _passes_grand_total(
    candidate: ScoredCandidate, group: MetricGroup, ctx: FingerprintContext
) -> tuple[bool, NearMiss | None]:
    computed = ctx.executor.evaluate(candidate.expr, group.as_of)
    ok, err = ctx.executor.matches(computed, group.grand_total.raw_value)
    near = NearMiss(candidate.expr, err) if (err is not None and not ok) else None
    return ok, near


def resolve_metric(group: MetricGroup, ctx: FingerprintContext) -> ResolvedMapping:
    """Run tiers 1 -> 2 -> 3, score survivors on the full constraint set,
    and pick the best mapping — or declare the metric UNMAPPABLE."""
    tiers_run: list[str] = []
    survivors: list[ScoredCandidate] = []
    best_near_miss: NearMiss | None = None
    near_miss_tables: set[str] = set()

    def keep_near(miss: NearMiss | None) -> None:
        nonlocal best_near_miss
        if miss is None:
            return
        if best_near_miss is None or miss.relative_error < best_near_miss.relative_error:
            best_near_miss = miss

    # Tier 1 — always
    tiers_run.append("tier1")
    for cand in tier1_candidates(
        group.metric_name, ctx.adapter, ctx.tableau_evidence, ctx.glossary, ctx.config
    ):
        ok, miss = _passes_grand_total(cand, group, ctx)
        keep_near(miss)
        if ok:
            survivors.append(cand)

    # Tier 2 — only when Tier 1 produced zero survivors
    if not survivors:
        tiers_run.append("tier2")
        for cand in tier2_candidates(ctx.adapter, ctx.config):
            ok, miss = _passes_grand_total(cand, group, ctx)
            if ok:
                survivors.append(cand)
            elif miss is not None:
                keep_near(miss)
                if miss.relative_error <= ctx.config.near_miss_window:
                    near_miss_tables.add(cand.expr.lhs.table)

    # Tier 3 — only when unresolved AND structure hints or locality exist
    hints = [
        h for h in ctx.hypotheses
        if h.metric_name.lower() == group.metric_name.lower()
    ]
    scope = ctx.resolved_tables | near_miss_tables
    if not survivors and (hints or ctx.tableau_evidence or scope):
        tiers_run.append("tier3")
        for cand in tier3_candidates(
            group.metric_name, hints, ctx.tableau_evidence, ctx.adapter,
            ctx.glossary, ctx.config, scope,
        ):
            ok, miss = _passes_grand_total(cand, group, ctx)
            keep_near(miss)
            if ok:
                survivors.append(cand)

    evaluations = [score_candidate(c, group, ctx) for c in survivors]
    sims = {c.expr.canonical_key(): c.name_sim for c in survivors}
    corroborations = {
        c.expr.canonical_key(): c.corroboration for c in survivors if c.corroboration
    }

    def sort_key(ev: CandidateEvaluation):
        complexity = (ev.expr.op is not None) + (ev.expr.predicate is not None)
        return (
            -_BAND_RANK[ev.band],
            -ev.score,
            complexity,
            -sims.get(ev.expr.canonical_key(), 0.0),
            ev.expr.canonical_key(),
        )

    evaluations.sort(key=sort_key)
    accepted = [e for e in evaluations if _BAND_RANK[e.band] >= _BAND_RANK[ConfidenceBand.WEAK]]
    rejected = [e for e in evaluations if e.band == ConfidenceBand.REJECTED]

    if not accepted:
        near_eval = None
        if best_near_miss is not None:
            near_eval = CandidateEvaluation(
                expr=best_near_miss.expr,
                band=ConfidenceBand.REJECTED,
                constraints=[
                    ConstraintResult(
                        kind=ConstraintKind.GRAND_TOTAL,
                        instance_id=group.grand_total.instance_id,
                        description=f"grand total @ {group.as_of} (best near-miss)",
                        reported=group.grand_total.raw_value,
                        satisfied=False,
                        relative_error=best_near_miss.relative_error,
                    )
                ],
                satisfied=0,
                total=1,
            )
        elif rejected:
            near_eval = rejected[0]
        return ResolvedMapping(
            report_id=group.grand_total.report_id,
            metric_name=group.metric_name,
            grand_total_instance_id=group.grand_total.instance_id,
            band=ConfidenceBand.UNMAPPABLE,
            best=None,
            rejected=rejected,
            near_miss=near_eval,
            tiers_run=tiers_run,
            prose_formula_hint=group.grand_total.prose_formula_hint,
        )

    best = accepted[0]
    ctx.resolved_tables.add(best.expr.lhs.table)
    lines = []
    if (line := corroborations.get(best.expr.canonical_key())) is not None:
        lines.append(line)
    if group.grand_total.prose_formula_hint:
        lines.append(f"report commentary: \"{group.grand_total.prose_formula_hint}\"")
    return ResolvedMapping(
        report_id=group.grand_total.report_id,
        metric_name=group.metric_name,
        grand_total_instance_id=group.grand_total.instance_id,
        band=best.band,
        best=best,
        rejected=rejected,
        near_miss=rejected[0] if rejected else None,
        tiers_run=tiers_run,
        corroborations=lines,
        prose_formula_hint=group.grand_total.prose_formula_hint,
    )


def resolve_all(
    instances: list[ReportMetricInstance], ctx: FingerprintContext
) -> list[ResolvedMapping]:
    return [resolve_metric(group, ctx) for group in group_instances(instances)]
