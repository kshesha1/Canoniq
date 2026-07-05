"""Tableau `.twb` extractor (Module C).

`.twb` is XML — pure lxml, no LLM. Extracts datasource connections,
calculated fields (translated to normalized SQL via sqlglot), and
worksheet shelf usage (which columns act as dimensions vs measures in
practice).

Tableau calculated fields are near-executable ground truth: their trust
prior (`tableau_calc`, 0.85) sits just below steward-confirmed sources.
Malformed formulas are logged and skipped — never a crash.
"""

import logging
import re
from pathlib import Path

import sqlglot
from lxml import etree

from canoniq.models import TableauEvidence

logger = logging.getLogger(__name__)

_COLUMN_REF_RE = re.compile(r"\[([^\]]+)\]")

# Tableau aggregate -> SQL. Anything not listed here (table calcs like
# WINDOW_SUM, LOD expressions, etc.) is out of scope: log and skip.
_AGG_MAP = {
    "SUM": "SUM",
    "AVG": "AVG",
    "MIN": "MIN",
    "MAX": "MAX",
    "COUNT": "COUNT",
    "COUNTD": "COUNT(DISTINCT {})",
}

_UNSUPPORTED_FN_RE = re.compile(
    r"\b(WINDOW_\w+|LOOKUP|RUNNING_\w+|FIXED|INCLUDE|EXCLUDE|FIRST|LAST|INDEX|RANK)\b",
    re.IGNORECASE,
)


class TableauFormulaError(ValueError):
    pass


def translate_formula(formula: str) -> tuple[str, list[str]]:
    """Translate a Tableau calculated-field formula into normalized SQL.

    Returns (normalized_sql, referenced_columns).
    Raises TableauFormulaError for anything outside simple aggregate
    arithmetic — the caller logs and skips.
    """
    if _UNSUPPORTED_FN_RE.search(formula):
        raise TableauFormulaError(f"unsupported Tableau function in: {formula!r}")

    referenced = _COLUMN_REF_RE.findall(formula)
    if not referenced:
        raise TableauFormulaError(f"no column references in: {formula!r}")

    sql = re.sub(
        r"\bCOUNTD\s*\(\s*\[([^\]]+)\]\s*\)",
        lambda m: _AGG_MAP["COUNTD"].format(m.group(1)),
        formula,
        flags=re.IGNORECASE,
    )
    sql = _COLUMN_REF_RE.sub(lambda m: m.group(1), sql)

    try:
        parsed = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError as exc:
        raise TableauFormulaError(f"unparseable after translation: {sql!r}") from exc
    return parsed.sql(), referenced


def _worksheet_roles(root: etree._Element) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(field name -> worksheets using it, field name -> shelf role)."""
    usage: dict[str, list[str]] = {}
    roles: dict[str, str] = {}
    for worksheet in root.iter("worksheet"):
        ws_name = worksheet.get("name", "")
        for dep_col in worksheet.iter("column"):
            name = (dep_col.get("name") or "").strip("[]")
            if not name:
                continue
            usage.setdefault(name, []).append(ws_name)
            role = dep_col.get("role")
            if role:
                roles[name] = role
    return usage, roles


def extract_from_twb(path: str) -> list[TableauEvidence]:
    """Extract TableauEvidence for every parseable calculated field."""
    try:
        root = etree.parse(path).getroot()
    except (etree.XMLSyntaxError, OSError) as exc:
        logger.warning("cannot parse Tableau workbook %s: %s", path, exc)
        return []

    # datasource relations -> physical table references
    physical_tables: list[str] = []
    for relation in root.iter("relation"):
        if relation.get("type") == "table" and relation.get("name"):
            physical_tables.append(relation.get("name"))

    usage, roles = _worksheet_roles(root)

    evidence: list[TableauEvidence] = []
    for column in root.iter("column"):
        calc = column.find("calculation")
        if calc is None or not calc.get("formula"):
            continue
        caption = column.get("caption") or (column.get("name") or "").strip("[]")
        formula = calc.get("formula")
        try:
            sql, referenced = translate_formula(formula)
        except TableauFormulaError as exc:
            logger.warning("skipping calculated field %r: %s", caption, exc)
            continue

        field_name = (column.get("name") or "").strip("[]")
        field_worksheets = set(usage.get(field_name, []))
        # shelf usage: every column sharing a worksheet with this field
        # tells us how it is used in practice (dimension vs measure)
        role_hints = {
            col: role
            for col, role in roles.items()
            if col in referenced
            or col == field_name
            or field_worksheets & set(usage.get(col, []))
        }
        evidence.append(
            TableauEvidence(
                source_file=str(Path(path)),
                caption=caption,
                physical_expr_sql=sql,
                referenced_columns=referenced,
                worksheet_names=usage.get(field_name, []),
                role_hints=role_hints,
            )
        )
    if physical_tables:
        logger.info(
            "twb %s: %d physical tables, %d calculated fields extracted",
            path, len(physical_tables), len(evidence),
        )
    return evidence
