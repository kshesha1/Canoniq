"""Deterministic row generation for the brownfield benchmark plus the
report figures derived from those rows.

The report figures are COMPUTED FROM the generated data (never vice versa)
so every non-trap figure is exactly reproducible from the matching Iceberg
snapshot — that reproducibility is the whole point of the benchmark.
"""

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

# --- fictional reference data (public Basel vocabulary only) -----------------

LEGAL_ENTITIES: list[tuple[str, str, str]] = [
    # (LE_CD, LE_NM, RGN_CD)
    ("MNY", "Meridian NY", "AMER"),
    ("MLN", "Meridian London", "EMEA"),
    ("MSG", "Meridian Singapore", "APAC"),
    ("MFR", "Meridian Frankfurt", "EMEA"),
]

ASSET_CLASSES: list[tuple[str, str]] = [
    # (ASST_CLS_CD, ASST_CLS_DESC)
    ("CRP", "Corporate"),
    ("RTL", "Retail"),
    ("SVR", "Sovereign"),
]

EVENT_TYPES: list[tuple[str, str]] = [
    # (EVT_TYP_CD, human label as printed in the report)
    ("INT_FRD", "Internal Fraud"),
    ("EXT_FRD", "External Fraud"),
    ("SYS_FAIL", "System Failure"),
    ("PROC_ERR", "Process Error"),
]

RISK_FACTORS = ["IR_DELTA", "IR_VEGA", "FX_DELTA", "EQ_DELTA"]

Q4_END = date(2025, 12, 31)
Q1_END = date(2026, 3, 31)
QUARTER_ENDS = {"sor_2025q4": Q4_END, "sor_2026q1": Q1_END}

# Silent redefinition planted in the Q1-2026 edition: same metric name,
# but the figures exclude this risk factor. No document mentions it.
Q1_EXCLUDED_RISK_FACTOR = "IR_VEGA"

DEFAULT_SEED = 42


def _amt(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _dsum(rows: list[dict], column: str) -> Decimal:
    return sum((_dec(r[column]) for r in rows), Decimal("0"))


@dataclass
class QuarterData:
    """All physical rows for one quarter-end snapshot."""

    as_of: date
    crd_exp_fct: list[dict]
    rwa_calc_fct: list[dict]
    ops_loss_evt: list[dict]
    mkt_rsk_snstvty: list[dict]


@dataclass
class QuarterFigures:
    """Report figures for one edition, computed from a QuarterData.
    All values are raw USD (or raw counts / percentages) as Decimal."""

    as_of: date
    rwa_total: Decimal = Decimal("0")
    rwa_by_le: dict[str, Decimal] = field(default_factory=dict)         # LE_NM -> value
    rwa_by_cls: dict[str, Decimal] = field(default_factory=dict)        # ASST_CLS_DESC -> value
    cre_total: Decimal = Decimal("0")                                   # exp - collateral
    cre_by_le: dict[str, Decimal] = field(default_factory=dict)
    ace_total: Decimal = Decimal("0")                                   # gross exposure
    ace_by_cls: dict[str, Decimal] = field(default_factory=dict)
    ops_total: Decimal = Decimal("0")
    ops_by_evt: dict[str, Decimal] = field(default_factory=dict)        # "Internal Fraud (INT_FRD)"
    mrs_total: Decimal = Decimal("0")                                   # Q1: excludes IR_VEGA
    mrs_by_le: dict[str, Decimal] = field(default_factory=dict)
    ascb_pct: Decimal = Decimal("0")                                    # spreadsheet-only trap


def _gen_crd_exp(rng: random.Random, as_of: date) -> list[dict]:
    rows = []
    for le_cd, _, _ in LEGAL_ENTITIES:
        for cls_cd, _ in ASSET_CLASSES:
            for i in range(15):
                exp = _amt(rng, 50e6, 900e6)
                rows.append(
                    {
                        "EXP_AMT_USD": exp,
                        "COLL_HELD_AMT": round(exp * rng.uniform(0.10, 0.50), 2),
                        "LE_CD": le_cd,
                        "ASST_CLS_CD": cls_cd,
                        "AS_OF_DT": as_of,
                        "CPTY_ID": f"CP{le_cd}{cls_cd}{i:03d}",
                    }
                )
    return rows


# Trap #1 (deprecated twin): RWA_AMT_V2_DEPR is constructed with exact
# margin control (iterative proportional fitting) so that EVERY legal-entity
# breakdown is ~5-7% off and every asset-class breakdown is >=1% off, while
# the GRAND total lands only 0.3% off — inside the 0.5% fingerprint
# tolerance. Tier-1/2 accept it on the grand total; the constraint solver
# must REJECT it on breakdowns.
_V2_GRAND_TOTAL_RATIO = 1.003
_V2_LE_RATIOS = [0.93, 1.075, 0.945, 1.06]      # per legal entity, pre-adjustment
_V2_CLS_RATIOS = [0.94, 1.07, 0.98]             # per asset class, pre-adjustment


def _ipf_targets(matrix: list[list[float]]) -> list[list[float]]:
    """Fit a V2 matrix to exact row (legal entity) and column (asset class)
    ratio targets whose margins are reconciled to the grand-total ratio."""
    n_rows, n_cols = len(matrix), len(matrix[0])
    row_sums = [sum(row) for row in matrix]
    col_sums = [sum(matrix[i][j] for i in range(n_rows)) for j in range(n_cols)]
    grand = sum(row_sums)
    target_grand = grand * _V2_GRAND_TOTAL_RATIO

    def _reconcile(ratios: list[float], sums: list[float]) -> list[float]:
        shift = (target_grand - sum(r * s for r, s in zip(ratios, sums, strict=True))) / sum(sums)
        return [(r + shift) * s for r, s in zip(ratios, sums, strict=True)]

    row_targets = _reconcile(_V2_LE_RATIOS, row_sums)
    col_targets = _reconcile(_V2_CLS_RATIOS, col_sums)

    fitted = [list(row) for row in matrix]
    for _ in range(50):
        for i in range(n_rows):
            factor = row_targets[i] / sum(fitted[i])
            fitted[i] = [v * factor for v in fitted[i]]
        for j in range(n_cols):
            col_total = sum(fitted[i][j] for i in range(n_rows))
            factor = col_targets[j] / col_total
            for i in range(n_rows):
                fitted[i][j] *= factor
    return fitted


def _gen_rwa(rng: random.Random, as_of: date) -> list[dict]:
    v3 = [
        [_amt(rng, 2e9, 9e9) for _ in ASSET_CLASSES]
        for _ in LEGAL_ENTITIES
    ]
    v2 = _ipf_targets(v3)

    rows = []
    for i, (le_cd, _, _) in enumerate(LEGAL_ENTITIES):
        for j, (cls_cd, _) in enumerate(ASSET_CLASSES):
            rows.append(
                {
                    "RWA_AMT_V3": v3[i][j],
                    "RWA_AMT_V2_DEPR": round(v2[i][j], 2),
                    "LE_CD": le_cd,
                    "ASST_CLS_CD": cls_cd,
                    "AS_OF_DT": as_of,
                }
            )
    return rows


def _gen_ops_loss(rng: random.Random, as_of: date) -> list[dict]:
    quarter_start = date(as_of.year, as_of.month - 2, 1)
    span_days = (as_of - quarter_start).days
    rows = []
    seq = 0
    for le_cd, _, _ in LEGAL_ENTITIES:
        for evt_cd, _ in EVENT_TYPES:
            for _i in range(rng.randint(2, 5)):
                seq += 1
                rows.append(
                    {
                        "LOSS_AMT": _amt(rng, 1e5, 5e6),
                        "EVT_TYP_CD": evt_cd,
                        "LE_CD": le_cd,
                        "EVT_DT": quarter_start + timedelta(days=rng.randint(0, span_days)),
                        "EVT_ID": f"EV{as_of.year}{as_of.month:02d}{seq:04d}",
                    }
                )
    return rows


def _gen_mkt_rsk(
    rng: random.Random, as_of: date, decoy_target_total: Decimal
) -> list[dict]:
    """SNSTVTY_AMT rows plus trap #2: the decoy column HDG_NTNL_AMT, whose
    grand total is planted to coincide with the Operational Losses report
    figure (within fingerprint tolerance in Q1, so it survives Tier 2 and
    must be rejected on dimensional breakdowns by the solver)."""
    rows = []
    for le_cd, _, _ in LEGAL_ENTITIES:
        for factor in RISK_FACTORS:
            for _i in range(rng.randint(3, 6)):
                rows.append(
                    {
                        "SNSTVTY_AMT": _amt(rng, 1e7, 2e8),
                        "RSK_FCTR_CD": factor,
                        "LE_CD": le_cd,
                        "AS_OF_DT": as_of,
                    }
                )

    weights = [rng.uniform(0.5, 1.5) for _ in rows]
    total_weight = sum(weights)
    remaining = decoy_target_total
    for row, weight in zip(rows[:-1], weights[:-1], strict=True):
        part = (decoy_target_total * _dec(weight) / _dec(total_weight)).quantize(
            Decimal("0.01")
        )
        row["HDG_NTNL_AMT"] = float(part)
        remaining -= part
    rows[-1]["HDG_NTNL_AMT"] = float(remaining.quantize(Decimal("0.01")))
    return rows


def generate_quarter(rng: random.Random, as_of: date, is_q1: bool) -> QuarterData:
    crd = _gen_crd_exp(rng, as_of)
    rwa = _gen_rwa(rng, as_of)
    ops = _gen_ops_loss(rng, as_of)

    ops_total = _dsum(ops, "LOSS_AMT")
    # Q1: decoy lands 0.2% off the ops-losses figure (inside the 0.5%
    # tolerance — a genuine Tier-2 coincidence). Q4: nowhere near, so the
    # decoy also fails the prior-quarter footnote constraint.
    decoy_target = ops_total * (Decimal("1.002") if is_q1 else Decimal("1.30"))
    mkt = _gen_mkt_rsk(rng, as_of, decoy_target)

    return QuarterData(
        as_of=as_of, crd_exp_fct=crd, rwa_calc_fct=rwa, ops_loss_evt=ops, mkt_rsk_snstvty=mkt
    )


def compute_figures(data: QuarterData, is_q1: bool) -> QuarterFigures:
    fig = QuarterFigures(as_of=data.as_of)
    le_names = {cd: nm for cd, nm, _ in LEGAL_ENTITIES}
    cls_names = dict(ASSET_CLASSES)
    evt_labels = {cd: f"{label} ({cd})" for cd, label in EVENT_TYPES}

    fig.rwa_total = _dsum(data.rwa_calc_fct, "RWA_AMT_V3")
    for le_cd, le_nm in le_names.items():
        rows = [r for r in data.rwa_calc_fct if r["LE_CD"] == le_cd]
        fig.rwa_by_le[le_nm] = _dsum(rows, "RWA_AMT_V3")
    for cls_cd, desc in cls_names.items():
        rows = [r for r in data.rwa_calc_fct if r["ASST_CLS_CD"] == cls_cd]
        fig.rwa_by_cls[desc] = _dsum(rows, "RWA_AMT_V3")

    fig.cre_total = _dsum(data.crd_exp_fct, "EXP_AMT_USD") - _dsum(
        data.crd_exp_fct, "COLL_HELD_AMT"
    )
    for le_cd, le_nm in le_names.items():
        rows = [r for r in data.crd_exp_fct if r["LE_CD"] == le_cd]
        fig.cre_by_le[le_nm] = _dsum(rows, "EXP_AMT_USD") - _dsum(rows, "COLL_HELD_AMT")

    fig.ace_total = _dsum(data.crd_exp_fct, "EXP_AMT_USD")
    for cls_cd, desc in cls_names.items():
        rows = [r for r in data.crd_exp_fct if r["ASST_CLS_CD"] == cls_cd]
        fig.ace_by_cls[desc] = _dsum(rows, "EXP_AMT_USD")

    fig.ops_total = _dsum(data.ops_loss_evt, "LOSS_AMT")
    for evt_cd, label in evt_labels.items():
        rows = [r for r in data.ops_loss_evt if r["EVT_TYP_CD"] == evt_cd]
        fig.ops_by_evt[label] = _dsum(rows, "LOSS_AMT")

    mrs_rows = data.mkt_rsk_snstvty
    if is_q1:
        # The silent redefinition: same metric name in the report, but the
        # Q1 figures quietly exclude one risk factor.
        mrs_rows = [r for r in mrs_rows if r["RSK_FCTR_CD"] != Q1_EXCLUDED_RISK_FACTOR]
    fig.mrs_total = _dsum(mrs_rows, "SNSTVTY_AMT")
    for le_cd, le_nm in le_names.items():
        rows = [r for r in mrs_rows if r["LE_CD"] == le_cd]
        fig.mrs_by_le[le_nm] = _dsum(rows, "SNSTVTY_AMT")

    # Trap #3: computed in a spreadsheet, exists in no table or simple
    # combination of tables. Gold label: unmappable.
    fig.ascb_pct = Decimal("2.85") if is_q1 else Decimal("2.60")
    return fig


@dataclass
class BenchmarkData:
    q4: QuarterData
    q1: QuarterData
    q4_figures: QuarterFigures
    q1_figures: QuarterFigures


def generate_benchmark_data(seed: int = DEFAULT_SEED) -> BenchmarkData:
    rng = random.Random(seed)
    q4 = generate_quarter(rng, Q4_END, is_q1=False)
    q1 = generate_quarter(rng, Q1_END, is_q1=True)
    q4_figures = compute_figures(q4, is_q1=False)
    q1_figures = compute_figures(q1, is_q1=True)
    _assert_trap_geometry(q4, q1, q4_figures, q1_figures)
    return BenchmarkData(q4=q4, q1=q1, q4_figures=q4_figures, q1_figures=q1_figures)


def _rel_err(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / abs(b)


def _assert_trap_geometry(
    q4: QuarterData, q1: QuarterData, f4: QuarterFigures, f1: QuarterFigures
) -> None:
    """Self-check that the planted traps have the intended geometry and that
    no column total accidentally collides with an unrelated report figure."""
    tol = Decimal("0.005")

    for data, fig in ((q4, f4), (q1, f1)):
        v2_total = _dsum(data.rwa_calc_fct, "RWA_AMT_V2_DEPR")
        assert _rel_err(v2_total, fig.rwa_total) <= tol, "V2 twin must pass grand total"
        for le_cd, le_nm, _ in LEGAL_ENTITIES:
            rows = [r for r in data.rwa_calc_fct if r["LE_CD"] == le_cd]
            v2_le = _dsum(rows, "RWA_AMT_V2_DEPR")
            assert _rel_err(v2_le, fig.rwa_by_le[le_nm]) > tol, (
                f"V2 twin must fail LE breakdown {le_nm}"
            )
        for cls_cd, desc in ASSET_CLASSES:
            rows = [r for r in data.rwa_calc_fct if r["ASST_CLS_CD"] == cls_cd]
            v2_cls = _dsum(rows, "RWA_AMT_V2_DEPR")
            assert _rel_err(v2_cls, fig.rwa_by_cls[desc]) > tol, (
                f"V2 twin must fail asset-class breakdown {desc}"
            )

    decoy_q1 = _dsum(q1.mkt_rsk_snstvty, "HDG_NTNL_AMT")
    assert _rel_err(decoy_q1, f1.ops_total) <= tol, "decoy must hit ops total in Q1"
    decoy_q4 = _dsum(q4.mkt_rsk_snstvty, "HDG_NTNL_AMT")
    assert _rel_err(decoy_q4, f4.ops_total) > tol, "decoy must miss ops total in Q4"

    # No accidental single-column collisions with unrelated headline figures.
    intended = {
        ("RWA_AMT_V3", "rwa_total"),
        ("RWA_AMT_V2_DEPR", "rwa_total"),   # trap #1, by construction
        ("EXP_AMT_USD", "ace_total"),
        ("LOSS_AMT", "ops_total"),
        ("HDG_NTNL_AMT", "ops_total"),      # trap #2, Q1 only by construction
        ("SNSTVTY_AMT", "mrs_total"),       # true in Q4 (unfiltered definition)
    }
    for data, fig in ((q4, f4), (q1, f1)):
        col_totals = {
            "EXP_AMT_USD": _dsum(data.crd_exp_fct, "EXP_AMT_USD"),
            "COLL_HELD_AMT": _dsum(data.crd_exp_fct, "COLL_HELD_AMT"),
            "RWA_AMT_V3": _dsum(data.rwa_calc_fct, "RWA_AMT_V3"),
            "RWA_AMT_V2_DEPR": _dsum(data.rwa_calc_fct, "RWA_AMT_V2_DEPR"),
            "LOSS_AMT": _dsum(data.ops_loss_evt, "LOSS_AMT"),
            "SNSTVTY_AMT": _dsum(data.mkt_rsk_snstvty, "SNSTVTY_AMT"),
            "HDG_NTNL_AMT": _dsum(data.mkt_rsk_snstvty, "HDG_NTNL_AMT"),
        }
        report_totals = {
            "rwa_total": fig.rwa_total,
            "cre_total": fig.cre_total,
            "ace_total": fig.ace_total,
            "ops_total": fig.ops_total,
            "mrs_total": fig.mrs_total,
        }
        for col, col_total in col_totals.items():
            for name, reported in report_totals.items():
                if _rel_err(col_total, reported) <= tol and (col, name) not in intended:
                    raise AssertionError(
                        f"accidental collision: SUM({col}) matches {name} — reseed"
                    )
