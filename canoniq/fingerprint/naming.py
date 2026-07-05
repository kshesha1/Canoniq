"""Name similarity between business metric language and cryptic
Oracle-heritage identifiers.

Deterministic: abbreviation expansion + token overlap + sequence ratio.
Column descriptions mined from BRDs/policies boost matches when present.
# V2: pluggable embedding backend for term resolution.
"""

import re
from difflib import SequenceMatcher

# Public financial/regulatory abbreviation conventions. Expansion of a
# token may be multi-word ("RWA" -> "risk weighted assets").
ABBREVIATIONS: dict[str, str] = {
    "amt": "amount",
    "exp": "exposure",
    "coll": "collateral",
    "rwa": "risk weighted assets",
    "le": "legal entity",
    "cd": "code",
    "nm": "name",
    "rgn": "region",
    "asst": "asset",
    "cls": "class",
    "desc": "description",
    "snstvty": "sensitivity",
    "rsk": "risk",
    "fctr": "factor",
    "evt": "event",
    "typ": "type",
    "dt": "date",
    "fct": "fact",
    "calc": "calculation",
    "crd": "credit",
    "mkt": "market",
    "ops": "operational",
    "cpty": "counterparty",
    "hdg": "hedge",
    "ntnl": "notional",
    "ref": "reference",
    "depr": "deprecated",
    "usd": "usd",
    "id": "identifier",
    "avg": "average",
    "tot": "total",
    "bal": "balance",
    "qty": "quantity",
    "pct": "percent",
}

_STOPWORDS = {"the", "of", "by", "a", "an", "in", "for", "to", "and", "total",
              "gross", "net", "adjusted", "all"}
# "total"/"gross"/"net"/"adjusted" are qualifiers, not signal: nearly every
# board metric carries one, so they only produce false token overlap.

# Version / lifecycle suffixes carry no semantic content: a deprecated twin
# (RWA_AMT_V2_DEPR) is exactly as name-plausible as its successor — which is
# precisely why name matching must not down-rank it and empirical
# fingerprinting must adjudicate between the two.
_NOISE_TOKENS = {"v1", "v2", "v3", "v4", "v5", "depr", "deprecated", "old",
                 "legacy", "bak", "tmp", "new"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def expand_identifier(identifier: str) -> set[str]:
    """CRD_EXP_FCT.EXP_AMT_USD -> {credit, exposure, fact, amount, usd, ...}"""
    words: set[str] = set()
    for token in _tokens(identifier) - _NOISE_TOKENS:
        expansion = ABBREVIATIONS.get(token)
        if expansion:
            words.update(expansion.split())
        words.add(token)
    return words


def similarity(business_text: str, identifier: str, description: str = "") -> float:
    """0..1 similarity between business language and a physical identifier,
    optionally boosted by a mined business description of the column."""
    text_tokens = _tokens(business_text)
    # expand the business side too ("RWA" appears verbatim in reports)
    expanded_text: set[str] = set()
    for t in text_tokens:
        expansion = ABBREVIATIONS.get(t)
        if expansion:
            expanded_text.update(expansion.split())
        expanded_text.add(t)
    expanded_text -= _STOPWORDS

    id_tokens = expand_identifier(identifier) - _STOPWORDS
    if not expanded_text or not id_tokens:
        return 0.0

    overlap = expanded_text & id_tokens
    f1 = 2 * len(overlap) / (len(expanded_text) + len(id_tokens))
    ratio = SequenceMatcher(
        None, " ".join(sorted(expanded_text)), " ".join(sorted(id_tokens))
    ).ratio()
    score = max(f1, ratio * 0.8)

    if description:
        desc_tokens = _tokens(description) - _STOPWORDS
        if desc_tokens:
            desc_overlap = expanded_text & desc_tokens
            desc_f1 = 2 * len(desc_overlap) / (len(expanded_text) + len(desc_tokens))
            score = max(score, desc_f1)
    return score


def resolve_term(
    term_description: str,
    columns: list[tuple[str, str]],
    glossary: dict[tuple[str, str], str] | None = None,
    top_n: int = 3,
) -> list[tuple[str, str, float]]:
    """Rank physical columns for one prose term description
    ('gross exposure' -> CRD_EXP_FCT.EXP_AMT_USD). Each term is a much
    easier sub-problem than the whole metric."""
    glossary = glossary or {}
    scored = [
        (table, col, similarity(term_description, f"{table}.{col}",
                                glossary.get((table, col), "")))
        for table, col in columns
    ]
    scored.sort(key=lambda x: (-x[2], x[0], x[1]))
    return [s for s in scored[:top_n] if s[2] > 0.15]
