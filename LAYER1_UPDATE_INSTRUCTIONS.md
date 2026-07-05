# Claude Code Instructions — Layer 1 Ingest Update
# Canoniq: Add DDL, Document (BRD/PDF/Word/Excel) and DDL-only mode

## Context

The existing Layer 1 ingest (`canoniq/ingest/`) has:
- `base.py`         — ColumnSchema, TableSchema, RawQuery, abstract Connector
- `warehouse.py`    — DuckDBWarehouseConnector (schema introspection)
- `query_log.py`    — QueryLogFileConnector (SQL file → RawQuery list)
- `dbt_manifest.py` — DbtManifestConnector (already built)
- `watcher.py`      — SignalWatcher (already built)

The original design assumed SQL query logs as the primary evidence source.
We are now adding three new first-class input types:
  1. Raw DDL files (.sql containing CREATE TABLE statements)
  2. Business documents: BRD, PDF reports, Word docs (.pdf, .docx, .txt)
  3. Excel reports and financial models (.xlsx)

SQL query logs remain supported but are now SUPPLEMENTARY — useful when
available, not required. The pipeline must produce meaningful semantic
YAML from DDL + documents alone.

Do NOT rewrite any existing files unless explicitly told to below.
Work additively. Existing connectors are correct and stay as-is.

---

## Step 1 — Extend base.py with new data models

ADD the following dataclasses to `canoniq/ingest/base.py`.
Do not remove or modify any existing dataclasses.
Add after the existing RawQuery dataclass.

```python
from enum import Enum

class SourceType(str, Enum):
    """All recognised evidence sources with their OntoRank authority tier."""
    # Structured sources — high trust floor
    DBT_METRIC         = "dbt_metric"          # authority: 1.00
    DBT_MODEL          = "dbt_model"            # authority: 0.85
    DATA_DICTIONARY    = "data_dictionary"      # authority: 0.85
    DDL_CONSTRAINT     = "ddl_constraint"       # authority: 0.75
    LOOKER_MEASURE     = "looker_measure"       # authority: 0.80
    TABLEAU_FIELD      = "tableau_field"        # authority: 0.78

    # Document sources — medium trust, boosted by approval signals
    BRD_APPROVED       = "brd_approved"         # authority: 0.90
    BRD_DRAFT          = "brd_draft"            # authority: 0.65
    EXCEL_NAMED        = "excel_named"          # authority: 0.70
    EXCEL_FORMULA      = "excel_formula"        # authority: 0.50
    PDF_REPORT         = "pdf_report"           # authority: 0.55
    CONFLUENCE_PAGE    = "confluence_page"      # authority: 0.60

    # Inferred sources — lower trust floor
    DDL_NAMING         = "ddl_naming_convention" # authority: 0.45
    QUERY_LOG_COMPLEX  = "query_log_complex"    # authority: 0.60
    QUERY_LOG_SIMPLE   = "query_log_simple"     # authority: 0.40
    AD_HOC             = "ad_hoc"               # authority: 0.20


@dataclass
class DDLTableEvidence:
    """
    Structured evidence extracted from a raw DDL CREATE TABLE statement.
    Produced by DDLExtractor. Feeds into EvidenceBundle alongside
    AggregationCandidate / DimensionCandidate from the SQL extractor.
    """
    table_name: str
    column_candidates: list["DDLColumnCandidate"]
    pk_columns: list[str]           # from CONSTRAINT PRIMARY KEY or inline PK
    fk_pairs: list[tuple[str, str, str]]  # (local_col, ref_table, ref_col)
    check_constraints: list[str]    # raw SQL expressions from CHECK(...)
    inline_comments: dict[str, str] # column_name → COMMENT text if present
    source_file: str                # path to the DDL file


@dataclass
class DDLColumnCandidate:
    """A single column extracted from DDL with semantic classification."""
    name: str
    raw_type: str                   # original SQL type string
    normalized_type: str            # string | number | time | boolean
    is_nullable: bool
    inferred_role: str              # "measure_input" | "dimension" | "identifier" | "flag" | "unknown"
    inference_reason: str           # human-readable: "suffix _amt → measure_input"
    default_value: str | None
    inline_comment: str | None


@dataclass
class DocumentMetricCandidate:
    """
    A metric or KPI definition extracted from an unstructured document
    (BRD, PDF, Word, Excel). The LLM extracts these; they are then
    grounded against the real schema before being trusted.
    """
    raw_name: str                   # exactly as written in the document
    raw_definition: str             # exactly as written: "sum of net sales minus returns"
    raw_filter: str | None          # any condition mentioned: "for completed orders"
    resolved_expression: str | None # SQL expression after schema grounding, or None
    resolved_table: str | None      # which table this resolves to, or None
    source_type: SourceType
    source_file: str
    source_page: int | None         # page number if PDF/Word
    source_section: str | None      # section heading if available
    has_approval_signal: bool       # True if doc contains sign-off / approved-by
    grounding_confidence: float     # 0.0–1.0: how well it mapped to real schema
    ambiguous: bool                 # True if LLM flagged it as unclear
```

Also ADD this abstract method to the existing `Connector` ABC:

```python
@abstractmethod
def get_ddl_evidence(self) -> list[DDLTableEvidence]:
    """
    Extract structured evidence from DDL.
    Connectors that don't have DDL should return [].
    """
    ...
```

---

## Step 2 — Create canoniq/ingest/ddl_extractor.py (NEW FILE)

Create a new file `canoniq/ingest/ddl_extractor.py`.

This extractor takes one or more DDL files (.sql with CREATE TABLE statements)
and produces DDLTableEvidence objects using sqlglot's DDL parsing.
No LLM is used — this is entirely deterministic.

```python
"""
DDL extractor — parses CREATE TABLE statements and infers semantic roles
from column naming conventions, data types, and declared constraints.

No LLM involved. This is the deterministic foundation layer that runs
even when no query logs or documents are available.
"""

import re
import logging
from pathlib import Path

import sqlglot
from sqlglot import exp

from canoniq.ingest.base import (
    DDLColumnCandidate,
    DDLTableEvidence,
    SourceType,
)
from canoniq.ingest.warehouse import normalize_type   # reuse existing helper

logger = logging.getLogger(__name__)

# ── naming convention patterns ──────────────────────────────────────────────
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
        return "flag", f"data type boolean → flag"

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
    for node in create_table.find_all(exp.Check):
        checks.append(str(node.this))
    return checks


def _extract_inline_comment(col_def: exp.ColumnDef) -> str | None:
    """Extract COMMENT 'text' attached to a column definition if present."""
    for prop in col_def.find_all(exp.SchemaCommentProperty):
        return str(prop.this).strip("'\"")
    return None


def _extract_pk_columns(create_table: exp.Create) -> list[str]:
    """Extract primary key column names from constraint or inline PK."""
    pk_cols = []
    for constraint in create_table.find_all(exp.PrimaryKeyColumnConstraint):
        # Inline: col_name TYPE PRIMARY KEY
        parent = constraint.parent
        if isinstance(parent, exp.ColumnDef):
            pk_cols.append(parent.name)

    for pk in create_table.find_all(exp.PrimaryKey):
        # Table-level: PRIMARY KEY (col1, col2)
        for col_expr in pk.find_all(exp.Column):
            if col_expr.name not in pk_cols:
                pk_cols.append(col_expr.name)

    return pk_cols


def _extract_fk_pairs(
    create_table: exp.Create,
) -> list[tuple[str, str, str]]:
    """Extract (local_col, ref_table, ref_col) from FOREIGN KEY constraints."""
    pairs = []
    for fk in create_table.find_all(exp.ForeignKey):
        local_cols = [c.name for c in fk.find_all(exp.Column)]
        ref = fk.args.get("reference")
        if not ref:
            continue
        ref_table = ref.this.name if ref.this else None
        ref_cols = [c.name for c in ref.find_all(exp.Column)]
        if ref_table and local_cols and ref_cols:
            for lc, rc in zip(local_cols, ref_cols):
                pairs.append((lc, ref_table, rc))
    return pairs


def parse_ddl_file(path: str, dialect: str = "ansi") -> list[DDLTableEvidence]:
    """
    Parse a .sql DDL file and return one DDLTableEvidence per CREATE TABLE found.
    Silently skips non-CREATE-TABLE statements (views, indexes, comments, etc.).
    """
    text = Path(path).read_text()
    evidences = []

    statements = sqlglot.parse(text, dialect=dialect)
    for stmt in statements:
        if not isinstance(stmt, exp.Create):
            continue
        if stmt.kind and stmt.kind.upper() != "TABLE":
            continue

        table_expr = stmt.find(exp.Table)
        if not table_expr:
            continue
        table_name = table_expr.name

        col_candidates = []
        for col_def in stmt.find_all(exp.ColumnDef):
            raw_type = col_def.args.get("kind", "")
            raw_type_str = str(raw_type) if raw_type else "VARCHAR"
            normalized = normalize_type(raw_type_str)

            # Nullability: NOT NULL constraint present?
            not_null = any(
                isinstance(c, exp.NotNullColumnConstraint)
                for c in col_def.find_all(exp.ColumnConstraint)
            )

            # Default value
            default_expr = col_def.args.get("default")
            default_val = str(default_expr) if default_expr else None

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
                    default_value=default_val,
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
```

---

## Step 3 — Create canoniq/ingest/document_extractor.py (NEW FILE)

Create a new file `canoniq/ingest/document_extractor.py`.

This extractor handles BRD, PDF, Word, and plain text documents.
It uses Claude as a structured EXTRACTOR (not a generator) to pull
metric definitions out of unstructured prose, then grounds each
extracted candidate against the real warehouse schema.

```python
"""
Document extractor — extracts metric and KPI definitions from unstructured
business documents (BRD, PDF, Word, plain text).

Uses the LLM as an information extractor only — it identifies and structures
what is already written in the document. It does not invent definitions.

After extraction, each candidate is grounded against the real warehouse schema:
column names in the extracted formula are mapped to actual columns that exist.
Candidates that cannot be grounded with confidence > GROUNDING_THRESHOLD
are flagged as ambiguous rather than silently dropped.
"""

import json
import logging
import re
from pathlib import Path

import anthropic
from pydantic import BaseModel

from canoniq.ingest.base import (
    DocumentMetricCandidate,
    SourceType,
    TableSchema,
)

logger = logging.getLogger(__name__)

GROUNDING_THRESHOLD = 0.6   # below this → mark ambiguous, still include

# ── document readers ─────────────────────────────────────────────────────────

def _read_pdf(path: str) -> str:
    """Extract text from PDF using pypdf (install: pip install pypdf)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        raise ImportError(
            "pypdf is required for PDF extraction. "
            "Install with: pip install pypdf"
        )


def _read_docx(path: str) -> str:
    """Extract text from Word .docx using python-docx."""
    try:
        import docx
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    except ImportError:
        raise ImportError(
            "python-docx is required for Word extraction. "
            "Install with: pip install python-docx"
        )


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_document(path: str) -> str:
    """Dispatch to the correct reader based on file extension."""
    ext = Path(path).suffix.lower()
    readers = {
        ".pdf":  _read_pdf,
        ".docx": _read_docx,
        ".doc":  _read_docx,
        ".txt":  _read_text,
        ".md":   _read_text,
    }
    reader = readers.get(ext)
    if not reader:
        raise ValueError(
            f"Unsupported document type '{ext}'. "
            f"Supported: {list(readers)}"
        )
    return reader(path)


# ── approval signal detection ─────────────────────────────────────────────────

_APPROVAL_PATTERNS = [
    re.compile(r"approved\s+by", re.IGNORECASE),
    re.compile(r"sign.?off", re.IGNORECASE),
    re.compile(r"reviewed\s+by", re.IGNORECASE),
    re.compile(r"authorized\s+by", re.IGNORECASE),
    re.compile(r"version\s+history", re.IGNORECASE),
    re.compile(r"document\s+owner", re.IGNORECASE),
]


def has_approval_signal(text: str) -> bool:
    """Return True if the document text contains governance/approval markers."""
    return any(p.search(text) for p in _APPROVAL_PATTERNS)


# ── LLM extraction ────────────────────────────────────────────────────────────

class _RawExtractedMetric(BaseModel):
    """Intermediate model for LLM-extracted metric before schema grounding."""
    raw_name: str
    raw_definition: str
    raw_filter: str | None = None
    source_section: str | None = None
    source_page: int | None = None
    ambiguous: bool = False


_EXTRACTION_SYSTEM = """\
You are an information extraction assistant. Your job is to find and structure
metric and KPI definitions that are explicitly stated in business documents.

CRITICAL RULES:
1. Only extract metrics that are explicitly defined or described in the text.
2. Do NOT invent, infer, or create definitions not present in the document.
3. Capture the raw business language exactly — do not translate to SQL.
4. If a definition is unclear or contradictory, set ambiguous: true.
5. If you find no metrics at all, return an empty array.
6. Return ONLY a valid JSON array. No preamble, no markdown, no explanation.
"""

_EXTRACTION_USER_TEMPLATE = """\
Extract all metric and KPI definitions from the following document.

For each metric found, return a JSON object with:
  - raw_name: the metric name exactly as written
  - raw_definition: the calculation or description exactly as written
  - raw_filter: any filters, conditions, or exclusions mentioned (or null)
  - source_section: the section or heading where it appears (or null)
  - source_page: page number if mentioned (or null)
  - ambiguous: true if the definition is unclear or contradictory

Schema context — these are the actual column names available
(only relevant so you can recognise references to real data):
{schema_summary}

Document text:
---
{document_text}
---

Return ONLY a JSON array. No explanation.
"""


def _schema_summary(schemas: list[TableSchema]) -> str:
    """Build a compact schema reference for the extraction prompt."""
    lines = []
    for s in schemas:
        col_names = ", ".join(c.name for c in s.columns)
        lines.append(f"Table {s.fully_qualified_name}: {col_names}")
    return "\n".join(lines)


def _call_llm_extractor(
    document_text: str,
    schemas: list[TableSchema],
    model: str = "claude-sonnet-4-6",
) -> list[_RawExtractedMetric]:
    """
    Call Claude to extract metric definitions from document text.
    Returns a list of raw (pre-grounding) metric candidates.
    Falls back to empty list on any LLM or parse error.
    """
    client = anthropic.Anthropic()
    prompt = _EXTRACTION_USER_TEMPLATE.format(
        schema_summary=_schema_summary(schemas),
        document_text=document_text[:12000],  # truncate: ~3k tokens
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0,
            system=_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_json = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw_json = re.sub(r"^```[a-z]*\n?", "", raw_json)
        raw_json = re.sub(r"\n?```$", "", raw_json)
        data = json.loads(raw_json)
        return [_RawExtractedMetric(**item) for item in data]
    except Exception as exc:
        logger.warning("LLM extraction failed for document: %s", exc)
        return []


# ── schema grounding ──────────────────────────────────────────────────────────

def _ground_candidate(
    raw: _RawExtractedMetric,
    schemas: list[TableSchema],
) -> tuple[str | None, str | None, float]:
    """
    Try to map the raw business definition to real schema objects.

    Returns (resolved_expression, resolved_table, confidence).
    confidence = 0.0 if nothing could be resolved.

    Strategy (simple heuristic for v0 — no LLM needed here):
    1. Tokenize the raw_definition
    2. For each token that matches a real column name exactly → match
    3. Confidence = matched_tokens / total_meaningful_tokens
    4. Infer aggregation from keywords: "sum", "total" → SUM();
       "count", "number of" → COUNT(DISTINCT); "average", "avg" → AVG()
    5. resolved_expression is the SQL expression if confidence is high enough
    """
    all_columns: dict[str, str] = {}  # col_name → table_name
    for schema in schemas:
        for col in schema.columns:
            all_columns[col.name.lower()] = schema.fully_qualified_name

    definition_lower = raw.raw_definition.lower()
    words = re.findall(r"\b[a-z_]+\b", definition_lower)
    matched = [(w, all_columns[w]) for w in words if w in all_columns]

    if not matched:
        return None, None, 0.0

    confidence = len(matched) / max(len(words), 1)

    # Infer aggregation function from keywords
    agg = "SUM"
    if any(kw in definition_lower for kw in ("count", "number of", "how many")):
        agg = "COUNT"
    elif any(kw in definition_lower for kw in ("average", "avg", "mean")):
        agg = "AVG"

    # Use the most frequently matched table
    from collections import Counter
    table_counts = Counter(t for _, t in matched)
    resolved_table = table_counts.most_common(1)[0][0]

    # Build a simple expression from the best column match
    best_col = matched[0][0]
    if agg == "COUNT":
        resolved_expression = f"COUNT(DISTINCT {best_col})"
    else:
        resolved_expression = f"{agg}({best_col})"

    return resolved_expression, resolved_table, min(confidence, 1.0)


# ── public API ────────────────────────────────────────────────────────────────

def extract_from_document(
    path: str,
    schemas: list[TableSchema],
    llm_model: str = "claude-sonnet-4-6",
) -> list[DocumentMetricCandidate]:
    """
    Extract metric candidates from a business document.

    Pipeline:
    1. Read document text (PDF / DOCX / TXT)
    2. Detect approval signals for OntoRank boost
    3. LLM extracts raw metric definitions (information extraction only)
    4. Ground each raw candidate against real schema
    5. Return DocumentMetricCandidate list (including ambiguous ones, flagged)
    """
    logger.info("Extracting from document: %s", path)
    text = read_document(path)
    approved = has_approval_signal(text)

    source_type = (
        SourceType.BRD_APPROVED if approved else SourceType.BRD_DRAFT
    )
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        source_type = SourceType.PDF_REPORT
    # BRD_APPROVED override if PDF also has approval signal
    if ext == ".pdf" and approved:
        source_type = SourceType.BRD_APPROVED

    raw_candidates = _call_llm_extractor(text, schemas, model=llm_model)
    logger.info("LLM extracted %d raw candidates", len(raw_candidates))

    results = []
    for raw in raw_candidates:
        resolved_expr, resolved_table, confidence = _ground_candidate(
            raw, schemas
        )
        results.append(
            DocumentMetricCandidate(
                raw_name=raw.raw_name,
                raw_definition=raw.raw_definition,
                raw_filter=raw.raw_filter,
                resolved_expression=resolved_expr,
                resolved_table=resolved_table,
                source_type=source_type,
                source_file=path,
                source_page=raw.source_page,
                source_section=raw.source_section,
                has_approval_signal=approved,
                grounding_confidence=confidence,
                ambiguous=raw.ambiguous or confidence < GROUNDING_THRESHOLD,
            )
        )
        logger.debug(
            "  %s → expr=%s table=%s conf=%.2f ambiguous=%s",
            raw.raw_name, resolved_expr, resolved_table,
            confidence, results[-1].ambiguous,
        )

    return results
```

---

## Step 4 — Create canoniq/ingest/excel_extractor.py (NEW FILE)

Create `canoniq/ingest/excel_extractor.py`.

Extracts metric candidates from Excel reports and financial models.
Handles named ranges, calculated cells with formulas, pivot table
column headers, and sheet-level context.

```python
"""
Excel extractor — extracts metric candidates from Excel workbooks.

Targets:
  - Named ranges → metric name + any formula attached
  - Cells with formula starting with = → calculated measure candidates
  - Row/column header detection → dimension candidates
  - Sheet names → business domain classification

No LLM involved. Pure openpyxl parsing.
Requires: pip install openpyxl
"""

import logging
import re
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from canoniq.ingest.base import DocumentMetricCandidate, SourceType, TableSchema

logger = logging.getLogger(__name__)

# Excel aggregation function patterns → SQL equivalents
_EXCEL_TO_SQL_AGG = {
    "SUM":        "SUM",
    "COUNT":      "COUNT",
    "COUNTA":     "COUNT",
    "COUNTIF":    "COUNT",       # approximate
    "COUNTIFS":   "COUNT",
    "AVERAGE":    "AVG",
    "AVERAGEIF":  "AVG",
    "AVERAGEIFS": "AVG",
    "MIN":        "MIN",
    "MAX":        "MAX",
}

_FORMULA_AGG_RE = re.compile(
    r"\b(" + "|".join(_EXCEL_TO_SQL_AGG) + r")\s*\(",
    re.IGNORECASE,
)


def _detect_agg(formula: str) -> str | None:
    """Return the SQL aggregation name for an Excel formula, or None."""
    match = _FORMULA_AGG_RE.search(formula)
    if not match:
        return None
    return _EXCEL_TO_SQL_AGG.get(match.group(1).upper())


def _nearest_label(sheet, cell) -> str | None:
    """
    Find the nearest non-empty, non-formula text cell to the left of
    or above `cell` — used as a human-readable metric name.
    """
    # Check cell directly to the left
    if cell.column > 1:
        left = sheet.cell(row=cell.row, column=cell.column - 1)
        if left.value and not str(left.value).startswith("="):
            return str(left.value).strip()
    # Check cell directly above
    if cell.row > 1:
        above = sheet.cell(row=cell.row - 1, column=cell.column)
        if above.value and not str(above.value).startswith("="):
            return str(above.value).strip()
    return None


def _clean_name(raw: str) -> str:
    """Convert a label like 'Total Net Revenue (USD)' to 'total_net_revenue'."""
    clean = re.sub(r"[^a-zA-Z0-9\s_]", "", raw)
    return re.sub(r"\s+", "_", clean.strip()).lower()


def extract_from_excel(
    path: str,
    schemas: list[TableSchema],
) -> list[DocumentMetricCandidate]:
    """
    Extract metric candidates from an Excel workbook.

    Returns DocumentMetricCandidate list. Candidates from named ranges
    get SourceType.EXCEL_NAMED (higher trust); candidates from bare
    formula cells get SourceType.EXCEL_FORMULA (lower trust).
    """
    logger.info("Extracting from Excel: %s", path)
    wb = openpyxl.load_workbook(path, data_only=False)
    results: list[DocumentMetricCandidate] = []

    # Build a flat set of real column names for grounding
    real_columns: set[str] = set()
    for schema in schemas:
        for col in schema.columns:
            real_columns.add(col.name.lower())

    # ── Pass 1: named ranges (highest trust) ────────────────────────────────
    for named_range in wb.defined_names.definedName:
        name = named_range.name
        # Skip internal Excel names
        if name.startswith("_") or name.startswith("Print"):
            continue
        results.append(
            DocumentMetricCandidate(
                raw_name=name,
                raw_definition=f"Named range: {name}",
                raw_filter=None,
                resolved_expression=None,
                resolved_table=None,
                source_type=SourceType.EXCEL_NAMED,
                source_file=path,
                source_page=None,
                source_section=named_range.attr_text,
                has_approval_signal=False,
                grounding_confidence=0.3,   # named but no column mapping yet
                ambiguous=True,
            )
        )

    # ── Pass 2: formula cells ────────────────────────────────────────────────
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if not cell.value:
                    continue
                formula = str(cell.value)
                if not formula.startswith("="):
                    continue

                agg_fn = _detect_agg(formula)
                if not agg_fn:
                    continue

                label = _nearest_label(sheet, cell)
                raw_name = _clean_name(label) if label else (
                    f"{sheet.title}_{get_column_letter(cell.column)}{cell.row}"
                )

                # Try to find a column name referenced in the formula
                formula_tokens = re.findall(r"\b[a-z_]+\b", formula.lower())
                matched_cols = [t for t in formula_tokens if t in real_columns]

                if matched_cols:
                    resolved_expr = f"{agg_fn}({matched_cols[0]})"
                    confidence = len(matched_cols) / max(len(formula_tokens), 1)
                else:
                    resolved_expr = None
                    confidence = 0.1

                results.append(
                    DocumentMetricCandidate(
                        raw_name=raw_name,
                        raw_definition=f"Excel formula: {formula} (sheet: {sheet.title})",
                        raw_filter=None,
                        resolved_expression=resolved_expr,
                        resolved_table=None,
                        source_type=SourceType.EXCEL_FORMULA,
                        source_file=path,
                        source_page=None,
                        source_section=sheet.title,
                        has_approval_signal=False,
                        grounding_confidence=min(confidence, 1.0),
                        ambiguous=resolved_expr is None,
                    )
                )

    logger.info(
        "Excel extraction complete: %d candidates from %s",
        len(results), Path(path).name,
    )
    return results
```

---

## Step 5 — Update canoniq/config.py

Find the Config dataclass and ADD these fields.
Do not remove or modify any existing fields.

```python
# Add to the Config dataclass:

# Layer 1 — new input sources
ddl_files: list[str] = field(default_factory=list)
# Paths to .sql DDL files. If provided, DDLExtractor runs even without
# a live warehouse connection.

document_files: list[str] = field(default_factory=list)
# Paths to BRD/PDF/Word/text documents. DocumentExtractor will process each.

excel_files: list[str] = field(default_factory=list)
# Paths to Excel reports. ExcelExtractor will process each.

require_query_log: bool = False
# If False (default), pipeline runs without query logs.
# DDL + documents are sufficient inputs.
```

Also add the following to the `canoniq.yaml.example` file
(or create it if it doesn't exist):

```yaml
# canoniq.yaml.example — annotated config

project_name: my_semantic_model

warehouse:
  type: duckdb
  path: ./warehouse.db

# DDL files — used when no live warehouse connection or to supplement it.
# The DDL extractor runs naming-convention heuristics to infer semantic roles.
ddl_files:
  - ./schema/store_sales.sql
  - ./schema/customer.sql

# Business documents — BRDs, PDFs, Word docs, plain text.
# Claude extracts metric definitions; they are grounded against real schema.
document_files:
  - ./docs/revenue_definitions_brd.pdf
  - ./docs/kpi_glossary.docx

# Excel reports — named ranges and formula cells are mined for metric candidates.
excel_files:
  - ./reports/monthly_kpi_report.xlsx

# SQL query log — OPTIONAL. Adds frequency signal but not required.
query_log:
  type: file
  path: ./queries.sql        # comment out or remove if you have no query log

# Output
output:
  formats: [metricflow, osi]
  dir: ./canoniq_output/

# OntoRank weights — adjust to match your organisation's trust model
ontorank:
  weights:
    source_authority: 0.30
    usage_frequency: 0.25
    cross_source_agreement: 0.20
    recency: 0.15
    certification_status: 0.10
  thresholds:
    auto_merge: 0.85
    review: 0.50
    drop: 0.50

llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_retries: 3

continuous:
  enabled: false
  poll_interval_seconds: 300
```

---

## Step 6 — Update canoniq/mining/evidence_bundle.py

The `build_evidence_bundle` function currently only accepts
`AggregationCandidate`, `DimensionCandidate`, and `JoinCandidate`.

ADD a new parameter `document_candidates` and `ddl_evidence` and
merge them into the bundle.

Find the `build_evidence_bundle` function signature and update it to:

```python
def build_evidence_bundle(
    table: TableSchema,
    agg_candidates: list[AggregationCandidate],
    dim_candidates: list[DimensionCandidate],
    join_candidates: list[JoinCandidate],
    dbt_metrics: list[dict] | None = None,
    ddl_evidence: DDLTableEvidence | None = None,       # NEW
    document_candidates: list[DocumentMetricCandidate] | None = None,  # NEW
) -> EvidenceBundle:
```

Inside the function, after the existing candidate merging logic, ADD:

```python
# Merge DDL evidence — convert DDLColumnCandidates into AggregationCandidates
# and DimensionCandidates that feed the same downstream ranking pipeline
if ddl_evidence:
    for col in ddl_evidence.column_candidates:
        if col.inferred_role == "measure_input":
            # Propose SUM as the default aggregation for numeric columns
            ddl_agg = AggregationCandidate(
                expression=f"SUM({col.name})",
                source_table=table.fully_qualified_name,
                source_column=col.name,
                agg_function="SUM",
                filter_expr=None,
                seen_in_queries=[],
                execution_count=0,  # no query log evidence
            )
            # Merge into existing candidates by expression key
            # (existing logic already handles deduplication)
            agg_candidates.append(ddl_agg)

        elif col.inferred_role == "dimension":
            ddl_dim = DimensionCandidate(
                column=col.name,
                table=table.fully_qualified_name,
                is_time=(col.normalized_type == "time"),
                seen_in_queries=[],
            )
            dim_candidates.append(ddl_dim)

# Merge document candidates — add them as AggregationCandidates
# with their resolved expressions if grounding confidence is sufficient
if document_candidates:
    for doc_cand in document_candidates:
        if doc_cand.resolved_expression and not doc_cand.ambiguous:
            doc_agg = AggregationCandidate(
                expression=doc_cand.resolved_expression,
                source_table=doc_cand.resolved_table or table.fully_qualified_name,
                source_column="",   # expression-level, not column-level
                agg_function="",    # already embedded in expression
                filter_expr=doc_cand.raw_filter,
                seen_in_queries=[],
                execution_count=0,
            )
            agg_candidates.append(doc_agg)
```

---

## Step 7 — Update the OntoRank scorer

In `canoniq/ranking/ontorank.py`, find the `SOURCE_AUTHORITY` dict and
REPLACE it with the full version from `canoniq/ingest/base.py` SourceType:

```python
SOURCE_AUTHORITY: dict[str, float] = {
    # from SourceType enum — keep in sync
    "dbt_metric":            1.00,
    "dbt_model":             0.85,
    "data_dictionary":       0.85,
    "ddl_constraint":        0.75,
    "looker_measure":        0.80,
    "tableau_field":         0.78,
    "brd_approved":          0.90,
    "brd_draft":             0.65,
    "excel_named":           0.70,
    "excel_formula":         0.50,
    "pdf_report":            0.55,
    "confluence_page":       0.60,
    "ddl_naming_convention": 0.45,
    "query_log_complex":     0.60,
    "query_log_simple":      0.40,
    "ad_hoc":                0.20,
}
```

---

## Step 8 — Update cli.py to wire the new extractors

Find the `canoniq run` command implementation in `canoniq/cli.py`.

After the existing warehouse + query log connector setup, ADD:

```python
from canoniq.ingest.ddl_extractor import parse_ddl_file
from canoniq.ingest.document_extractor import extract_from_document
from canoniq.ingest.excel_extractor import extract_from_excel

# ── DDL extraction ─────────────────────────────────────────────
all_ddl_evidence: list[DDLTableEvidence] = []
for ddl_path in config.ddl_files:
    evidence = parse_ddl_file(ddl_path)
    all_ddl_evidence.extend(evidence)
    click.echo(f"  DDL: {ddl_path} → {len(evidence)} table(s)")

# ── Document extraction ────────────────────────────────────────
all_doc_candidates: list[DocumentMetricCandidate] = []
for doc_path in config.document_files:
    candidates = extract_from_document(doc_path, schemas, llm_model=config.llm_model)
    all_doc_candidates.extend(candidates)
    click.echo(f"  Doc: {doc_path} → {len(candidates)} candidate(s)")

# ── Excel extraction ───────────────────────────────────────────
for xlsx_path in config.excel_files:
    candidates = extract_from_excel(xlsx_path, schemas)
    all_doc_candidates.extend(candidates)
    click.echo(f"  Excel: {xlsx_path} → {len(candidates)} candidate(s)")
```

Then when calling `build_evidence_bundle`, pass the new parameters:

```python
bundle = build_evidence_bundle(
    table=table_schema,
    agg_candidates=agg_candidates,
    dim_candidates=dim_candidates,
    join_candidates=join_candidates,
    dbt_metrics=dbt_metrics,
    ddl_evidence=next(
        (d for d in all_ddl_evidence if d.table_name == table_name), None
    ),
    document_candidates=[
        c for c in all_doc_candidates
        if c.resolved_table == table_schema.fully_qualified_name
        or c.resolved_table is None   # unresolved candidates passed through for LLM
    ],
)
```

---

## Step 9 — Add new dependencies to pyproject.toml

In the `dependencies` list in `pyproject.toml`, ADD:

```toml
"pypdf>=4.0.0",           # PDF text extraction
"python-docx>=1.1.0",     # Word document extraction
```

`openpyxl` is already a dependency (used by the existing Excel intake workbook
builder). Verify it is present; add it if missing.

---

## Step 10 — Write tests

Create `tests/test_ddl_extractor.py`:

```python
"""Tests for DDL extractor."""
from canoniq.ingest.ddl_extractor import parse_ddl_file, _infer_role
import tempfile, os

def test_infer_role_measure():
    assert _infer_role("total_amt", "number") == ("measure_input", "suffix '_amt' + numeric type → measure_input")

def test_infer_role_time():
    assert _infer_role("created_dt", "string")[0] == "dimension"

def test_infer_role_identifier():
    assert _infer_role("customer_id", "number")[0] == "identifier"

def test_infer_role_flag():
    assert _infer_role("is_active", "boolean")[0] == "flag"

def test_parse_ddl_file_basic():
    ddl = """
    CREATE TABLE orders (
        order_id     BIGINT PRIMARY KEY,
        customer_id  BIGINT NOT NULL,
        order_date   DATE,
        order_amount DECIMAL(10,2),
        status       VARCHAR(20) CHECK (status IN ('pending','completed','cancelled'))
    );
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(ddl)
        path = f.name
    try:
        results = parse_ddl_file(path)
        assert len(results) == 1
        evidence = results[0]
        assert evidence.table_name == "orders"
        assert "order_id" in evidence.pk_columns
        assert len(evidence.check_constraints) >= 1
        roles = {c.name: c.inferred_role for c in evidence.column_candidates}
        assert roles["order_id"] == "identifier"
        assert roles["order_amount"] == "measure_input"
        assert roles["order_date"] == "dimension"
    finally:
        os.unlink(path)
```

Create `tests/test_document_extractor.py`:

```python
"""Tests for document extractor — approval detection and grounding."""
from canoniq.ingest.document_extractor import has_approval_signal, _ground_candidate, _RawExtractedMetric
from canoniq.ingest.base import TableSchema, ColumnSchema

def _make_schema():
    return [TableSchema(
        fully_qualified_name="main.orders",
        primary_keys=["order_id"],
        row_count_approx=1000,
        columns=[
            ColumnSchema("order_id", "number", False, [], 1000),
            ColumnSchema("order_amount", "number", True, [], 500),
            ColumnSchema("status", "string", True, ["completed","pending"], 3),
        ]
    )]

def test_approval_signal_detected():
    assert has_approval_signal("Approved by: John Smith, CFO") is True
    assert has_approval_signal("Just a regular paragraph") is False

def test_grounding_finds_column():
    raw = _RawExtractedMetric(
        raw_name="Total Revenue",
        raw_definition="sum of order_amount for completed orders",
        raw_filter="completed orders",
    )
    expr, table, conf = _ground_candidate(raw, _make_schema())
    assert expr is not None
    assert "order_amount" in expr
    assert conf > 0.0

def test_grounding_no_match():
    raw = _RawExtractedMetric(
        raw_name="EBITDA",
        raw_definition="earnings before interest taxes depreciation",
    )
    expr, table, conf = _ground_candidate(raw, _make_schema())
    assert expr is None
    assert conf == 0.0
```

---

## Verification — run after all steps complete

```bash
# 1. No import errors
python -c "
from canoniq.ingest.ddl_extractor import parse_ddl_file
from canoniq.ingest.document_extractor import extract_from_document
from canoniq.ingest.excel_extractor import extract_from_excel
from canoniq.ingest.base import SourceType, DDLTableEvidence, DocumentMetricCandidate
print('All imports OK')
"

# 2. Unit tests pass
pytest tests/test_ddl_extractor.py tests/test_document_extractor.py -v

# 3. DDL smoke test — create a minimal DDL and parse it
python -c "
from canoniq.ingest.ddl_extractor import parse_ddl_file
import tempfile, os
ddl = '''
CREATE TABLE sales (
    sale_id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    sale_date DATE,
    net_amount DECIMAL(12,2),
    status VARCHAR(20)
);
'''
with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
    f.write(ddl); path = f.name
evidence = parse_ddl_file(path)
os.unlink(path)
print('Table:', evidence[0].table_name)
print('PK:', evidence[0].pk_columns)
for c in evidence[0].column_candidates:
    print(f'  {c.name}: {c.inferred_role} ({c.inference_reason})')
"

# 4. Full pipeline smoke test with DDL-only input (no query log)
# Update canoniq.yaml to point ddl_files at a real DDL file
# and remove or comment out query_log section, then:
canoniq run --config canoniq.yaml --table sales --verbose
```

---

## What NOT to change

- `canoniq/ingest/base.py` existing dataclasses (ColumnSchema, TableSchema, RawQuery, Connector) — only ADD to them
- `canoniq/ingest/warehouse.py` — no changes needed
- `canoniq/ingest/query_log.py` — no changes needed
- `canoniq/ingest/dbt_manifest.py` — no changes needed
- `canoniq/ingest/watcher.py` — no changes needed
- All of layers 2–7 (mining, ranking, proposer, emitters, validation, evals) — untouched
- Any existing tests — only ADD new test files
