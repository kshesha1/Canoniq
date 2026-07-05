"""Benchmark generator: `python -m benchmark.brownfield.generate`.

Builds the full synthetic brownfield benchmark:
  - Iceberg warehouse (SQLite catalog) with two quarter-end snapshots per
    table and the planted schema-evolution rename (EXP_AMT -> EXP_AMT_USD)
  - two PDF editions of the board report
  - Tableau workbook, policy documents, sparse BRD
  - gold_labels.yaml

Deterministic (seeded) and idempotent: the warehouse and all artifacts are
rebuilt from scratch on every run. (The Iceberg catalog necessarily
contains fresh UUIDs/timestamps; all business data and figures are
byte-for-byte deterministic.)
"""

import shutil
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog

from benchmark.brownfield import (
    AS_OF_SNAPSHOT_PROPERTY,
    BENCHMARK_ROOT,
    DOCS_DIR_NAME,
    GOLD_LABELS_NAME,
    ICEBERG_NAMESPACE,
    REPORTS_DIR_NAME,
    TABLEAU_DIR_NAME,
    WAREHOUSE_DIR_NAME,
)
from benchmark.brownfield.artifacts import (
    write_documents,
    write_gold_labels,
    write_tableau_workbook,
)
from benchmark.brownfield.data import (
    ASSET_CLASSES,
    DEFAULT_SEED,
    LEGAL_ENTITIES,
    BenchmarkData,
    QuarterData,
    generate_benchmark_data,
)
from benchmark.brownfield.report_pdf import build_editions, write_report_pdf
from canoniq.fingerprint.catalog import open_catalog

_SCHEMAS: dict[str, pa.Schema] = {
    # CRD_EXP_FCT is created with the Oracle-heritage name EXP_AMT; the
    # column is renamed to EXP_AMT_USD between the Q4 and Q1 snapshots
    # (planted schema-evolution signal).
    "CRD_EXP_FCT": pa.schema(
        [
            ("EXP_AMT", pa.float64()),
            ("COLL_HELD_AMT", pa.float64()),
            ("LE_CD", pa.string()),
            ("ASST_CLS_CD", pa.string()),
            ("AS_OF_DT", pa.date32()),
            ("CPTY_ID", pa.string()),
        ]
    ),
    "RWA_CALC_FCT": pa.schema(
        [
            ("RWA_AMT_V3", pa.float64()),
            ("RWA_AMT_V2_DEPR", pa.float64()),
            ("LE_CD", pa.string()),
            ("ASST_CLS_CD", pa.string()),
            ("AS_OF_DT", pa.date32()),
        ]
    ),
    "LE_REF": pa.schema(
        [("LE_CD", pa.string()), ("LE_NM", pa.string()), ("RGN_CD", pa.string())]
    ),
    "ASST_CLS_REF": pa.schema(
        [("ASST_CLS_CD", pa.string()), ("ASST_CLS_DESC", pa.string())]
    ),
    "OPS_LOSS_EVT": pa.schema(
        [
            ("LOSS_AMT", pa.float64()),
            ("EVT_TYP_CD", pa.string()),
            ("LE_CD", pa.string()),
            ("EVT_DT", pa.date32()),
            ("EVT_ID", pa.string()),
        ]
    ),
    "MKT_RSK_SNSTVTY": pa.schema(
        [
            ("SNSTVTY_AMT", pa.float64()),
            ("HDG_NTNL_AMT", pa.float64()),
            ("RSK_FCTR_CD", pa.string()),
            ("LE_CD", pa.string()),
            ("AS_OF_DT", pa.date32()),
        ]
    ),
}

_REF_ROWS = {
    "LE_REF": [
        {"LE_CD": cd, "LE_NM": nm, "RGN_CD": rgn} for cd, nm, rgn in LEGAL_ENTITIES
    ],
    "ASST_CLS_REF": [
        {"ASST_CLS_CD": cd, "ASST_CLS_DESC": desc} for cd, desc in ASSET_CLASSES
    ],
}


def _quarter_rows(data: QuarterData, use_heritage_exp_name: bool) -> dict[str, list[dict]]:
    crd = data.crd_exp_fct
    if use_heritage_exp_name:
        crd = [
            {**{k: v for k, v in r.items() if k != "EXP_AMT_USD"},
             "EXP_AMT": r["EXP_AMT_USD"]}
            for r in crd
        ]
    return {
        "CRD_EXP_FCT": crd,
        "RWA_CALC_FCT": data.rwa_calc_fct,
        "OPS_LOSS_EVT": data.ops_loss_evt,
        "MKT_RSK_SNSTVTY": data.mkt_rsk_snstvty,
        **_REF_ROWS,
    }


def open_benchmark_catalog(warehouse_dir: Path) -> SqlCatalog:
    """The catalog name must match what `canoniq bootstrap` opens
    (canoniq.fingerprint.catalog.open_catalog): SqlCatalog scopes all
    rows in catalog.db by catalog name."""
    return open_catalog(warehouse_dir, create=True)


def write_warehouse(warehouse_dir: Path, bench: BenchmarkData) -> None:
    if warehouse_dir.exists():
        shutil.rmtree(warehouse_dir)
    warehouse_dir.mkdir(parents=True)

    catalog = open_benchmark_catalog(warehouse_dir)
    catalog.create_namespace(ICEBERG_NAMESPACE)

    tables = {
        name: catalog.create_table(f"{ICEBERG_NAMESPACE}.{name}", schema=schema)
        for name, schema in _SCHEMAS.items()
    }

    def snapshot(rows_by_table: dict[str, list[dict]], as_of: date, exp_col: str) -> None:
        props = {AS_OF_SNAPSHOT_PROPERTY: as_of.isoformat()}
        for name, rows in rows_by_table.items():
            schema = _SCHEMAS[name]
            if name == "CRD_EXP_FCT" and exp_col != "EXP_AMT":
                fields = [
                    pa.field(exp_col if f.name == "EXP_AMT" else f.name, f.type)
                    for f in schema
                ]
                schema = pa.schema(fields)
            arrow = pa.Table.from_pylist(rows, schema=schema)
            tables[name].overwrite(arrow, snapshot_properties=props)

    # Q4 snapshot with the heritage column name...
    snapshot(_quarter_rows(bench.q4, use_heritage_exp_name=True), bench.q4.as_of, "EXP_AMT")

    # ...then the planted schema evolution...
    with tables["CRD_EXP_FCT"].update_schema() as update:
        update.rename_column("EXP_AMT", "EXP_AMT_USD")

    # ...then the Q1 snapshot under the new name.
    snapshot(
        _quarter_rows(bench.q1, use_heritage_exp_name=False), bench.q1.as_of, "EXP_AMT_USD"
    )


def generate(root: Path = BENCHMARK_ROOT, seed: int = DEFAULT_SEED) -> BenchmarkData:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bench = generate_benchmark_data(seed)

    write_warehouse(root / WAREHOUSE_DIR_NAME, bench)

    q4_edition, q1_edition = build_editions(bench)
    reports_dir = root / REPORTS_DIR_NAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_report_pdf(q4_edition, str(reports_dir / "sor_2025q4.pdf"))
    write_report_pdf(q1_edition, str(reports_dir / "sor_2026q1.pdf"))

    write_tableau_workbook(root / TABLEAU_DIR_NAME / "risk_dashboard.twb")
    write_documents(root / DOCS_DIR_NAME)
    write_gold_labels(root / GOLD_LABELS_NAME, q4_edition, q1_edition)
    return bench


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else BENCHMARK_ROOT
    generate(root)
    print(f"Benchmark generated under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
