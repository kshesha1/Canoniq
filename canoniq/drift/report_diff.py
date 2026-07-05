"""Semantic drift diff across report editions (Module E).

Given two sets of ReportMetricInstance + their resolved mappings, detect:
  renamed        — metric gone from edition A, but a B metric resolves to
                   the SAME physical expression
  redefined      — same printed name, but the resolved expressions differ,
                   OR the old expression evaluated at the new snapshot
                   diverges from the newly reported figure
  appeared / disappeared — set difference, net of renames

Every finding is checked against ingested documents (policy docs, BRDs):
if nothing mentions the change, `undocumented=True` — the headline field.
"""

import logging
from decimal import Decimal

from canoniq.fingerprint.executor import SnapshotExecutor, relative_error
from canoniq.fingerprint.solver import group_instances
from canoniq.models import (
    DriftFinding,
    DriftKind,
    ReportMetricInstance,
    ResolvedMapping,
)

logger = logging.getLogger(__name__)


def _expression(mapping: ResolvedMapping | None) -> str | None:
    if mapping is None or mapping.best is None:
        return None
    return mapping.best.expr.canonical_key()


def _documentation_hits(names: list[str], documents: dict[str, str]) -> list[str]:
    """Documents that mention any of the drifted metric names. A mention is
    only a proxy for an explanation, but an absent mention is definitive:
    nothing documents the change."""
    hits = []
    for doc_name, text in documents.items():
        lowered = text.lower()
        if any(name and name.lower() in lowered for name in names):
            hits.append(doc_name)
    return sorted(hits)


def diff_reports(
    old_instances: list[ReportMetricInstance],
    new_instances: list[ReportMetricInstance],
    old_mappings: dict[str, ResolvedMapping],
    new_mappings: dict[str, ResolvedMapping],
    executor: SnapshotExecutor | None = None,
    documents: dict[str, str] | None = None,
    tolerance: Decimal = Decimal("0.005"),
) -> list[DriftFinding]:
    documents = documents or {}
    old_groups = {g.metric_name: g for g in group_instances(old_instances)}
    new_groups = {g.metric_name: g for g in group_instances(new_instances)}
    findings: list[DriftFinding] = []

    def finalize(finding: DriftFinding, names: list[str]) -> None:
        finding.documentation_hits = _documentation_hits(names, documents)
        finding.undocumented = not finding.documentation_hits
        findings.append(finding)

    # 1. renames: disappeared name whose expression re-appears under a new name
    gone = set(old_groups) - set(new_groups)
    came = set(new_groups) - set(old_groups)
    renamed_old: set[str] = set()
    renamed_new: set[str] = set()
    for old_name in sorted(gone):
        old_expr = _expression(old_mappings.get(old_name))
        if old_expr is None:
            continue
        for new_name in sorted(came - renamed_new):
            if _expression(new_mappings.get(new_name)) == old_expr:
                renamed_old.add(old_name)
                renamed_new.add(new_name)
                finalize(
                    DriftFinding(
                        kind=DriftKind.RENAMED,
                        old_name=old_name,
                        new_name=new_name,
                        old_expression=old_expr,
                        new_expression=old_expr,
                        old_value=old_groups[old_name].grand_total.raw_value,
                        new_value=new_groups[new_name].grand_total.raw_value,
                        detail=(
                            f"'{old_name}' and '{new_name}' resolve to the same "
                            f"physical expression: {old_expr}"
                        ),
                    ),
                    [old_name, new_name],
                )
                break

    # 2. silent redefinitions among metrics present in both editions
    for name in sorted(set(old_groups) & set(new_groups)):
        old_expr = _expression(old_mappings.get(name))
        new_expr = _expression(new_mappings.get(name))
        old_total = old_groups[name].grand_total
        new_total = new_groups[name].grand_total

        redefined = False
        detail = ""
        if old_expr and new_expr and old_expr != new_expr:
            redefined = True
            detail = (
                f"resolved expression changed: {old_expr} -> {new_expr}"
            )
        elif old_expr and executor is not None:
            # same (or unresolved) expression: recompute the OLD definition
            # at the NEW snapshot and compare to the newly reported figure
            old_mapping = old_mappings[name]
            try:
                computed = executor.evaluate(
                    old_mapping.best.expr, new_total.as_of_date
                )
            except Exception as exc:
                logger.warning("drift divergence check failed for %r: %s", name, exc)
                computed = None
            if computed is not None:
                err = relative_error(computed, new_total.raw_value)
                if err > tolerance:
                    redefined = True
                    detail = (
                        f"old expression {old_expr} evaluated at "
                        f"{new_total.as_of_date} gives {computed:.2f}, but the "
                        f"report prints {new_total.raw_value:.2f} "
                        f"(divergence {err:.2%})"
                    )
        if redefined:
            finalize(
                DriftFinding(
                    kind=DriftKind.REDEFINED,
                    old_name=name,
                    new_name=name,
                    old_expression=old_expr,
                    new_expression=new_expr,
                    old_value=old_total.raw_value,
                    new_value=new_total.raw_value,
                    detail=detail,
                ),
                [name],
            )

    # 3. appeared / disappeared (net of renames)
    for name in sorted(came - renamed_new):
        finalize(
            DriftFinding(
                kind=DriftKind.APPEARED,
                new_name=name,
                new_expression=_expression(new_mappings.get(name)),
                new_value=new_groups[name].grand_total.raw_value,
                detail=f"'{name}' appears only in the newer edition",
            ),
            [name],
        )
    for name in sorted(gone - renamed_old):
        finalize(
            DriftFinding(
                kind=DriftKind.DISAPPEARED,
                old_name=name,
                old_expression=_expression(old_mappings.get(name)),
                old_value=old_groups[name].grand_total.raw_value,
                detail=f"'{name}' appears only in the older edition",
            ),
            [name],
        )

    # undocumented findings are the headline: they sort first
    findings.sort(key=lambda f: (not f.undocumented, f.kind, f.new_name or f.old_name or ""))
    return findings
