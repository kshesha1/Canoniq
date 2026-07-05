"""Abstract Connector base class and shared ingest data models."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass
class ColumnSchema:
    name: str
    data_type: str                 # normalized: string | number | time | boolean
    is_nullable: bool
    sample_values: list[Any]       # up to 5 distinct values
    cardinality_approx: int | None # None if unknown


@dataclass
class TableSchema:
    fully_qualified_name: str      # db.schema.table
    columns: list[ColumnSchema]
    primary_keys: list[str]
    row_count_approx: int | None


@dataclass
class RawQuery:
    sql: str
    execution_count: int           # how many times this shape was run
    distinct_users: int
    last_executed_at: str          # ISO datetime
    source: str                    # "query_log" | "dbt" | "looker" | etc.


class SourceType(StrEnum):
    """All recognised evidence sources with their OntoRank authority tier."""

    # Empirical sources — the highest non-steward trust prior: reproduction
    # of published figures against warehouse snapshots outranks any document.
    NUMERIC_FINGERPRINT = "numeric_fingerprint"  # authority: 0.98

    # Structured sources — high trust floor
    DBT_METRIC = "dbt_metric"                  # authority: 1.00
    DBT_MODEL = "dbt_model"                    # authority: 0.85
    DATA_DICTIONARY = "data_dictionary"        # authority: 0.85
    TABLEAU_CALC = "tableau_calc"              # authority: 0.85 (near-executable)
    DDL_CONSTRAINT = "ddl_constraint"          # authority: 0.75
    LOOKER_MEASURE = "looker_measure"          # authority: 0.80
    TABLEAU_FIELD = "tableau_field"            # authority: 0.78

    # Document sources — medium trust, boosted by approval signals
    BRD_APPROVED = "brd_approved"              # authority: 0.90
    BRD_DRAFT = "brd_draft"                    # authority: 0.65
    EXCEL_NAMED = "excel_named"                # authority: 0.70
    EXCEL_FORMULA = "excel_formula"            # authority: 0.50
    PDF_REPORT = "pdf_report"                  # authority: 0.55
    CONFLUENCE_PAGE = "confluence_page"        # authority: 0.60

    # Inferred sources — lower trust floor
    DDL_NAMING = "ddl_naming_convention"       # authority: 0.45
    QUERY_LOG_COMPLEX = "query_log_complex"    # authority: 0.60
    QUERY_LOG_SIMPLE = "query_log_simple"      # authority: 0.40
    AD_HOC = "ad_hoc"                          # authority: 0.20


@dataclass
class DDLColumnCandidate:
    """A single column extracted from DDL with semantic classification."""

    name: str
    raw_type: str                   # original SQL type string
    normalized_type: str            # string | number | time | boolean
    is_nullable: bool
    inferred_role: str              # measure_input | dimension | identifier | flag | unknown
    inference_reason: str           # human-readable: "suffix _amt -> measure_input"
    default_value: str | None
    inline_comment: str | None


@dataclass
class DDLTableEvidence:
    """
    Structured evidence extracted from a raw DDL CREATE TABLE statement.
    Produced by DDLExtractor. Feeds into EvidenceBundle alongside
    AggregationCandidate / DimensionCandidate from the SQL extractor.
    """

    table_name: str
    column_candidates: list[DDLColumnCandidate]
    pk_columns: list[str]           # from CONSTRAINT PRIMARY KEY or inline PK
    fk_pairs: list[tuple[str, str, str]]  # (local_col, ref_table, ref_col)
    check_constraints: list[str]    # raw SQL expressions from CHECK(...)
    inline_comments: dict[str, str] # column_name -> COMMENT text if present
    source_file: str                # path to the DDL file


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
    grounding_confidence: float     # 0.0-1.0: how well it mapped to real schema
    ambiguous: bool                 # True if LLM flagged it as unclear


class Connector(ABC):
    @abstractmethod
    def get_schemas(self) -> list[TableSchema]: ...

    @abstractmethod
    def get_query_log(self) -> list[RawQuery]: ...

    @abstractmethod
    def watch(self, callback: Callable[[Any], None]) -> None:
        """Event-driven mode: call callback(signal) when new signal arrives."""
        ...

    def get_ddl_evidence(self) -> list[DDLTableEvidence]:
        """
        Extract structured evidence from DDL.
        Connectors that don't have DDL return [] (the default here) rather
        than being forced to implement this — most existing connectors
        (warehouse, query log, dbt manifest) have nothing to contribute.
        """
        return []
