"""Cheap noise gate — runs BEFORE the expensive sqlglot extraction."""

from enum import Enum

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ErrorLevel


class SignalClass(Enum):
    ANALYTICAL = "analytical"      # has aggregations -> worth processing
    STRUCTURAL = "structural"      # DDL/DML -> skip
    NOISE = "noise"                # no meaningful signal


def classify_sql(sql: str, dialect: str = "duckdb") -> SignalClass:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return SignalClass.NOISE

    # Must be a SELECT
    if not isinstance(tree, exp.Select):
        return SignalClass.STRUCTURAL

    # Must have at least one aggregation function
    agg_funcs = list(tree.find_all(exp.AggFunc))
    if not agg_funcs:
        return SignalClass.NOISE

    return SignalClass.ANALYTICAL
