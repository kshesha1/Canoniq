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
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel

from canoniq.ingest.base import DocumentMetricCandidate, SourceType, TableSchema

logger = logging.getLogger(__name__)

GROUNDING_THRESHOLD = 0.6   # below this -> mark ambiguous, still include

# -- document readers ---------------------------------------------------------


def _read_pdf(path: str) -> str:
    """Extract text from PDF using pypdf (install: pip install pypdf)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ImportError(
            "pypdf is required for PDF extraction. Install with: pip install pypdf"
        ) from e
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _read_docx(path: str) -> str:
    """Extract text from Word .docx using python-docx."""
    try:
        import docx
    except ImportError as e:
        raise ImportError(
            "python-docx is required for Word extraction. "
            "Install with: pip install python-docx"
        ) from e
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_document(path: str) -> str:
    """Dispatch to the correct reader based on file extension."""
    ext = Path(path).suffix.lower()
    readers = {
        ".pdf": _read_pdf,
        ".docx": _read_docx,
        ".doc": _read_docx,
        ".txt": _read_text,
        ".md": _read_text,
    }
    reader = readers.get(ext)
    if not reader:
        raise ValueError(f"Unsupported document type '{ext}'. Supported: {list(readers)}")
    return reader(path)


# -- approval signal detection -------------------------------------------------

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


# -- LLM extraction -------------------------------------------------------------


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
    client: Any = None,
) -> list[_RawExtractedMetric]:
    """
    Call Claude to extract metric definitions from document text.
    Returns a list of raw (pre-grounding) metric candidates.
    Falls back to empty list on any LLM or parse error.

    `client` is injectable (defaults to a real anthropic.Anthropic()) so
    tests never need to hit a real API, matching the pattern used in
    canoniq.proposer.llm.
    """
    client = client or anthropic.Anthropic()
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


# -- schema grounding -----------------------------------------------------------


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
    2. For each token that matches a real column name exactly -> match
    3. Confidence = matched_tokens / total_meaningful_tokens
    4. Infer aggregation from keywords: "sum", "total" -> SUM();
       "count", "number of" -> COUNT(DISTINCT); "average", "avg" -> AVG()
    5. resolved_expression is the SQL expression if confidence is high enough
    """
    all_columns: dict[str, str] = {}  # col_name -> table_name
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
    table_counts = Counter(t for _, t in matched)
    resolved_table = table_counts.most_common(1)[0][0]

    # Build a simple expression from the best column match
    best_col = matched[0][0]
    if agg == "COUNT":
        resolved_expression = f"COUNT(DISTINCT {best_col})"
    else:
        resolved_expression = f"{agg}({best_col})"

    return resolved_expression, resolved_table, min(confidence, 1.0)


# -- public API -------------------------------------------------------------------


def extract_from_document(
    path: str,
    schemas: list[TableSchema],
    llm_model: str = "claude-sonnet-4-6",
    client: Any = None,
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

    source_type = SourceType.BRD_APPROVED if approved else SourceType.BRD_DRAFT
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        source_type = SourceType.PDF_REPORT
    # BRD_APPROVED override if PDF also has approval signal
    if ext == ".pdf" and approved:
        source_type = SourceType.BRD_APPROVED

    raw_candidates = _call_llm_extractor(text, schemas, model=llm_model, client=client)
    logger.info("LLM extracted %d raw candidates", len(raw_candidates))

    results = []
    for raw in raw_candidates:
        resolved_expr, resolved_table, confidence = _ground_candidate(raw, schemas)
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
            "  %s -> expr=%s table=%s conf=%.2f ambiguous=%s",
            raw.raw_name,
            resolved_expr,
            resolved_table,
            confidence,
            results[-1].ambiguous,
        )

    return results
