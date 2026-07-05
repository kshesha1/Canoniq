"""Tiers 1-3 of candidate generation, each strictly bounded.

Tier 1 — verification of name-based candidates (evidence captions and
         high-similarity column names, top-k by similarity x trust).
Tier 2 — value-space search: single-column aggregates compared to the
         grand total, independent of names. This is the channel that finds
         cryptic mappings with zero lexical overlap.
Tier 3 — bounded composition search, only when structure hints exist:
         prose-seeded, Tableau-seeded, then locality-bounded blind search.

Expression grammar (enforced by CandidateExpr's validator):
    expr := AGG(col) | AGG(col) OP AGG(col) | AGG(col) WHERE simple_predicate
Nothing deeper.  # V2: multi-hop joins, nested arithmetic.
"""

import itertools
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter
from canoniq.fingerprint.naming import resolve_term, similarity
from canoniq.models import (
    CandidateExpr,
    FormulaHypothesis,
    SimplePredicate,
    TableauEvidence,
    Term,
)

logger = logging.getLogger(__name__)

TIER1_MIN_EVIDENCE_SIMILARITY = 0.4
# Version-suffix noise is stripped during expansion (see naming.py), so
# deprecated twins score as high as their successors and enter this
# shortlist — where the constraint solver, not the name, decides.
TIER1_MIN_COLUMN_SIMILARITY = 0.5
TIER3_MIN_TABLEAU_SIMILARITY = 0.3

_SQL_EXPR_RE = re.compile(
    r"^(?P<agg1>SUM|COUNT|AVG)\((?P<col1>\w+)\)"
    r"(?:\s*(?P<op>[-+/])\s*(?P<agg2>SUM|COUNT|AVG)\((?P<col2>\w+)\))?$"
)

_STRUCTURE_OPS = {"A - B": "-", "A + B": "+", "A / B": "/"}


@dataclass
class ScoredCandidate:
    expr: CandidateExpr
    name_sim: float = 0.0
    corroboration: str | None = None   # human-readable provenance line


@dataclass
class NearMiss:
    expr: CandidateExpr
    relative_error: Decimal


@dataclass
class Tier2Result:
    survivors: list[ScoredCandidate] = field(default_factory=list)
    near_miss_tables: set[str] = field(default_factory=set)
    best_near_miss: NearMiss | None = None


def _tables_for_columns(
    adapter: IcebergCatalogAdapter, columns: list[str]
) -> str | None:
    """The single table containing ALL of `columns`, or None."""
    matches = [
        table
        for table in adapter.table_names()
        if all(col in adapter.columns(table) for col in columns)
    ]
    return matches[0] if len(matches) == 1 else None


def parse_sql_expr(sql: str, adapter: IcebergCatalogAdapter) -> CandidateExpr | None:
    """Parse an evidence SQL expression (e.g. a translated Tableau formula)
    into the candidate grammar, attributing columns to catalog tables."""
    m = _SQL_EXPR_RE.match(sql.strip())
    if not m:
        return None
    columns = [m.group("col1")] + ([m.group("col2")] if m.group("col2") else [])
    table = _tables_for_columns(adapter, columns)
    if table is None:
        return None
    lhs = Term(agg=m.group("agg1"), table=table, column=m.group("col1"))
    if m.group("op"):
        rhs = Term(agg=m.group("agg2"), table=table, column=m.group("col2"))
        return CandidateExpr(lhs=lhs, op=m.group("op"), rhs=rhs)
    return CandidateExpr(lhs=lhs)


# --- Tier 1 -------------------------------------------------------------------


def tier1_candidates(
    metric_name: str,
    adapter: IcebergCatalogAdapter,
    tableau_evidence: list[TableauEvidence],
    glossary: dict[tuple[str, str], str],
    config: FingerprintConfig,
) -> list[ScoredCandidate]:
    """Name-based shortlist: evidence-backed expressions whose captions
    resemble the metric name, plus columns with genuinely high name
    similarity. Cheap; runs first, always."""
    out: list[ScoredCandidate] = []

    for ev in tableau_evidence:
        sim = similarity(metric_name, ev.caption)
        if sim < TIER1_MIN_EVIDENCE_SIMILARITY:
            continue
        expr = parse_sql_expr(ev.physical_expr_sql, adapter)
        if expr is None:
            continue
        expr.provenance = "tier1_tableau"
        out.append(
            ScoredCandidate(
                expr=expr,
                name_sim=sim,
                corroboration=(
                    f"tableau: {ev.source_file} / \"{ev.caption}\" = "
                    f"{ev.physical_expr_sql}"
                ),
            )
        )

    for table, col in adapter.numeric_columns()[: config.max_columns]:
        sim = similarity(metric_name, f"{table}.{col}", glossary.get((table, col), ""))
        if sim >= TIER1_MIN_COLUMN_SIMILARITY:
            expr = CandidateExpr(
                lhs=Term(agg="SUM", table=table, column=col), provenance="tier1_name"
            )
            out.append(ScoredCandidate(expr=expr, name_sim=sim))

    out.sort(key=lambda c: -c.name_sim)
    return _dedupe(out)[: config.tier1_top_k]


# --- Tier 2 -------------------------------------------------------------------


def tier2_candidates(
    adapter: IcebergCatalogAdapter, config: FingerprintConfig
) -> list[ScoredCandidate]:
    """Full scan of single-column hypotheses — name-independent. Bounded by
    max_columns so it stays sane on real catalogs."""
    out = []
    numeric = adapter.numeric_columns()
    if len(numeric) > config.max_columns:
        logger.warning(
            "tier2: %d numeric columns exceeds max_columns=%d — truncating",
            len(numeric), config.max_columns,
        )
        numeric = numeric[: config.max_columns]
    for table, col in numeric:
        out.append(
            ScoredCandidate(
                CandidateExpr(
                    lhs=Term(agg="SUM", table=table, column=col), provenance="tier2"
                )
            )
        )
    for table in adapter.table_names():
        out.append(
            ScoredCandidate(
                CandidateExpr(
                    lhs=Term(agg="COUNT", table=table, column="*"), provenance="tier2"
                )
            )
        )
    return out


# --- Tier 3 -------------------------------------------------------------------


def tier3_candidates(
    metric_name: str,
    hypotheses: list[FormulaHypothesis],
    tableau_evidence: list[TableauEvidence],
    adapter: IcebergCatalogAdapter,
    glossary: dict[tuple[str, str], str],
    config: FingerprintConfig,
    scope_tables: set[str],
) -> list[ScoredCandidate]:
    """Bounded composition search. Hypothesis generators in priority order:
    prose-seeded, Tableau-seeded, then locality-bounded blind search over
    `scope_tables` (tables already holding a resolved metric, near-miss
    tables, or one FK-hop away)."""
    out: list[ScoredCandidate] = []
    numeric = adapter.numeric_columns()

    # 1. prose-formula seeded: resolve each term independently — each term
    #    is an easier sub-problem than the whole metric.
    for hyp in hypotheses:
        if similarity(metric_name, hyp.metric_name) < 0.8 and (
            hyp.metric_name.lower() != metric_name.lower()
        ):
            continue
        term_candidates = [
            resolve_term(desc, numeric, glossary) for desc in hyp.term_descriptions
        ]
        if not all(term_candidates):
            continue
        corroboration = f"prose: \"{hyp.verbatim}\" ({hyp.source_locator})"
        if hyp.structure == "SUM(A)":
            for table, col, sim in term_candidates[0]:
                expr = CandidateExpr(
                    lhs=Term(agg="SUM", table=table, column=col),
                    provenance="tier3_prose",
                )
                out.append(ScoredCandidate(expr, name_sim=sim, corroboration=corroboration))
        else:
            op = _STRUCTURE_OPS[hyp.structure]
            for (t1, c1, s1), (t2, c2, s2) in itertools.product(*term_candidates[:2]):
                if (t1, c1) == (t2, c2):
                    continue
                expr = CandidateExpr(
                    lhs=Term(agg="SUM", table=t1, column=c1),
                    op=op,
                    rhs=Term(agg="SUM", table=t2, column=c2),
                    provenance="tier3_prose",
                )
                out.append(
                    ScoredCandidate(expr, name_sim=(s1 + s2) / 2, corroboration=corroboration)
                )

    # 2. Tableau seeded (looser similarity than Tier 1)
    for ev in tableau_evidence:
        sim = similarity(metric_name, ev.caption)
        if sim < TIER3_MIN_TABLEAU_SIMILARITY:
            continue
        expr = parse_sql_expr(ev.physical_expr_sql, adapter)
        if expr is None:
            continue
        expr.provenance = "tier3_tableau"
        out.append(
            ScoredCandidate(
                expr, name_sim=sim,
                corroboration=f"tableau: {ev.source_file} / \"{ev.caption}\"",
            )
        )

    # 3. locality-bounded blind search (last resort)
    blind: list[ScoredCandidate] = []
    scoped = set(scope_tables)
    for table in list(scoped):
        for other, _, _ in adapter.joined_tables(table):
            scoped.add(other)

    for table in sorted(scoped & set(adapter.table_names())):
        cols = [c for t, c in numeric if t == table]
        col_sims = {
            c: similarity(metric_name, f"{table}.{c}", glossary.get((table, c), ""))
            for c in cols
        }
        for c1, c2 in itertools.permutations(cols, 2):
            for op in ("-", "+", "/"):
                if op == "+" and c1 > c2:
                    continue  # A + B == B + A
                blind.append(
                    ScoredCandidate(
                        CandidateExpr(
                            lhs=Term(agg="SUM", table=table, column=c1),
                            op=op,
                            rhs=Term(agg="SUM", table=table, column=c2),
                            provenance="tier3_blind",
                        ),
                        name_sim=max(col_sims[c1], col_sims[c2]),
                    )
                )
        # filtered single aggregates on low-cardinality string columns
        for c in cols:
            for str_col in adapter.string_columns(table):
                values = adapter.distinct_values(
                    table, str_col, _any_as_of(adapter, table)
                )
                if not values or len(values) > config.max_filter_cardinality:
                    continue
                for value in sorted(values):
                    for op in ("=", "<>"):
                        blind.append(
                            ScoredCandidate(
                                CandidateExpr(
                                    lhs=Term(agg="SUM", table=table, column=c),
                                    predicate=SimplePredicate(
                                        column=str_col, op=op, value=value
                                    ),
                                    provenance="tier3_blind_filter",
                                ),
                                name_sim=col_sims[c],
                            )
                        )

    if len(blind) > config.max_tier3_hypotheses:
        logger.warning(
            "tier3 blind search for %r: %d hypotheses exceeds cap %d — "
            "truncating by name-similarity score",
            metric_name, len(blind), config.max_tier3_hypotheses,
        )
        blind.sort(key=lambda c: -c.name_sim)
        blind = blind[: config.max_tier3_hypotheses]

    return _dedupe(out + blind)


def _any_as_of(adapter: IcebergCatalogAdapter, table: str):
    """Latest business as-of available for a table (for distinct-value
    enumeration, which is not snapshot-sensitive for filter candidates)."""
    from datetime import date, datetime

    snaps = list(adapter.load(table).snapshots())
    best = None
    for snap in snaps:
        prop = snap.summary.additional_properties.get("canoniq.as_of")
        d = (
            date.fromisoformat(prop)
            if prop
            else datetime.fromtimestamp(snap.timestamp_ms / 1000).date()
        )
        if best is None or d > best:
            best = d
    return best


def _dedupe(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    seen: dict[str, ScoredCandidate] = {}
    for cand in candidates:
        key = cand.expr.canonical_key()
        if key not in seen:
            seen[key] = cand
        else:
            kept = seen[key]
            kept.name_sim = max(kept.name_sim, cand.name_sim)
            kept.corroboration = kept.corroboration or cand.corroboration
    return list(seen.values())
