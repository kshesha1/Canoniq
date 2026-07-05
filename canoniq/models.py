"""Central data models for report-first bootstrapping.

`ReportMetricInstance` is the anchor of the whole pipeline: every figure
printed in a board report is one instance, and everything downstream
(fingerprinting, drift, emitters, the conflict report) consumes or produces
mappings anchored to these instances.
"""

import hashlib
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Module B — report metric instances
# ---------------------------------------------------------------------------


class ReportMetricInstance(BaseModel):
    instance_id: str                   # deterministic hash
    report_id: str                     # e.g. "sor_2026q1"
    metric_name_verbatim: str          # exactly as printed
    value: Decimal
    unit: str                          # "USD_bn", "USD_mm", "count", "pct"
    scale_factor: Decimal              # e.g. 1e9 for figures printed in $bn
    as_of_date: date                   # report period end
    dimension_context: dict[str, str] = Field(default_factory=dict)
    # {"legal_entity": "Meridian NY", ...} — empty dict = grand total
    source_locator: str                # "page 7, table 3, row 2"
    prose_formula_hint: str | None = None  # extracted commentary logic, verbatim
    parent_total_id: str | None = None     # links breakdown rows to their total

    @staticmethod
    def make_instance_id(
        report_id: str,
        metric_name_verbatim: str,
        as_of_date: date,
        dimension_context: dict[str, str],
        value: Decimal,
    ) -> str:
        payload = "|".join(
            [
                report_id,
                metric_name_verbatim,
                as_of_date.isoformat(),
                ";".join(f"{k}={v}" for k, v in sorted(dimension_context.items())),
                str(value),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def raw_value(self) -> Decimal:
        """Value in base units (e.g. USD) after applying the print scale."""
        return self.value * self.scale_factor


class FormulaHypothesis(BaseModel):
    """Metric logic mined from report commentary prose. Seeds Tier-3
    fingerprinting: each term description is resolved independently."""

    metric_name: str
    structure: Literal["A - B", "A + B", "A / B", "SUM(A)"]
    term_descriptions: list[str]       # ["gross exposure", "collateral held"]
    source_locator: str = ""
    verbatim: str = ""                 # the sentence the hypothesis came from


# ---------------------------------------------------------------------------
# Module C — Tableau evidence
# ---------------------------------------------------------------------------


class TableauEvidence(BaseModel):
    source_file: str
    caption: str                       # calculated-field caption / name
    physical_expr_sql: str             # sqlglot-normalized SQL expression
    referenced_columns: list[str]
    worksheet_names: list[str] = Field(default_factory=list)
    role_hints: dict[str, str] = Field(default_factory=dict)
    # column -> "dimension" | "measure" from worksheet shelf usage


# ---------------------------------------------------------------------------
# Module D — fingerprint grammar, constraints, resolved mappings
# ---------------------------------------------------------------------------

AggFn = Literal["SUM", "COUNT", "AVG"]
BinOp = Literal["+", "-", "/"]
FilterOp = Literal["=", "<>"]


class Term(BaseModel):
    """AGG(table.column). column='*' only valid for COUNT."""

    agg: AggFn
    table: str
    column: str

    def sql(self) -> str:
        return f"{self.agg}({self.column})"

    def display(self) -> str:
        return f"{self.agg}({self.table}.{self.column})"


class SimplePredicate(BaseModel):
    column: str
    op: FilterOp
    value: str

    def sql(self) -> str:
        quoted = self.value.replace("'", "''")
        return f"{self.column} {self.op} '{quoted}'"


class CandidateExpr(BaseModel):
    """The full v1 expression grammar:

        expr := AGG(col) | AGG(col) OP AGG(col) | AGG(col) WHERE simple_predicate

    Anything deeper is out of scope.  # V2: nested arithmetic, multi-join.
    """

    lhs: Term
    op: BinOp | None = None
    rhs: Term | None = None
    predicate: SimplePredicate | None = None   # applies to lhs only
    provenance: str = "unknown"  # "tier1" | "tier2" | "tier3_prose" | ...

    @model_validator(mode="after")
    def _grammar(self) -> "CandidateExpr":
        if (self.op is None) != (self.rhs is None):
            raise ValueError("op and rhs must be provided together")
        if self.predicate is not None and self.op is not None:
            # grammar: filtered expressions are single-term only
            raise ValueError("WHERE predicate only allowed on single-term expressions")
        if self.lhs.column == "*" and self.lhs.agg != "COUNT":
            raise ValueError("'*' column only valid with COUNT")
        return self

    def display(self) -> str:
        s = self.display_unqualified()
        tables = sorted({t.table for t in self.terms()})
        return f"{s}  [{', '.join(tables)}]"

    def display_unqualified(self) -> str:
        s = self.lhs.sql()
        if self.op:
            s = f"{s} {self.op} {self.rhs.sql()}"
        if self.predicate:
            s = f"{s} WHERE {self.predicate.sql()}"
        return s

    def terms(self) -> list[Term]:
        return [self.lhs] + ([self.rhs] if self.rhs else [])

    def canonical_key(self) -> str:
        """Normalized identity for dedup / gold-label comparison."""
        parts = [f"{self.lhs.agg}({self.lhs.table}.{self.lhs.column})".upper()]
        if self.op:
            parts += [self.op, f"{self.rhs.agg}({self.rhs.table}.{self.rhs.column})".upper()]
        if self.predicate:
            parts += [
                "WHERE",
                f"{self.predicate.column}{self.predicate.op}'{self.predicate.value}'".upper(),
            ]
        return " ".join(parts)


class DimensionBinding(BaseModel):
    """How a report dimension key (e.g. 'legal_entity') binds to physical
    columns for one candidate mapping. A confirmed mapping resolves the
    measure AND its dimension joins simultaneously."""

    dimension_key: str                 # report-side key, e.g. "legal_entity"
    group_column: str                  # column whose values match report labels
    group_table: str                   # table holding group_column
    join_from: str | None = None       # "fact_table.col" when a join is needed
    join_to: str | None = None         # "ref_table.col"
    label_to_value: dict[str, str] = Field(default_factory=dict)


class ConstraintKind(StrEnum):
    GRAND_TOTAL = "grand_total"
    BREAKDOWN = "breakdown"
    PRIOR_PERIOD = "prior_period"


class ConstraintResult(BaseModel):
    kind: ConstraintKind
    instance_id: str
    description: str                   # "legal_entity=Meridian NY @ 2026-03-31"
    reported: Decimal                  # raw (scale-applied) reported value
    computed: Decimal | None = None    # None if not evaluable
    satisfied: bool = False
    relative_error: Decimal | None = None


class ConfidenceBand(StrEnum):
    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    WEAK = "WEAK"
    REJECTED = "REJECTED"
    UNMAPPABLE = "UNMAPPABLE"


class CandidateEvaluation(BaseModel):
    expr: CandidateExpr
    band: ConfidenceBand
    constraints: list[ConstraintResult] = Field(default_factory=list)
    dimension_bindings: list[DimensionBinding] = Field(default_factory=list)
    satisfied: int = 0
    total: int = 0

    @property
    def score(self) -> float:
        return self.satisfied / self.total if self.total else 0.0


class ResolvedMapping(BaseModel):
    """Final verdict for one report metric (a grand-total instance and all
    of its linked breakdown / prior-period instances)."""

    report_id: str
    metric_name: str
    grand_total_instance_id: str
    band: ConfidenceBand
    best: CandidateEvaluation | None = None    # None only when UNMAPPABLE
    rejected: list[CandidateEvaluation] = Field(default_factory=list)
    near_miss: CandidateEvaluation | None = None  # best rejected candidate
    tiers_run: list[str] = Field(default_factory=list)
    corroborations: list[str] = Field(default_factory=list)
    # human-readable provenance lines: "tableau: risk_dashboard.twb / Net Exposure"
    prose_formula_hint: str | None = None


# ---------------------------------------------------------------------------
# Module E — drift findings
# ---------------------------------------------------------------------------


class DriftKind(StrEnum):
    RENAMED = "renamed"
    REDEFINED = "redefined"
    APPEARED = "appeared"
    DISAPPEARED = "disappeared"


class DriftFinding(BaseModel):
    kind: DriftKind
    old_name: str | None = None
    new_name: str | None = None
    old_expression: str | None = None
    new_expression: str | None = None
    old_value: Decimal | None = None
    new_value: Decimal | None = None
    detail: str = ""
    undocumented: bool = True          # headline field: no doc explains the change
    documentation_hits: list[str] = Field(default_factory=list)
