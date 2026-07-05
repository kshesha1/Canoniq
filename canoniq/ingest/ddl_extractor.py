"""
DDL extractor — parses CREATE TABLE statements and infers semantic roles
from column naming conventions, data types, and declared constraints.

No LLM involved. This is the deterministic foundation layer that runs
even when no query logs or documents are available.
"""

import logging
from pathlib import Path

import sqlglot
from sqlglot import exp

from canoniq.ingest.base import DDLColumnCandidate, DDLTableEvidence
from canoniq.ingest.warehouse import normalize_type  # reuse existing helper

logger = logging.getLogger(__name__)

# -- naming convention patterns ----------------------------------------------
_MEASURE_SUFFIXES = (
    "_amt", "_amount", "_price", "_cost", "_revenue", "_sales",
    "_profit", "_fee", "_balance", "_total", "_sum", "_value",
    "_qty", "_quantity", "_count", "_num",
)
_TIME_SUFFIXES = (
    "_dt", "_date", "_ts", "_timestamp", "_at", "_time",
    "_year", "_month", "_day", "_week",
)
_ID_SUFFIXES = (
    "_sk", "_id", "_key", "_fk", "_pk", "_code", "_no", "_num",
)
_FLAG_SUFFIXES = (
    "_flag", "_ind", "_indicator", "_yn", "_bool",
)
_FLAG_PREFIXES = ("is_", "has_", "can_", "was_", "did_")


def _infer_role(col_name: str, normalized_type: str) -> tuple[str, str]:
    """
    Return (role, reason) from column name and type.

    Roles: measure_input | dimension | identifier | flag | unknown
    """
    lower = col_name.lower()

    # Type-driven: booleans are always flags
    if normalized_type == "boolean":
        return "flag", "data type boolean → flag"

    # Suffix-driven: check most specific first
    for suf in _ID_SUFFIXES:
        if lower.endswith(suf):
            return "identifier", f"suffix '{suf}' → identifier"

    for suf in _FLAG_SUFFIXES:
        if lower.endswith(suf):
            return "flag", f"suffix '{suf}' → flag"

    for pfx in _FLAG_PREFIXES:
        if lower.startswith(pfx):
            return "flag", f"prefix '{pfx}' → flag"

    for suf in _MEASURE_SUFFIXES:
        if lower.endswith(suf) and normalized_type == "number":
            return "measure_input", f"suffix '{suf}' + numeric type → measure_input"

    for suf in _TIME_SUFFIXES:
        if lower.endswith(suf) or normalized_type == "time":
            return "dimension", f"suffix '{suf}' or time type → time dimension"

    # Type-driven fallback
    if normalized_type == "number":
        return "measure_input", "numeric type → measure_input (no suffix match)"
    if normalized_type == "string":
        return "dimension", "string type → categorical dimension (no suffix match)"

    return "unknown", "no matching rule"


def _extract_check_constraints(create_table: exp.Create) -> list[str]:
    """Extract raw SQL expressions from all CHECK(...) constraints."""
    checks = []
    for node in create_table.find_all(exp.CheckColumnConstraint):
        checks.append(str(node.this))
    return checks


def _extract_inline_comment(col_def: exp.ColumnDef) -> str | None:
    """Extract COMMENT 'text' attached to a column definition if present."""
    for constraint in col_def.find_all(exp.CommentColumnConstraint):
        return str(constraint.this).strip("'\"")
    return None


def _extract_default_value(col_def: exp.ColumnDef) -> str | None:
    """Extract DEFAULT <value> attached to a column definition if present."""
    for constraint in col_def.find_all(exp.DefaultColumnConstraint):
        return str(constraint.this) if constraint.this is not None else None
    return None


def _extract_pk_columns(create_table: exp.Create) -> list[str]:
    """Extract primary key column names from constraint or inline PK."""
    pk_cols: list[str] = []

    # Inline: col_name TYPE PRIMARY KEY
    for constraint in create_table.find_all(exp.PrimaryKeyColumnConstraint):
        col_def = constraint.find_ancestor(exp.ColumnDef)
        if col_def and col_def.name not in pk_cols:
            pk_cols.append(col_def.name)

    # Table-level: PRIMARY KEY (col1, col2) -- expressions are Identifiers
    for pk in create_table.find_all(exp.PrimaryKey):
        for ident in pk.expressions:
            name = getattr(ident, "name", None)
            if name and name not in pk_cols:
                pk_cols.append(name)

    return pk_cols


def _extract_fk_pairs(
    create_table: exp.Create,
) -> list[tuple[str, str, str]]:
    """Extract (local_col, ref_table, ref_col) from FOREIGN KEY constraints."""
    pairs = []
    for fk in create_table.find_all(exp.ForeignKey):
        local_cols = [ident.name for ident in fk.expressions]

        reference = fk.args.get("reference")
        if not reference:
            continue

        # `reference.this` is a Schema wrapping the referenced Table and the
        # referenced column Identifiers (not a bare Table, and the columns
        # are Identifiers, not Column nodes).
        ref_schema = reference.this
        if ref_schema is None:
            continue
        ref_table_expr = ref_schema.find(exp.Table)
        ref_table = ref_table_expr.name if ref_table_expr else None
        ref_cols = [ident.name for ident in ref_schema.expressions]

        if ref_table and local_cols and ref_cols:
            for lc, rc in zip(local_cols, ref_cols, strict=False):
                pairs.append((lc, ref_table, rc))
    return pairs


def parse_ddl_file(path: str, dialect: str | None = None) -> list[DDLTableEvidence]:
    """
    Parse a .sql DDL file and return one DDLTableEvidence per CREATE TABLE found.
    Silently skips non-CREATE-TABLE statements (views, indexes, comments, etc.).
    """
    text = Path(path).read_text()
    evidences = []

    statements = sqlglot.parse(text, dialect=dialect)
    for stmt in statements:
        if stmt is None or not isinstance(stmt, exp.Create):
            continue
        if stmt.kind and stmt.kind.upper() != "TABLE":
            continue

        table_expr = stmt.find(exp.Table)
        if not table_expr:
            continue
        table_name = table_expr.name

        col_candidates = []
        for col_def in stmt.find_all(exp.ColumnDef):
            raw_type = col_def.args.get("kind")
            raw_type_str = str(raw_type) if raw_type else "VARCHAR"
            normalized = normalize_type(raw_type_str)

            not_null = bool(list(col_def.find_all(exp.NotNullColumnConstraint)))

            role, reason = _infer_role(col_def.name, normalized)
            inline_comment = _extract_inline_comment(col_def)

            col_candidates.append(
                DDLColumnCandidate(
                    name=col_def.name,
                    raw_type=raw_type_str,
                    normalized_type=normalized,
                    is_nullable=not not_null,
                    inferred_role=role,
                    inference_reason=reason,
                    default_value=_extract_default_value(col_def),
                    inline_comment=inline_comment,
                )
            )

        evidences.append(
            DDLTableEvidence(
                table_name=table_name,
                column_candidates=col_candidates,
                pk_columns=_extract_pk_columns(stmt),
                fk_pairs=_extract_fk_pairs(stmt),
                check_constraints=_extract_check_constraints(stmt),
                inline_comments={
                    c.name: c.inline_comment
                    for c in col_candidates
                    if c.inline_comment
                },
                source_file=path,
            )
        )
        logger.info("DDL parsed: %s (%d columns)", table_name, len(col_candidates))

    return evidences
