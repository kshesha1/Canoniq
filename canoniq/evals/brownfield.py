"""Scores a bootstrap run against benchmark/brownfield/gold_labels.yaml.

Produces the scorecard `canoniq benchmark` prints: extraction recall,
mapping precision/recall by confidence band, trap outcomes, drift
findings, wall-clock time.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from canoniq.models import ConfidenceBand, DriftKind
from canoniq.pipeline import BootstrapResult

_ACCEPT_BANDS = {ConfidenceBand.CONFIRMED, ConfidenceBand.PROBABLE}


@dataclass
class TrapOutcome:
    name: str
    expression: str
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.actual == self.expected


@dataclass
class MetricOutcome:
    report_id: str
    metric: str
    gold_expression: str
    resolved_expression: str | None
    band: str
    correct: bool


@dataclass
class Scorecard:
    extraction_recall: dict[str, float] = field(default_factory=dict)
    metric_outcomes: list[MetricOutcome] = field(default_factory=list)
    band_precision: dict[str, float] = field(default_factory=dict)
    band_counts: dict[str, int] = field(default_factory=dict)
    mapping_recall: float = 0.0
    unmappable_accuracy: float = 0.0
    traps: list[TrapOutcome] = field(default_factory=list)
    drift_expected: int = 0
    drift_found: int = 0
    elapsed_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            all(r >= 0.95 for r in self.extraction_recall.values())
            and self.mapping_recall == 1.0
            and self.unmappable_accuracy == 1.0
            and all(t.ok for t in self.traps)
            and self.drift_found == self.drift_expected
        )

    def to_dict(self) -> dict:
        return {
            "extraction_recall": self.extraction_recall,
            "mapping_recall": self.mapping_recall,
            "band_precision": self.band_precision,
            "band_counts": self.band_counts,
            "unmappable_accuracy": self.unmappable_accuracy,
            "metric_outcomes": [vars(m) for m in self.metric_outcomes],
            "traps": [
                {**vars(t), "ok": t.ok} for t in self.traps
            ],
            "drift": {"expected": self.drift_expected, "found": self.drift_found},
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "passed": self.passed,
        }


def _extraction_recall(gold_report: dict, extraction) -> float:
    extracted = {
        (
            i.metric_name_verbatim,
            tuple(sorted(i.dimension_context.items())),
            i.as_of_date.isoformat(),
        ): i.raw_value
        for i in extraction.instances
    }
    gold_instances = [
        (m["name"], inst) for m in gold_report["metrics"] for inst in m["instances"]
    ]
    hits = 0
    for name, inst in gold_instances:
        key = (name, tuple(sorted(inst["dims"].items())), inst["as_of"])
        raw = extracted.get(key)
        gold_raw = Decimal(inst["raw"])
        if raw is not None and abs(raw - gold_raw) <= Decimal("0.01") * abs(gold_raw):
            hits += 1
    return hits / len(gold_instances) if gold_instances else 0.0


def score_benchmark(gold: dict, result: BootstrapResult) -> Scorecard:
    card = Scorecard(elapsed_seconds=result.elapsed_seconds)

    mappable_total = 0
    mappable_correct = 0
    unmappable_total = 0
    unmappable_correct = 0
    band_hits: dict[str, list[bool]] = {}

    for report_id, gold_report in gold["reports"].items():
        extraction = result.extractions.get(report_id)
        mappings = result.mappings_by_report.get(report_id, {})
        if extraction is not None:
            card.extraction_recall[report_id] = _extraction_recall(gold_report, extraction)

        for gold_metric in gold_report["metrics"]:
            mapping = mappings.get(gold_metric["name"])
            band = mapping.band if mapping else ConfidenceBand.UNMAPPABLE
            resolved = (
                mapping.best.expr.canonical_key()
                if mapping and mapping.best
                else None
            )
            if gold_metric["expression"] == "unmappable":
                unmappable_total += 1
                correct = band == ConfidenceBand.UNMAPPABLE
                unmappable_correct += correct
            else:
                mappable_total += 1
                correct = (
                    band in _ACCEPT_BANDS and resolved == gold_metric["expression"]
                )
                mappable_correct += correct
                if band in _ACCEPT_BANDS:
                    band_hits.setdefault(band.value, []).append(
                        resolved == gold_metric["expression"]
                    )
            card.metric_outcomes.append(
                MetricOutcome(
                    report_id=report_id,
                    metric=gold_metric["name"],
                    gold_expression=gold_metric["expression"],
                    resolved_expression=resolved,
                    band=band.value,
                    correct=correct,
                )
            )

    card.mapping_recall = mappable_correct / mappable_total if mappable_total else 0.0
    card.unmappable_accuracy = (
        unmappable_correct / unmappable_total if unmappable_total else 1.0
    )
    card.band_precision = {
        band: sum(hits) / len(hits) for band, hits in band_hits.items()
    }
    card.band_counts = {band: len(hits) for band, hits in band_hits.items()}

    # traps: the planted wrong candidates must have been evaluated and REJECTED
    for trap_name, trap in gold["traps"].items():
        actual = "not evaluated"
        for mappings in result.mappings_by_report.values():
            mapping = mappings.get(trap["metric"])
            if mapping is None:
                continue
            if mapping.best and mapping.best.expr.canonical_key() == trap["expression"]:
                actual = mapping.band.value  # the trap won — very bad
            elif any(
                r.expr.canonical_key() == trap["expression"] for r in mapping.rejected
            ):
                actual = "REJECTED"
        card.traps.append(
            TrapOutcome(
                name=trap_name,
                expression=trap["expression"],
                expected=trap["expected_band"],
                actual=actual,
            )
        )

    expected_drift = gold.get("drift", [])
    card.drift_expected = len(expected_drift)
    found = 0
    for expectation in expected_drift:
        for finding in result.drift_findings:
            if (
                expectation["kind"] == "renamed"
                and finding.kind == DriftKind.RENAMED
                and finding.old_name == expectation["old_name"]
                and finding.new_name == expectation["new_name"]
            ) or (
                expectation["kind"] == "redefined"
                and finding.kind == DriftKind.REDEFINED
                and finding.old_name == expectation["name"]
            ):
                found += 1
                break
    card.drift_found = found
    return card
