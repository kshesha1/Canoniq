"""Synthetic brownfield benchmark — a fictional regulated risk data mart.

All names, figures, and document text are invented from public Basel /
regulatory vocabulary. Nothing resembles any real institution's internals.
"""

from pathlib import Path

BENCHMARK_ROOT = Path(__file__).parent

WAREHOUSE_DIR_NAME = "warehouse"
REPORTS_DIR_NAME = "reports"
TABLEAU_DIR_NAME = "tableau"
DOCS_DIR_NAME = "docs"
GOLD_LABELS_NAME = "gold_labels.yaml"

ICEBERG_NAMESPACE = "risk_mart"
CATALOG_DB_NAME = "catalog.db"

# Snapshot property carrying the business as-of date of each snapshot.
AS_OF_SNAPSHOT_PROPERTY = "canoniq.as_of"
