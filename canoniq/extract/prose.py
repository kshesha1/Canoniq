"""Deterministic prose mining: formula hypotheses and metric definition
statements from report commentary and policy documents.

These regex miners are the LLM-free default path (the benchmark must run
offline and deterministically). `canoniq.extract.report` layers an
optional LLM pass on top for messier real-world prose.
"""

import re

from canoniq.models import FormulaHypothesis

# Words that terminate a term description ("gross exposure less collateral
# held, measured across..." -> the term ends before "measured").
_TERM_STOP = (
    r"(?:,|\.|;| measured| recorded| computed| aggregated| across| during"
    r"| under| held for| presented| at each| for reporting)"
)

_METRIC_NAME = r"(?P<name>(?:The )?[A-Z][A-Za-z0-9 /&-]{2,60}?)"
_TERM = r"[a-z][a-z0-9 \-]{2,60}?"


def _clean_name(name: str) -> str:
    name = re.sub(r"^The ", "", name).strip()
    # PDF text flattening can run a section heading straight into the first
    # sentence ("Total Credit RWA Total Credit RWA is..."): collapse the
    # duplicated prefix.
    tokens = name.split()
    half = len(tokens) // 2
    if half and tokens[:half] == tokens[half:]:
        name = " ".join(tokens[:half])
    return name


def _clean_term(term: str) -> str:
    return term.strip().rstrip(",.;")


_BINARY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            rf"{_METRIC_NAME} is (?:calculated|defined|derived) as "
            rf"(?P<a>{_TERM}) less (?:eligible )?(?P<b>{_TERM}){_TERM_STOP}"
        ),
        "A - B",
    ),
    (
        re.compile(
            rf"{_METRIC_NAME} is (?:calculated|defined|derived) as "
            rf"(?P<a>{_TERM}) plus (?P<b>{_TERM}){_TERM_STOP}"
        ),
        "A + B",
    ),
    (
        re.compile(
            rf"{_METRIC_NAME} is (?:calculated|defined|derived) as "
            rf"(?P<a>{_TERM}) divided by (?P<b>{_TERM}){_TERM_STOP}"
        ),
        "A / B",
    ),
]

_UNARY_PATTERNS: list[re.Pattern] = [
    re.compile(rf"{_METRIC_NAME} (?:is|represents?) the sum of (?P<a>{_TERM}){_TERM_STOP}"),
    re.compile(rf"{_METRIC_NAME} represents? (?P<a>{_TERM}) measured before"),
    re.compile(rf"{_METRIC_NAME} aggregates? (?P<a>{_TERM}) across"),
    re.compile(
        rf"{_METRIC_NAME} is (?:defined|calculated) as the aggregate "
        rf"(?P<a>{_TERM}){_TERM_STOP}"
    ),
]


def mine_formula_hypotheses(text: str, source_locator: str = "") -> list[FormulaHypothesis]:
    """Mine metric-logic hypotheses from commentary prose. Purely
    structural — no invention: every hypothesis quotes the sentence it
    came from verbatim."""
    hypotheses: list[FormulaHypothesis] = []
    seen: set[tuple[str, str]] = set()
    flat = " ".join(text.split())

    def add(name: str, structure: str, terms: list[str], match: re.Match) -> None:
        name = _clean_name(name)
        key = (name, structure)
        if key in seen:
            return
        seen.add(key)
        sentence_start = flat.rfind(".", 0, match.start()) + 1
        sentence_end = flat.find(".", match.end())
        verbatim = flat[sentence_start : sentence_end + 1].strip()
        # drop a section heading that ran into the sentence
        # ("Total Credit RWA Total Credit RWA is the sum of...")
        if verbatim.startswith(f"{name} {name}"):
            verbatim = verbatim[len(name) + 1 :]
        hypotheses.append(
            FormulaHypothesis(
                metric_name=name,
                structure=structure,
                term_descriptions=[_clean_term(t) for t in terms],
                source_locator=source_locator,
                verbatim=verbatim,
            )
        )

    for pattern, structure in _BINARY_PATTERNS:
        for m in pattern.finditer(flat):
            add(m.group("name"), structure, [m.group("a"), m.group("b")], m)
    for pattern in _UNARY_PATTERNS:
        for m in pattern.finditer(flat):
            add(m.group("name"), "SUM(A)", [m.group("a")], m)
    return hypotheses


_STATEMENT_RE = re.compile(
    rf"{_METRIC_NAME} (?:is (?:defined|calculated|derived) as|represents?"
    rf"|aggregates?)\b[^.]*\."
)


def mine_metric_statements(text: str) -> list[tuple[str, str]]:
    """(metric name, verbatim definition sentence) pairs — the raw material
    for cross-source contradiction detection in the conflict report."""
    flat = " ".join(text.split())
    out = []
    for m in _STATEMENT_RE.finditer(flat):
        out.append((_clean_name(m.group("name")), m.group(0).strip()))
    return out


_DOC_DATE_RES = [
    re.compile(r"Effective\s+(?P<date>\w+ \d{1,2}, \d{4})"),
    re.compile(r"Effective\s+(?P<date>\d{4}-\d{2}-\d{2})"),
    re.compile(r"dated\s+(?P<date>\w+ \d{1,2}, \d{4})", re.IGNORECASE),
]


def mine_document_date(text: str) -> str | None:
    """Best-effort document date ('Effective March 1, 2024' etc.)."""
    for pattern in _DOC_DATE_RES:
        m = pattern.search(text)
        if m:
            return m.group("date")
    return None


_COLUMN_GLOSSARY_RE = re.compile(
    r"(?P<table>[A-Z][A-Z0-9_]+)\.(?P<column>[A-Z][A-Z0-9_]+)\s*:\s*(?P<desc>[^\n]+)"
)


def mine_column_glossary(text: str) -> dict[tuple[str, str], str]:
    """(table, column) -> business description, mined from BRD-style
    bullet lists ('CRD_EXP_FCT.EXP_AMT_USD: gross exposure amount...').
    Feeds Tier-3 term resolution."""
    return {
        (m.group("table"), m.group("column")): m.group("desc").strip()
        for m in _COLUMN_GLOSSARY_RE.finditer(text)
    }
