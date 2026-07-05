"""Module A acceptance: deterministic generation, idempotency, and the
round-trip proof that every non-trap report figure is reproducible from
the correct Iceberg snapshot via PyIceberg + DuckDB within 0.5%."""

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pdfplumber
import pytest
import yaml

from benchmark.brownfield import AS_OF_SNAPSHOT_PROPERTY, ICEBERG_NAMESPACE
from benchmark.brownfield.data import generate_benchmark_data
from benchmark.brownfield.generate import generate, open_benchmark_catalog

TOLERANCE = Decimal("0.005")

_EXPR_RE = re.compile(
    r"^SUM\((?P<t1>\w+)\.(?P<c1>\w+)\)"
    r"(?:\s*(?P<op>[-+/])\s*SUM\((?P<t2>\w+)\.(?P<c2>\w+)\))?"
    r"(?:\s*WHERE\s*(?P<fcol>\w+)(?P<fop><>|=)'(?P<fval>\w+)')?$"
)

_DIM_JOINS = {
    # dim key -> (label column, ref table, join column)
    "legal_entity": ("LE_NM", "LE_REF", "LE_CD"),
    "asset_class": ("ASST_CLS_DESC", "ASST_CLS_REF", "ASST_CLS_CD"),
    "event_type": ("EVT_TYP_CD", None, None),  # label carries the code in parens
}


def _snapshot_arrow(catalog, table_name: str, as_of: date):
    """Latest snapshot whose as-of property matches, normalized to current
    column names (handles the planted EXP_AMT -> EXP_AMT_USD rename)."""
    table = catalog.load_table(f"{ICEBERG_NAMESPACE}.{table_name}")
    match = None
    for snap in table.snapshots():
        prop = snap.summary.additional_properties.get(AS_OF_SNAPSHOT_PROPERTY)
        if prop and abs(date.fromisoformat(prop) - as_of) <= timedelta(days=3):
            match = snap
    assert match is not None, f"no snapshot for {table_name} @ {as_of}"
    arrow = table.scan(snapshot_id=match.snapshot_id).to_arrow()
    current_names = {f.field_id: f.name for f in table.schema().fields}
    snap_schema = next(s for s in table.metadata.schemas if s.schema_id == match.schema_id)
    renames = {
        f.name: current_names[f.field_id]
        for f in snap_schema.fields
        if f.field_id in current_names and current_names[f.field_id] != f.name
    }
    if renames:
        arrow = arrow.rename_columns([renames.get(n, n) for n in arrow.column_names])
    return arrow


def _evaluate(con, expr: str, dims: dict[str, str], catalog, as_of: date) -> Decimal:
    m = _EXPR_RE.match(expr)
    assert m, f"unparseable gold expression: {expr}"
    t1 = m.group("t1")
    con.register("fact", _snapshot_arrow(catalog, t1, as_of))

    where = ""
    if m.group("fcol"):
        where = f" WHERE {m.group('fcol')} {m.group('fop')} '{m.group('fval')}'"

    join, group_filter = "", ""
    if dims:
        (dim_key, label), = dims.items()
        label_col, ref_table, join_col = _DIM_JOINS[dim_key]
        if ref_table:
            con.register("ref", _snapshot_arrow(catalog, ref_table, as_of))
            join = f" JOIN ref ON fact.{join_col} = ref.{join_col}"
            group_filter = f"ref.{label_col} = '{label}'"
        else:
            code = re.search(r"\(([A-Z_]+)\)", label).group(1)
            group_filter = f"fact.{label_col} = '{code}'"
        where = f"{where} AND {group_filter}" if where else f" WHERE {group_filter}"

    def term_sql(table: str, column: str) -> Decimal:
        assert table == t1, "two-term gold expressions stay within one table here"
        row = con.sql(f"SELECT SUM({column}) FROM fact{join}{where}").fetchone()
        return Decimal(str(row[0]))

    value = term_sql(t1, m.group("c1"))
    if m.group("op"):
        rhs = term_sql(m.group("t2"), m.group("c2"))
        value = value - rhs if m.group("op") == "-" else (
            value + rhs if m.group("op") == "+" else value / rhs
        )
    return value


def test_round_trip_all_non_trap_figures(brownfield_root: Path):
    gold = yaml.safe_load((brownfield_root / "gold_labels.yaml").read_text())
    catalog = open_benchmark_catalog(brownfield_root / "warehouse")
    con = duckdb.connect()

    checked = 0
    for report in gold["reports"].values():
        for metric in report["metrics"]:
            if metric["expression"] == "unmappable":
                continue
            for inst in metric["instances"]:
                computed = _evaluate(
                    con,
                    metric["expression"],
                    inst["dims"],
                    catalog,
                    date.fromisoformat(inst["as_of"]),
                )
                reported = Decimal(inst["raw"])
                rel_err = abs(computed - reported) / abs(reported)
                assert rel_err <= TOLERANCE, (
                    f"{metric['name']} {inst['dims']} @ {inst['as_of']}: "
                    f"computed {computed} vs reported {reported} (err {rel_err})"
                )
                checked += 1
    assert checked >= 50  # both editions, totals + breakdowns + footnotes


def test_generation_is_deterministic():
    a = generate_benchmark_data(seed=42)
    b = generate_benchmark_data(seed=42)
    assert a.q1_figures == b.q1_figures
    assert a.q4.crd_exp_fct == b.q4.crd_exp_fct


def test_generation_is_idempotent(brownfield_root: Path):
    gold_before = (brownfield_root / "gold_labels.yaml").read_text()
    generate(brownfield_root)  # second run over the same directory
    assert (brownfield_root / "gold_labels.yaml").read_text() == gold_before
    # warehouse is rebuilt and still resolvable
    catalog = open_benchmark_catalog(brownfield_root / "warehouse")
    assert catalog.load_table(f"{ICEBERG_NAMESPACE}.CRD_EXP_FCT") is not None


def test_reports_have_expected_shape(brownfield_root: Path):
    for name in ("sor_2025q4.pdf", "sor_2026q1.pdf"):
        with pdfplumber.open(brownfield_root / "reports" / name) as pdf:
            n_tables = sum(len(page.extract_tables()) for page in pdf.pages)
            assert 12 <= len(pdf.pages) <= 15, f"{name}: {len(pdf.pages)} pages"
            assert n_tables >= 7


def test_schema_evolution_rename_planted(brownfield_root: Path):
    catalog = open_benchmark_catalog(brownfield_root / "warehouse")
    table = catalog.load_table(f"{ICEBERG_NAMESPACE}.CRD_EXP_FCT")
    names_across_history = {
        field.name for schema in table.metadata.schemas for field in schema.fields
    }
    assert {"EXP_AMT", "EXP_AMT_USD"} <= names_across_history


@pytest.mark.parametrize("column", ["RWA_AMT_V2_DEPR", "HDG_NTNL_AMT"])
def test_trap_columns_exist(brownfield_root: Path, column: str):
    catalog = open_benchmark_catalog(brownfield_root / "warehouse")
    all_columns = {
        f.name
        for t in ("RWA_CALC_FCT", "MKT_RSK_SNSTVTY")
        for f in catalog.load_table(f"{ICEBERG_NAMESPACE}.{t}").schema().fields
    }
    assert column in all_columns
