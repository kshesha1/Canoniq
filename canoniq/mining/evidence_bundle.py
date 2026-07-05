"""Evidence bundle — aggregates candidates across all queries into a
ranked bundle per table, ready for OntoRank + the LLM proposer."""

from collections import defaultdict
from dataclasses import dataclass

from canoniq.ingest.base import DDLTableEvidence, DocumentMetricCandidate, TableSchema
from canoniq.mining.sql_extractor import AggregationCandidate, DimensionCandidate, JoinCandidate

# A query that mined >= this many distinct aggregation shapes counts as
# "complex" for OntoRank's source_authority signal (query_log_complex vs
# query_log_simple); see ranking/ontorank.py.
COMPLEX_QUERY_AGG_THRESHOLD = 2

_CanonicalAggKey = tuple[str, str, str]  # (source_table, source_column, agg_function)


@dataclass
class MetricEvidence:
    """All evidence supporting a single metric candidate."""

    expression: str                # canonical SQL expression
    source_table: str
    execution_count: int           # total across all queries
    distinct_users: int
    last_seen_at: str
    source_types: list[str]        # ["query_log", "dbt", "looker"]
    is_certified: bool             # defined in dbt with passing tests
    filter_variants: list[str | None]  # different WHERE clauses; None = no filter


@dataclass
class EvidenceBundle:
    """Complete evidence for one table, ready for OntoRank + LLM."""

    table: TableSchema
    metric_candidates: list[MetricEvidence]
    dimension_candidates: list[DimensionCandidate]
    join_candidates: list[JoinCandidate]


def _simple_table_name(table: TableSchema) -> str:
    return table.fully_qualified_name.split(".")[-1]


def _canonical_expression(agg_function: str, source_column: str) -> str:
    """Alias-independent, whitespace-normalized display expression."""
    if source_column == "*":
        return f"{agg_function}(*)"
    if agg_function == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {source_column})"
    return f"{agg_function}({source_column})"


def _canonical_key(candidate: AggregationCandidate) -> _CanonicalAggKey:
    if not candidate.source_column and not candidate.agg_function:
        # Document-derived candidates (see the document_candidates merge
        # below) carry a complete expression with no discrete column/agg
        # breakdown -- key on the expression itself so distinct document
        # metrics don't all collapse into one "(table, '', '')" bucket.
        return (candidate.source_table.lower(), candidate.expression.lower(), "")
    return (
        candidate.source_table.lower(),
        candidate.source_column.lower(),
        candidate.agg_function,
    )


# AggregationCandidate has no dedicated field for non-query evidence
# sources (DDL, documents, Excel) -- their seen_in_queries entries never
# correspond to a real query hash. Rather than defaulting those to
# "query_log_simple" (silently mislabeling non-query evidence and zeroing
# out its real OntoRank authority), source-tagged entries are marked with
# this prefix and resolved directly to their real SourceType below.
_SOURCE_TYPE_MARKER_PREFIX = "source_type:"


def _source_type_marker(source_type: str) -> str:
    return f"{_SOURCE_TYPE_MARKER_PREFIX}{source_type}"


def _classify_query_complexity(agg_candidates: list[AggregationCandidate]) -> dict[str, str]:
    """Map query_id -> 'query_log_complex' | 'query_log_simple' based on how
    many distinct aggregation shapes were mined from that query."""
    keys_per_query: dict[str, set[_CanonicalAggKey]] = defaultdict(set)
    for candidate in agg_candidates:
        key = _canonical_key(candidate)
        for query_id in candidate.seen_in_queries:
            if query_id.startswith(_SOURCE_TYPE_MARKER_PREFIX):
                continue
            keys_per_query[query_id].add(key)

    return {
        query_id: (
            "query_log_complex"
            if len(keys) >= COMPLEX_QUERY_AGG_THRESHOLD
            else "query_log_simple"
        )
        for query_id, keys in keys_per_query.items()
    }


def _resolve_source_type(query_id: str, query_complexity: dict[str, str]) -> str:
    if query_id.startswith(_SOURCE_TYPE_MARKER_PREFIX):
        return query_id.removeprefix(_SOURCE_TYPE_MARKER_PREFIX)
    return query_complexity.get(query_id, "query_log_simple")


def _dbt_certifies(expression: str, table_name: str, dbt_metrics: list[dict] | None) -> bool:
    """Check whether `expression` on `table_name` is already defined (and
    presumably tested) as a dbt metric. dbt_metrics entries are dicts with
    at least an "expression" key and optionally a "table" key."""
    if not dbt_metrics:
        return False

    normalized_expr = expression.replace(" ", "").lower()
    for metric in dbt_metrics:
        metric_expr = str(metric.get("expression", "")).replace(" ", "").lower()
        metric_table = str(metric.get("table", "")).lower()
        if metric_expr == normalized_expr and (not metric_table or metric_table == table_name):
            return True
    return False


def _dedupe_preserve_order(values: list[str | None]) -> list[str | None]:
    seen: dict[str | None, None] = {}
    for v in values:
        seen.setdefault(v, None)
    return list(seen.keys())


def _ddl_column_source_type(col_name: str, ddl_evidence: DDLTableEvidence) -> str:
    """A column backed by an explicit PK/FK/CHECK constraint is more
    trustworthy than one inferred purely from naming convention."""
    is_key = col_name in ddl_evidence.pk_columns or col_name in {
        local_col for local_col, _, _ in ddl_evidence.fk_pairs
    }
    has_check = any(col_name in check for check in ddl_evidence.check_constraints)
    if is_key or has_check:
        return "ddl_constraint"
    return "ddl_naming_convention"


def _merge_ddl_evidence(
    table_name: str,
    ddl_evidence: DDLTableEvidence | None,
    agg_candidates: list[AggregationCandidate],
    dim_candidates: list[DimensionCandidate],
) -> None:
    """Convert DDLColumnCandidates into AggregationCandidates and
    DimensionCandidates that feed the same downstream ranking pipeline.
    Mutates the two (already-local-copy) lists in place."""
    if not ddl_evidence:
        return

    for col in ddl_evidence.column_candidates:
        source_type_marker = _source_type_marker(
            _ddl_column_source_type(col.name, ddl_evidence)
        )
        if col.inferred_role == "measure_input":
            # Propose SUM as the default aggregation for numeric columns.
            # Existing dedup-by-canonical-key logic downstream merges this
            # with any matching query-log-mined SUM(col) automatically.
            agg_candidates.append(
                AggregationCandidate(
                    expression=f"SUM({col.name})",
                    source_table=table_name,
                    source_column=col.name,
                    agg_function="SUM",
                    filter_expr=None,
                    seen_in_queries=[source_type_marker],
                    execution_count=0,  # no query log evidence
                    distinct_users=0,
                    last_seen_at="",
                )
            )
        elif col.inferred_role == "dimension":
            dim_candidates.append(
                DimensionCandidate(
                    column=col.name,
                    table=table_name,
                    is_time=(col.normalized_type == "time"),
                    seen_in_queries=[source_type_marker],
                )
            )


def _merge_document_candidates(
    table_name: str,
    document_candidates: list[DocumentMetricCandidate] | None,
    agg_candidates: list[AggregationCandidate],
) -> None:
    """Add grounded, non-ambiguous document candidates as AggregationCandidates
    using their already-resolved expression. Mutates `agg_candidates` (an
    already-local-copy list) in place."""
    if not document_candidates:
        return

    for doc_cand in document_candidates:
        if doc_cand.resolved_expression and not doc_cand.ambiguous:
            agg_candidates.append(
                AggregationCandidate(
                    expression=doc_cand.resolved_expression,
                    # build_evidence_bundle is already scoped to one table
                    # (the `table` parameter); the caller is responsible for
                    # only passing document candidates relevant to it.
                    source_table=table_name,
                    source_column="",   # expression-level, not column-level
                    agg_function="",    # already embedded in expression
                    filter_expr=doc_cand.raw_filter,
                    seen_in_queries=[_source_type_marker(doc_cand.source_type)],
                    execution_count=0,
                    distinct_users=0,
                    last_seen_at="",
                )
            )


def build_evidence_bundle(
    table: TableSchema,
    agg_candidates: list[AggregationCandidate],
    dim_candidates: list[DimensionCandidate],
    join_candidates: list[JoinCandidate],
    dbt_metrics: list[dict] | None = None,
    ddl_evidence: DDLTableEvidence | None = None,
    document_candidates: list[DocumentMetricCandidate] | None = None,
) -> EvidenceBundle:
    """
    Merge and deduplicate candidates by expression.
    Group identical expressions (modulo whitespace/alias) together.
    Sum execution_counts, union source_types, pick latest last_seen_at.
    Mark is_certified=True if expression appears in dbt_metrics.

    `ddl_evidence` and `document_candidates` are merged in as additional
    AggregationCandidate/DimensionCandidate evidence before the usual
    per-table filtering and grouping runs, so they flow through the exact
    same dedup/ranking pipeline as query-log-mined evidence. Local copies
    of the input lists are used so this function never mutates the
    caller's `agg_candidates`/`dim_candidates` (callers such as the CLI
    reuse the same mined lists across multiple tables).
    """
    table_name = _simple_table_name(table)

    agg_candidates = list(agg_candidates)
    dim_candidates = list(dim_candidates)
    _merge_ddl_evidence(table_name, ddl_evidence, agg_candidates, dim_candidates)
    _merge_document_candidates(table_name, document_candidates, agg_candidates)

    table_aggs = [c for c in agg_candidates if c.source_table.lower() == table_name.lower()]
    table_dims = [c for c in dim_candidates if c.table.lower() == table_name.lower()]
    table_joins = [
        c
        for c in join_candidates
        if c.from_table.lower() == table_name.lower() or c.to_table.lower() == table_name.lower()
    ]

    query_complexity = _classify_query_complexity(table_aggs)

    groups: dict[_CanonicalAggKey, list[AggregationCandidate]] = defaultdict(list)
    for candidate in table_aggs:
        groups[_canonical_key(candidate)].append(candidate)

    metric_candidates = []
    for (_, _, agg_function), members in groups.items():
        source_column = members[0].source_column
        if not source_column and not agg_function:
            # Document-derived candidate: expression is already complete
            # (see _merge_document_candidates), not reconstructible from a
            # column/agg pair.
            expression = members[0].expression
        else:
            expression = _canonical_expression(agg_function, source_column)

        source_types = {
            _resolve_source_type(query_id, query_complexity)
            for member in members
            for query_id in member.seen_in_queries
        }
        is_certified = _dbt_certifies(expression, table_name, dbt_metrics)
        if is_certified:
            source_types.add("dbt_metric")

        metric_candidates.append(
            MetricEvidence(
                expression=expression,
                source_table=table_name,
                execution_count=sum(m.execution_count for m in members),
                distinct_users=sum(m.distinct_users for m in members),
                last_seen_at=max(m.last_seen_at for m in members),
                source_types=sorted(source_types),
                is_certified=is_certified,
                filter_variants=_dedupe_preserve_order([m.filter_expr for m in members]),
            )
        )

    dedup_dims: dict[tuple[str, str], DimensionCandidate] = {}
    for dim in table_dims:
        dim_key = (dim.table, dim.column)
        if dim_key not in dedup_dims:
            dedup_dims[dim_key] = DimensionCandidate(
                column=dim.column,
                table=dim.table,
                is_time=dim.is_time,
                seen_in_queries=list(dim.seen_in_queries),
            )
        else:
            dedup_dims[dim_key].seen_in_queries.extend(dim.seen_in_queries)

    dedup_joins: dict[tuple[str, str, str, str], JoinCandidate] = {}
    for join in table_joins:
        join_key = (join.from_table, join.to_table, join.from_column, join.to_column)
        if join_key not in dedup_joins:
            dedup_joins[join_key] = JoinCandidate(
                from_table=join.from_table,
                to_table=join.to_table,
                from_column=join.from_column,
                to_column=join.to_column,
                join_type=join.join_type,
                seen_in_queries=list(join.seen_in_queries),
            )
        else:
            dedup_joins[join_key].seen_in_queries.extend(join.seen_in_queries)

    return EvidenceBundle(
        table=table,
        metric_candidates=metric_candidates,
        dimension_candidates=list(dedup_dims.values()),
        join_candidates=list(dedup_joins.values()),
    )
