"""Conflict report (Module F): one markdown artifact + a JSON twin.

Four sections:
  1. Confirmed mappings — with constraint counts and corroborating sources
  2. Contradictions — evidence that disagrees, surfaced verbatim with
     source, date and trust for each side. Contradictions between
     high-trust sources are NEVER auto-resolved.
  3. Unmappable — escalate to steward: a work queue, not an error log
  4. Drift register — undocumented findings first
"""

import json
from dataclasses import dataclass

from canoniq.extract.prose import mine_formula_hypotheses
from canoniq.fingerprint.naming import similarity
from canoniq.models import (
    ConfidenceBand,
    ConstraintKind,
    DriftFinding,
    ResolvedMapping,
    TableauEvidence,
)
from canoniq.ranking.ontorank import SOURCE_AUTHORITY

_ACCEPTED_BANDS = (ConfidenceBand.CONFIRMED, ConfidenceBand.PROBABLE, ConfidenceBand.WEAK)


@dataclass
class EvidenceStatement:
    """One source's claim about how a metric is defined."""

    metric_name: str
    definition: str                    # verbatim snippet
    source: str                        # file / artifact name
    source_date: str | None
    source_type: str                   # key into SOURCE_AUTHORITY
    structure: str | None = None       # "A - B" | "SUM(A)" | raw SQL shape

    @property
    def trust(self) -> float:
        return SOURCE_AUTHORITY.get(self.source_type, 0.0)


@dataclass
class Contradiction:
    metric_name: str
    sides: list[EvidenceStatement]
    empirical_note: str | None = None  # what fingerprinting actually proved


def statements_from_document(
    doc_name: str, text: str, source_type: str, source_date: str | None
) -> list[EvidenceStatement]:
    """Mine definition statements from a policy/BRD document. Structure is
    inferred by running the prose formula miner over each statement."""
    from canoniq.extract.prose import mine_metric_statements

    out = []
    for metric_name, sentence in mine_metric_statements(text):
        hyps = mine_formula_hypotheses(sentence)
        structure = hyps[0].structure if hyps else None
        out.append(
            EvidenceStatement(
                metric_name=metric_name,
                definition=sentence,
                source=doc_name,
                source_date=source_date,
                source_type=source_type,
                structure=structure,
            )
        )
    return out


def statements_from_tableau(evidence: list[TableauEvidence]) -> list[EvidenceStatement]:
    out = []
    for ev in evidence:
        structure = "A - B" if " - " in ev.physical_expr_sql else (
            "A + B" if " + " in ev.physical_expr_sql else (
                "A / B" if " / " in ev.physical_expr_sql else "SUM(A)"
            )
        )
        out.append(
            EvidenceStatement(
                metric_name=ev.caption,
                definition=ev.physical_expr_sql,
                source=ev.source_file,
                source_date=None,
                source_type="tableau_calc",
                structure=structure,
            )
        )
    return out


def detect_contradictions(
    statements: list[EvidenceStatement],
    mappings: dict[str, ResolvedMapping] | None = None,
) -> list[Contradiction]:
    """Group statements by (fuzzy) metric name; different structural claims
    within a group are a contradiction. High-trust vs high-trust conflicts
    are exactly the ones that must surface."""
    mappings = mappings or {}

    def same_metric(a: str, b: str) -> bool:
        a_low, b_low = a.lower(), b.lower()
        # suffix match handles section headings bleeding into mined names
        # ("Metric Definitions Total Credit Risk Exposure")
        return (
            a_low == b_low
            or a_low.endswith(b_low)
            or b_low.endswith(a_low)
            or similarity(a, b) >= 0.85
        )

    groups: list[tuple[str, list[EvidenceStatement]]] = []
    for stmt in statements:
        for i, (name, members) in enumerate(groups):
            if same_metric(stmt.metric_name, name):
                members.append(stmt)
                if len(stmt.metric_name) < len(name):
                    groups[i] = (stmt.metric_name, members)  # prefer the cleaner name
                break
        else:
            groups.append((stmt.metric_name, [stmt]))

    out = []
    for name, members in groups:
        structures = {m.structure for m in members if m.structure}
        if len(structures) <= 1:
            continue
        empirical = None
        for metric_name, mapping in mappings.items():
            if mapping.best is not None and (
                metric_name.lower() == name.lower()
                or similarity(metric_name, name) >= 0.85
            ):
                empirical = (
                    f"fingerprinting reproduced the published figures with "
                    f"{mapping.best.expr.canonical_key()} "
                    f"({mapping.best.satisfied}/{mapping.best.total} constraints, "
                    f"{mapping.band})"
                )
                break
        out.append(
            Contradiction(
                metric_name=name,
                sides=sorted(members, key=lambda s: -s.trust),
                empirical_note=empirical,
            )
        )
    return out


# --- report assembly ----------------------------------------------------------


def _constraint_summary(mapping: ResolvedMapping) -> str:
    best = mapping.best
    if best is None:
        return "-"
    dims: dict[str, int] = {}
    priors = 0
    for c in best.constraints:
        if c.kind == ConstraintKind.BREAKDOWN and c.satisfied:
            dim = c.description.split("=")[0]
            dims[dim] = dims.get(dim, 0) + 1
        elif c.kind == ConstraintKind.PRIOR_PERIOD and c.satisfied:
            priors += 1
    parts = [f"{best.satisfied}/{best.total} figures reproduced"]
    extras = [f"{n} {dim} breakdowns" for dim, n in sorted(dims.items())]
    if priors:
        extras.append(f"{priors} prior-period figure{'s' if priors > 1 else ''}")
    if extras:
        parts.append("incl. " + ", ".join(extras))
    return ", ".join(parts)


def _near_miss_summary(mapping: ResolvedMapping) -> str:
    near = mapping.near_miss
    if near is None:
        return "no candidate came close"
    errs = [c.relative_error for c in near.constraints if c.relative_error is not None]
    if errs:
        return (
            f"best near-miss: {near.expr.canonical_key()} "
            f"(off by {min(errs):.2%} on the grand total)"
        )
    return f"best near-miss: {near.expr.canonical_key()} ({near.satisfied}/{near.total})"


def _mapping_row(mapping: ResolvedMapping) -> dict:
    return {
        "report_id": mapping.report_id,
        "metric": mapping.metric_name,
        "expression": mapping.best.expr.canonical_key() if mapping.best else None,
        "band": mapping.band.value,
        "constraints": _constraint_summary(mapping),
        "corroborations": mapping.corroborations,
        "dimension_bindings": [
            b.model_dump() for b in (mapping.best.dimension_bindings if mapping.best else [])
        ],
        "constraint_detail": [
            {
                "kind": c.kind.value,
                "description": c.description,
                "reported": str(c.reported),
                "computed": str(c.computed) if c.computed is not None else None,
                "satisfied": c.satisfied,
            }
            for c in (mapping.best.constraints if mapping.best else [])
        ],
    }


def build_conflict_report(
    mappings: list[ResolvedMapping],
    contradictions: list[Contradiction],
    drift_findings: list[DriftFinding],
    title: str = "Canoniq Conflict Report",
) -> tuple[str, dict]:
    """Returns (markdown, json_twin)."""
    accepted = [m for m in mappings if m.band in _ACCEPTED_BANDS]
    unmappable = [m for m in mappings if m.band == ConfidenceBand.UNMAPPABLE]

    lines = [f"# {title}", ""]

    lines += ["## 1. Confirmed mappings", ""]
    if accepted:
        lines += [
            "| Report | Metric | Physical expression | Band | Constraints | Corroboration |",
            "|---|---|---|---|---|---|",
        ]
        for m in accepted:
            corr = "<br>".join(m.corroborations) if m.corroborations else "—"
            lines.append(
                f"| {m.report_id} | {m.metric_name} | "
                f"`{m.best.expr.canonical_key()}` | {m.band.value} | "
                f"{_constraint_summary(m)} | {corr} |"
            )
    else:
        lines.append("_No mappings reached WEAK or better._")
    lines.append("")

    lines += ["## 2. Contradictions", ""]
    if contradictions:
        lines.append(
            "_Evidence below disagrees. Contradictions between high-trust "
            "sources are surfaced, never auto-resolved._"
        )
        lines.append("")
        for c in contradictions:
            lines.append(f"### {c.metric_name}")
            for side in c.sides:
                dated = f", {side.source_date}" if side.source_date else ""
                lines.append(
                    f"- **{side.source}**{dated} (trust {side.trust:.2f}, "
                    f"{side.source_type}): “{side.definition}”"
                )
            if c.empirical_note:
                lines.append(f"- **Empirical:** {c.empirical_note}")
            lines.append("")
    else:
        lines += ["_No contradictions detected._", ""]

    lines += ["## 3. Unmappable — escalate to steward", ""]
    if unmappable:
        lines.append(
            "_These metrics could not be reproduced from the warehouse. "
            "They are a steward work queue, not errors: the system does "
            "not force low-confidence mappings._"
        )
        lines.append("")
        for m in unmappable:
            lines.append(f"### {m.metric_name} ({m.report_id})")
            lines.append(f"- tiers run: {', '.join(m.tiers_run)}")
            lines.append(f"- {_near_miss_summary(m)}")
            if m.prose_formula_hint:
                lines.append(f"- report commentary: “{m.prose_formula_hint}”")
            lines.append("- **action:** confirm the source system with the metric owner")
            lines.append("")
    else:
        lines += ["_Every report metric was mapped._", ""]

    lines += ["## 4. Drift register", ""]
    if drift_findings:
        lines += [
            "| Kind | Metric | Undocumented | Detail |",
            "|---|---|---|---|",
        ]
        for f in drift_findings:
            name = f.new_name or f.old_name or ""
            if f.kind.value == "renamed":
                name = f"{f.old_name} → {f.new_name}"
            flag = "**YES**" if f.undocumented else "no"
            lines.append(f"| {f.kind.value} | {name} | {flag} | {f.detail} |")
    else:
        lines.append("_No drift detected (or only one report edition ingested)._")
    lines.append("")

    json_twin = {
        "title": title,
        "confirmed_mappings": [_mapping_row(m) for m in accepted],
        "contradictions": [
            {
                "metric": c.metric_name,
                "empirical": c.empirical_note,
                "sides": [
                    {
                        "source": s.source,
                        "source_date": s.source_date,
                        "source_type": s.source_type,
                        "trust": s.trust,
                        "definition": s.definition,
                        "structure": s.structure,
                    }
                    for s in c.sides
                ],
            }
            for c in contradictions
        ],
        "unmappable": [
            {
                "report_id": m.report_id,
                "metric": m.metric_name,
                "tiers_run": m.tiers_run,
                "near_miss": _near_miss_summary(m),
                "prose_formula_hint": m.prose_formula_hint,
            }
            for m in unmappable
        ],
        "drift_register": [f.model_dump(mode="json") for f in drift_findings],
    }
    return "\n".join(lines), json_twin


def write_conflict_report(
    out_dir, mappings, contradictions, drift_findings, stem: str = "conflict_report"
) -> tuple[str, str]:
    """Write markdown + JSON twins; returns their paths."""
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown, twin = build_conflict_report(mappings, contradictions, drift_findings)
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    md_path.write_text(markdown)
    json_path.write_text(json.dumps(twin, indent=2, default=str))
    return str(md_path), str(json_path)
