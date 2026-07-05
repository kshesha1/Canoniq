"""Numeric fingerprinting engine (Module D).

Three bounded tiers of candidate generation plus a constraint-satisfaction
solver, all executed via DuckDB over PyIceberg snapshot scans. Every query
targets the snapshot whose as-of matches the report figure being tested.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class FingerprintConfig:
    tolerance: Decimal = Decimal("0.005")   # |computed-reported|/|reported|
    snapshot_window_days: int = 3           # fail loudly beyond this
    tier1_top_k: int = 10                   # OntoRank/name shortlist size
    max_columns: int = 500                  # Tier-2 single-column scan bound
    max_tier3_hypotheses: int = 2000        # per metric; truncate by score
    near_miss_window: Decimal = Decimal("0.6")  # scopes Tier-3 filter search
    max_filter_cardinality: int = 12        # only filter on low-card columns
