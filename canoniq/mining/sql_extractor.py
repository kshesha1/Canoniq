"""SQL extractor — the core mining logic.

Takes a `RawQuery` that passed signal classification and extracts structured
candidates (aggregations, dimensions, joins) using sqlglot's AST.

Key implementation constraint: every column name emitted by this module MUST
exist in the `schemas` dict passed in. This is the primary anti-hallucination
guardrail — if a column can't be resolved against the real warehouse schema,
the candidate is dropped and a warning is logged, never passed downstream to
the LLM proposer.
"""

import hashlib
import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.qualify import qualify

from canoniq.ingest.base import ColumnSchema, RawQuery, TableSchema

logger = logging.getLogger(__name__)


@dataclass
class AggregationCandidate:
    """A candidate metric extracted from a SQL aggregation expression."""

    expression: str                # e.g. "SUM(o.total_amount)"
    source_table: str              # resolved table name
    source_column: str             # resolved column name
    agg_function: str              # SUM | COUNT | AVG | MIN | MAX | COUNT_DISTINCT
    filter_expr: str | None        # e.g. "status = 'completed'" if WHERE applied
    seen_in_queries: list[str]     # query hashes where this appeared
    execution_count: int           # total executions across all queries
    distinct_users: int            # from the originating RawQuery
    last_seen_at: str              # from the originating RawQuery


@dataclass
class DimensionCandidate:
    """A candidate dimension from GROUP BY and WHERE columns."""

    column: str
    table: str
    is_time: bool                  # True if column type is time/date
    seen_in_queries: list[str] = field(default_factory=list)


@dataclass
class JoinCandidate:
    """A candidate join relationship extracted from JOIN clauses."""

    from_table: str
    to_table: str
    from_column: str
    to_column: str
    join_type: str                 # LEFT | INNER | etc.
    seen_in_queries: list[str] = field(default_factory=list)


def _query_id(query: RawQuery) -> str:
    """A short, stable id for a RawQuery, used to track which query
    contributed a given piece of evidence."""
    return hashlib.sha256(query.sql.encode()).hexdigest()[:16]


def _build_schema_lookup(schemas: dict[str, TableSchema]) -> dict[str, TableSchema]:
    """Normalize the caller's schemas dict to {simple_table_name: TableSchema},
    tolerating both plain and fully-qualified (db.schema.table) keys."""
    lookup: dict[str, TableSchema] = {}
    for key, table_schema in schemas.items():
        simple_name = key.split(".")[-1].lower()
        lookup[simple_name] = table_schema
    return lookup


def _lookup_column(table_schema: TableSchema, column_name: str) -> ColumnSchema | None:
    for col in table_schema.columns:
        if col.name.lower() == column_name.lower():
            return col
    return None


def _build_sqlglot_schema(schema_lookup: dict[str, TableSchema]) -> dict[str, object]:
    """Build the nested {table: {column: type}} dict sqlglot's qualifier needs
    to resolve aliases and expand `SELECT *`. Column types are placeholders —
    qualify only needs to know a column exists, not its warehouse type."""
    return {
        table_name: {col.name: "TEXT" for col in table_schema.columns}
        for table_name, table_schema in schema_lookup.items()
    }


def _build_alias_map(tree: exp.Expr) -> dict[str, str]:
    """Map every table alias (or bare table name) in the query to its real,
    physical table name."""
    alias_map: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        alias_map[table.alias_or_name] = table.name
    return alias_map


def _resolve_column(
    column: exp.Column,
    alias_map: dict[str, str],
    schema_lookup: dict[str, TableSchema],
) -> tuple[str, ColumnSchema] | None:
    """Resolve a qualified sqlglot Column node to (real_table_name, ColumnSchema),
    or None if it can't be validated against the known schema."""
    table_alias = column.table
    if not table_alias:
        return None

    real_table = alias_map.get(table_alias, table_alias).lower()
    table_schema = schema_lookup.get(real_table)
    if table_schema is None:
        return None

    col_schema = _lookup_column(table_schema, column.name)
    if col_schema is None:
        return None

    return real_table, col_schema


def _owning_filter_expr(node: exp.Expr, dialect: str) -> str | None:
    """Return the WHERE clause text of the nearest enclosing SELECT, if any."""
    select = node.find_ancestor(exp.Select)
    if select is None:
        return None
    where = select.args.get("where")
    if where is None:
        return None
    return where.this.sql(dialect=dialect)


def _extract_aggregations(
    tree: exp.Expr,
    alias_map: dict[str, str],
    schema_lookup: dict[str, TableSchema],
    query: RawQuery,
    dialect: str,
) -> list[AggregationCandidate]:
    candidates = []
    query_id = _query_id(query)

    for agg in tree.find_all(exp.AggFunc):
        is_count_distinct = isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct)
        has_star = any(agg.find_all(exp.Star))
        columns = list(agg.find_all(exp.Column))

        if has_star:
            source_table = None
            for table_alias in alias_map:
                source_table = alias_map[table_alias].lower()
                break
            if source_table is None or source_table not in schema_lookup:
                logger.warning("Skipping COUNT(*) with no resolvable table: %s", agg.sql())
                continue
            source_column = "*"
        elif columns:
            resolved = [_resolve_column(c, alias_map, schema_lookup) for c in columns]
            if any(r is None for r in resolved):
                logger.warning(
                    "Skipping aggregation with unresolvable column(s): %s", agg.sql()
                )
                continue
            source_table, col_schema = resolved[0]  # type: ignore[misc]
            source_column = col_schema.name
        else:
            logger.warning("Skipping aggregation with no column reference: %s", agg.sql())
            continue

        agg_function = "COUNT_DISTINCT" if is_count_distinct else type(agg).__name__.upper()

        candidates.append(
            AggregationCandidate(
                expression=agg.sql(dialect=dialect),
                source_table=source_table,
                source_column=source_column,
                agg_function=agg_function,
                filter_expr=_owning_filter_expr(agg, dialect),
                seen_in_queries=[query_id],
                execution_count=query.execution_count,
                distinct_users=query.distinct_users,
                last_seen_at=query.last_executed_at,
            )
        )

    return candidates


def _extract_dimensions(
    tree: exp.Expr,
    alias_map: dict[str, str],
    schema_lookup: dict[str, TableSchema],
    query: RawQuery,
) -> list[DimensionCandidate]:
    query_id = _query_id(query)
    seen: dict[tuple[str, str], DimensionCandidate] = {}

    candidate_columns: list[exp.Column] = []
    for group in tree.find_all(exp.Group):
        candidate_columns.extend(group.find_all(exp.Column))
    for where in tree.find_all(exp.Where):
        candidate_columns.extend(where.find_all(exp.Column))

    for column in candidate_columns:
        resolved = _resolve_column(column, alias_map, schema_lookup)
        if resolved is None:
            logger.warning("Skipping dimension with unresolvable column: %s", column.sql())
            continue
        table_name, col_schema = resolved
        key = (table_name, col_schema.name)
        if key not in seen:
            seen[key] = DimensionCandidate(
                column=col_schema.name,
                table=table_name,
                is_time=(col_schema.data_type == "time"),
                seen_in_queries=[query_id],
            )

    return list(seen.values())


def _extract_joins(
    tree: exp.Expr,
    alias_map: dict[str, str],
    schema_lookup: dict[str, TableSchema],
    query: RawQuery,
) -> list[JoinCandidate]:
    query_id = _query_id(query)
    candidates = []

    for join in tree.find_all(exp.Join):
        on_expr = join.args.get("on")
        if on_expr is None or not isinstance(join.this, exp.Table):
            continue

        introduced_table = alias_map.get(join.this.alias_or_name, join.this.name).lower()
        join_type = " ".join(filter(None, [join.side, join.kind])) or "INNER"

        for eq in on_expr.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue

            left_resolved = _resolve_column(left, alias_map, schema_lookup)
            right_resolved = _resolve_column(right, alias_map, schema_lookup)
            if left_resolved is None or right_resolved is None:
                logger.warning("Skipping join condition with unresolvable column: %s", eq.sql())
                continue

            left_table, left_col = left_resolved
            right_table, right_col = right_resolved

            if right_table == introduced_table:
                from_table, from_col = left_table, left_col
                to_table, to_col = right_table, right_col
            else:
                from_table, from_col = right_table, right_col
                to_table, to_col = left_table, left_col

            candidates.append(
                JoinCandidate(
                    from_table=from_table,
                    to_table=to_table,
                    from_column=from_col.name,
                    to_column=to_col.name,
                    join_type=join_type,
                    seen_in_queries=[query_id],
                )
            )

    return candidates


def extract_candidates(
    query: RawQuery,
    schemas: dict[str, TableSchema],
    dialect: str = "duckdb",
) -> tuple[list[AggregationCandidate], list[DimensionCandidate], list[JoinCandidate]]:
    """Parse a single SQL query and extract all candidates."""
    try:
        tree = sqlglot.parse_one(query.sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception as e:
        logger.warning("Skipping unparseable query in extractor: %s (%s)", query.sql, e)
        return [], [], []

    schema_lookup = _build_schema_lookup(schemas)
    sqlglot_schema = _build_sqlglot_schema(schema_lookup)

    try:
        tree = qualify(
            tree,
            dialect=dialect,
            schema=sqlglot_schema,
            validate_qualify_columns=False,
            identify=False,
        )
    except Exception as e:
        logger.warning("Skipping query that failed qualification: %s (%s)", query.sql, e)
        return [], [], []

    alias_map = _build_alias_map(tree)

    agg_candidates = _extract_aggregations(tree, alias_map, schema_lookup, query, dialect)
    dim_candidates = _extract_dimensions(tree, alias_map, schema_lookup, query)
    join_candidates = _extract_joins(tree, alias_map, schema_lookup, query)

    return agg_candidates, dim_candidates, join_candidates
