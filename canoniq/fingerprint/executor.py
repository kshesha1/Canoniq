"""D0 — shared fingerprint execution machinery.

All evaluation runs in DuckDB over PyIceberg snapshot scans registered as
Arrow views; every query targets the snapshot matching the report's as-of
date (SnapshotNotFoundError propagates loudly otherwise).

Money is compared as Decimal throughout — no float tolerance arithmetic.
"""

import logging
from datetime import date
from decimal import Decimal

import duckdb

from canoniq.fingerprint import FingerprintConfig
from canoniq.fingerprint.catalog import IcebergCatalogAdapter
from canoniq.models import CandidateExpr, DimensionBinding, Term

logger = logging.getLogger(__name__)


def relative_error(computed: Decimal, reported: Decimal) -> Decimal:
    if reported == 0:
        return abs(computed)
    return abs(computed - reported) / abs(reported)


class SnapshotExecutor:
    def __init__(self, adapter: IcebergCatalogAdapter, config: FingerprintConfig | None = None):
        self.adapter = adapter
        self.config = config or FingerprintConfig()
        self.con = duckdb.connect()
        self._views: dict[tuple[str, date], str] = {}
        # the solver re-evaluates aggressively: cache on the full query shape
        self._cache: dict[tuple, Decimal | dict[str, Decimal] | None] = {}

    def matches(self, computed: Decimal | None, reported: Decimal) -> tuple[bool, Decimal | None]:
        if computed is None:
            return False, None
        err = relative_error(computed, reported)
        return err <= self.config.tolerance, err

    # -- view registration -------------------------------------------------

    def _view(self, table: str, as_of: date) -> str:
        key = (table, as_of)
        if key not in self._views:
            name = f"{table}__{as_of.strftime('%Y%m%d')}"
            self.con.register(name, self.adapter.arrow_for(table, as_of))
            self._views[key] = name
        return self._views[key]

    # -- evaluation ----------------------------------------------------------

    def _binding_clauses(
        self, term_table: str, as_of: date, binding: DimensionBinding | None
    ) -> tuple[str, str, str] | None:
        """(select_prefix, join_clause, group_clause) or None if the binding
        cannot apply to this term's table."""
        if binding is None:
            return "", "", ""
        if binding.join_from:
            join_table, join_col = binding.join_from.split(".")
            if join_table != term_table:
                return None
            ref_table, ref_col = binding.join_to.split(".")
            ref_view = self._view(ref_table, as_of)
            join = (
                f" JOIN {ref_view} r ON f.{join_col} = r.{ref_col}"
            )
            group_expr = f"r.{binding.group_column}"
        else:
            if binding.group_table != term_table:
                return None
            join = ""
            group_expr = f"f.{binding.group_column}"
        return f"{group_expr} AS grp, ", join, f" GROUP BY {group_expr}"

    def _evaluate_term(
        self,
        term: Term,
        as_of: date,
        predicate_sql: str,
        binding: DimensionBinding | None,
    ) -> Decimal | dict[str, Decimal] | None:
        clauses = self._binding_clauses(term.table, as_of, binding)
        if clauses is None:
            return None
        select_prefix, join, group = clauses

        view = self._view(term.table, as_of)
        agg_sql = f"{term.agg}(*)" if term.column == "*" else f"{term.agg}(f.{term.column})"
        where = f" WHERE {predicate_sql}" if predicate_sql else ""
        sql = f"SELECT {select_prefix}{agg_sql} FROM {view} f{join}{where}{group}"
        try:
            rows = self.con.sql(sql).fetchall()
        except duckdb.Error as exc:
            logger.debug("evaluation failed (%s): %s", sql, exc)
            return None

        if binding is None:
            value = rows[0][0] if rows else None
            return None if value is None else Decimal(str(value))
        return {
            str(grp): Decimal(str(value))
            for grp, value in rows
            if value is not None and grp is not None
        }

    def evaluate(
        self,
        expr: CandidateExpr,
        as_of: date,
        binding: DimensionBinding | None = None,
    ) -> Decimal | dict[str, Decimal] | None:
        """Evaluate a candidate expression at the snapshot matching `as_of`.

        Returns a Decimal (no binding) or {group value -> Decimal}. None
        when the expression is not evaluable in this shape (e.g. a grouped
        two-term candidate whose terms live in different tables).
        """
        binding_key = None
        if binding is not None:
            binding_key = (
                binding.group_table, binding.group_column,
                binding.join_from, binding.join_to,
            )
        cache_key = (expr.canonical_key(), as_of, binding_key)
        if cache_key in self._cache:
            return self._cache[cache_key]

        predicate_sql = ""
        if expr.predicate is not None:
            predicate_sql = f"f.{expr.predicate.sql()}"

        lhs = self._evaluate_term(expr.lhs, as_of, predicate_sql, binding)
        result: Decimal | dict[str, Decimal] | None
        if expr.op is None:
            result = lhs
        else:
            # grammar: the predicate never applies to two-term expressions
            rhs = self._evaluate_term(expr.rhs, as_of, "", binding)
            if lhs is None or rhs is None:
                result = None
            elif binding is None:
                result = self._combine(lhs, rhs, expr.op)
            else:
                if expr.lhs.table != expr.rhs.table:
                    # grouped cross-table arithmetic needs its own join
                    # path per term — beyond the v1 grammar.  # V2
                    result = None
                else:
                    result = {
                        key: combined
                        for key in set(lhs) & set(rhs)
                        if (combined := self._combine(lhs[key], rhs[key], expr.op))
                        is not None
                    }

        self._cache[cache_key] = result
        return result

    @staticmethod
    def _combine(a: Decimal, b: Decimal, op: str) -> Decimal | None:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if b == 0:
            return None
        return a / b
