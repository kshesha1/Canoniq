# canoniq — Semantic Layer Authoring Agent
## Full Build Specification for Claude Code

---

## 0. Context and framing

### What this is
`canoniq` is an open-source, warehouse-agnostic semantic layer authoring agent.
It mines a warehouse schema and SQL query logs, scores every metric candidate
by trust (OntoRank), and emits validated, compiler-checked dbt MetricFlow YAML
and OSI v1.0 YAML — the portable, git-committable specs no existing tool
auto-generates from query patterns.

### What it is NOT
- Not a runtime query engine (that is ktx's job)
- Not a BI tool or dashboard
- Not a chatbot or NL-to-SQL query layer
- Not Snowflake/Databricks-locked

### Competitive position
| Tool | Query-log mining | Portable spec output | Open source |
|------|-----------------|---------------------|-------------|
| Snowflake Semantic View Autopilot | ✅ | ❌ (Snowflake-only) | ❌ |
| ktx (Kaelio) | Partial | ❌ (ktx-native YAML) | ✅ |
| dbt Wizard | ❌ (schema only) | ✅ MetricFlow | ❌ (Cloud only) |
| **canoniq** | ✅ | ✅ MetricFlow + OSI | ✅ |

### Primary differentiators
1. **Query-log frequency mining** — ranks SQL aggregation patterns by how
   often analysts actually run them (Snowflake Autopilot style, open-source)
2. **OntoRank trust scoring** — every metric candidate gets a 5-signal trust
   score before the LLM sees it; sources are not treated equally
3. **Compiler-validated output** — generated YAML goes through `mf validate`
   in a self-correction loop; cannot emit YAML that fails compilation
4. **Multi-spec emission** — one mining run → MetricFlow YAML + OSI v1.0 YAML
5. **Event-driven continuous learning** — new signals (query logs, dbt runs,
   dashboards) trigger ingest automatically; not a manual batch pull

### Inspiration and prior art (be honest in README)
- Architecturally inspired by **ktx** (github.com/Kaelio/ktx, Apache 2.0) —
  specifically the hybrid wiki retrieval pattern, confidence-scored join
  detection, and ingest-as-git-PR design. canoniq is a separate initiative
  focused on one-shot portable YAML generation, not a runtime context layer.
- Snowflake Semantic View Autopilot — closest prior art for query-log mining;
  closed-source and Snowflake-locked.
- Genie Ontology (Databricks) — inspiration for OntoRank and continuous
  learning; closed-source and Databricks-locked.
- DBAutoDoc (arxiv 2603.23050) — 6-phase pipeline architecture reference.
- OSI v1.0 spec (github.com/open-semantic-interchange/OSI, Apache 2.0).

---

## 1. Repository layout

```
canoniq/
├── SPEC.md                        ← this file
├── README.md                      ← generated last
├── pyproject.toml
├── canoniq/
│   ├── __init__.py
│   ├── cli.py                     ← entry point: `canoniq run`
│   ├── config.py                  ← Config dataclass, loads canoniq.yaml
│   │
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── base.py                ← abstract Connector base class
│   │   ├── warehouse.py           ← schema introspection via information_schema
│   │   ├── query_log.py           ← SQL log reader / Snowflake QUERY_HISTORY
│   │   ├── dbt_manifest.py        ← parse dbt manifest.json
│   │   └── watcher.py             ← event-driven file/poll watcher loop
│   │
│   ├── mining/
│   │   ├── __init__.py
│   │   ├── sql_extractor.py       ← sqlglot AST → candidates
│   │   ├── signal_classifier.py   ← cheap noise gate before full pipeline
│   │   └── evidence_bundle.py     ← assemble + deduplicate candidates
│   │
│   ├── ranking/
│   │   ├── __init__.py
│   │   └── ontorank.py            ← 5-signal trust scorer
│   │
│   ├── proposer/
│   │   ├── __init__.py
│   │   ├── models.py              ← Pydantic: MetricProposal, EvidenceItem,
│   │   │                             Conflict, SemanticModelProposal
│   │   └── llm.py                 ← Instructor + Claude → grounded proposals
│   │
│   ├── emitters/
│   │   ├── __init__.py
│   │   ├── metricflow.py          ← → dbt MetricFlow YAML
│   │   ├── osi.py                 ← → OSI v1.0 YAML (already prototyped)
│   │   └── cube.py                ← → Cube.dev YAML (stretch goal)
│   │
│   ├── validation/
│   │   ├── __init__.py
│   │   └── loop.py                ← LangGraph: generate → mf validate → retry
│   │
│   └── evals/
│       ├── __init__.py
│       ├── harness.py             ← run mf query vs gold SQL, score results
│       └── tpcds_gold.py          ← TPC-DS gold queries + expected answers
│
├── tests/
│   ├── fixtures/
│   │   ├── tpcds_schema.sql       ← TPC-DS DDL for DuckDB
│   │   ├── tpcds_queries.sql      ← TPC-DS 99 canonical queries (synthetic log)
│   │   └── sample_manifest.json   ← minimal dbt manifest fixture
│   ├── test_sql_extractor.py
│   ├── test_ontorank.py
│   ├── test_emitters.py
│   └── test_validation_loop.py
│
└── examples/
    ├── tpcds_duckdb/              ← end-to-end demo on TPC-DS
    └── canoniq.yaml.example        ← annotated config file
```

---

## 2. Configuration

### canoniq.yaml (user-facing config)

```yaml
project_name: my_semantic_model
warehouse:
  type: duckdb                     # duckdb | snowflake | bigquery | trino
  path: ./warehouse.db             # DuckDB only
  # For Snowflake:
  # account: xy12345.us-east-1
  # user: ${SNOWFLAKE_USER}
  # password: ${SNOWFLAKE_PASSWORD}
  # database: ANALYTICS
  # schema: PUBLIC
  # warehouse: COMPUTE_WH

query_log:
  type: file                       # file | snowflake_history | trino_history
  path: ./queries.sql              # file type: one SQL statement per line
  # For Snowflake:
  # lookback_days: 90
  # min_execution_count: 3        # ignore queries run fewer than N times

sources:
  dbt_manifest: ./target/manifest.json   # optional

output:
  formats: [metricflow, osi]       # which specs to emit
  dir: ./canoniq_output/

ontorank:
  weights:
    source_authority: 0.30
    usage_frequency: 0.25
    cross_source_agreement: 0.20
    recency: 0.15
    certification_status: 0.10
  thresholds:
    auto_merge: 0.85               # above this → write without human review
    review: 0.50                   # between review and auto_merge → queue for review
    drop: 0.50                     # below this → discard silently

llm:
  provider: anthropic              # anthropic | openai
  model: claude-sonnet-4-6
  max_retries: 3                   # validation loop max attempts

continuous:
  enabled: false                   # set true to run event-driven watcher
  poll_interval_seconds: 300       # how often to check query log for new queries
```

### Config dataclass (config.py)

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class OntoRankWeights:
    source_authority: float = 0.30
    usage_frequency: float = 0.25
    cross_source_agreement: float = 0.20
    recency: float = 0.15
    certification_status: float = 0.10

@dataclass
class OntoRankThresholds:
    auto_merge: float = 0.85
    review: float = 0.50
    drop: float = 0.50

@dataclass
class Config:
    project_name: str
    warehouse_type: Literal["duckdb", "snowflake", "bigquery", "trino"]
    output_formats: list[str]
    output_dir: str
    ontorank_weights: OntoRankWeights = field(default_factory=OntoRankWeights)
    ontorank_thresholds: OntoRankThresholds = field(default_factory=OntoRankThresholds)
    llm_model: str = "claude-sonnet-4-6"
    llm_max_retries: int = 3
    continuous_enabled: bool = False
    poll_interval_seconds: int = 300

def load_config(path: str = "canoniq.yaml") -> Config:
    """Load and validate canoniq.yaml into a Config instance."""
    ...
```

---

## 3. Layer-by-layer implementation spec

---

### Layer 1 — Ingest (`canoniq/ingest/`)

#### 3.1 Base connector (`base.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class TableSchema:
    fully_qualified_name: str      # db.schema.table
    columns: list[ColumnSchema]
    primary_keys: list[str]
    row_count_approx: int | None

@dataclass
class ColumnSchema:
    name: str
    data_type: str                 # normalized: string | number | time | boolean
    is_nullable: bool
    sample_values: list            # up to 5 distinct values
    cardinality_approx: int | None # None if unknown

@dataclass
class RawQuery:
    sql: str
    execution_count: int           # how many times this shape was run
    distinct_users: int
    last_executed_at: str          # ISO datetime
    source: str                    # "query_log" | "dbt" | "looker" | etc.

class Connector(ABC):
    @abstractmethod
    def get_schemas(self) -> list[TableSchema]: ...

    @abstractmethod
    def get_query_log(self) -> list[RawQuery]: ...

    @abstractmethod
    def watch(self, callback) -> None:
        """Event-driven mode: call callback(signal) when new signal arrives."""
        ...
```

#### 3.2 Warehouse connector (`warehouse.py`)

- Connect to the configured warehouse (DuckDB for v0, Snowflake next)
- Run `information_schema.columns`, `information_schema.table_constraints`
  to build `TableSchema` objects
- For DuckDB: use `duckdb.connect(path)` and `PRAGMA database_list`
- For Snowflake: use `snowflake-connector-python`
- Normalize all column types to `string | number | time | boolean`
- Sample up to 5 distinct values per column for context
- Infer primary keys from `information_schema.table_constraints` first;
  fall back to name-heuristic (`*_id` columns with high cardinality)

#### 3.3 Query log connector (`query_log.py`)

Two modes:

**File mode** (v0 — for DuckDB demo):
```
- Read a flat .sql file, one statement per line (or semicolon-separated)
- Parse each with sqlglot to validate it is syntactically valid SQL
- Normalize / parameterize: strip string literals, numeric literals
- Group by parameterized hash (same query shape = same group)
- Assign execution_count = group size, last_executed_at = now()
```

**Snowflake mode** (week 2):
```sql
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(QUERY_TEXT) AS query_text,
    COUNT(*) AS execution_count,
    COUNT(DISTINCT USER_NAME) AS distinct_users,
    MAX(START_TIME) AS last_executed_at
FROM SNOWFLAKE.ACCOUNT_USAGE.AGGREGATE_QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
  AND QUERY_TYPE = 'SELECT'
  AND EXECUTION_STATUS = 'SUCCESS'
GROUP BY QUERY_PARAMETERIZED_HASH
HAVING execution_count >= 3
ORDER BY execution_count DESC
```

#### 3.4 dbt manifest connector (`dbt_manifest.py`)

- Load `target/manifest.json` (dbt Core output, always available locally)
- Extract: model names, column descriptions, metric definitions
  (MetricFlow `semantic_models` and `metrics` nodes)
- Map to internal `TableSchema` + `RawQuery` representations
- If a metric is already defined in dbt → mark as `certified: true`
  (this is the highest OntoRank signal)

#### 3.5 Event-driven watcher (`watcher.py`)

```python
import time
from pathlib import Path

class SignalWatcher:
    """
    Polls configured sources for new signals and emits them to the pipeline.
    Runs in a background thread when continuous mode is enabled.
    """
    def __init__(self, config: Config, connectors: list[Connector]):
        self.config = config
        self.connectors = connectors
        self._seen_hashes: set[str] = set()   # deduplicate signals

    def run_once(self) -> list[RawSignal]:
        """Single poll pass — usable in batch mode too."""
        ...

    def run_forever(self, callback) -> None:
        """Event loop: poll every poll_interval_seconds."""
        while True:
            new_signals = self.run_once()
            for signal in new_signals:
                callback(signal)
            time.sleep(self.config.poll_interval_seconds)
```

**v0 sources to implement:**
1. `WarehouseQueryLogConnector` — polls query log file for new SQL shapes
2. `DbtManifestConnector` — file-watches `manifest.json` for changes

**Deferred to week 2:**
- Tableau LangChain connector
- Looker MCP connector (via looker-mcp-server)
- Notion API connector
- Slack webhook receiver

---

### Layer 2 — Mining (`canoniq/mining/`)

#### 3.6 Signal classifier (`signal_classifier.py`)

Cheap noise gate — runs BEFORE the expensive sqlglot extraction.

```python
import sqlglot
from sqlglot import expressions as exp

class SignalClass(Enum):
    ANALYTICAL = "analytical"      # has aggregations → worth processing
    STRUCTURAL = "structural"      # DDL/DML → skip
    NOISE = "noise"                # no meaningful signal

def classify_sql(sql: str) -> SignalClass:
    try:
        tree = sqlglot.parse_one(sql, error_level="raise")
    except Exception:
        return SignalClass.NOISE

    # Must be a SELECT
    if not isinstance(tree, exp.Select):
        return SignalClass.STRUCTURAL

    # Must have at least one aggregation function
    agg_funcs = list(tree.find_all(exp.AggFunc))
    if not agg_funcs:
        return SignalClass.NOISE

    return SignalClass.ANALYTICAL
```

#### 3.7 SQL extractor (`sql_extractor.py`)

The core mining logic. Takes a `RawQuery` that passed classification
and extracts structured candidates using sqlglot's AST.

```python
@dataclass
class AggregationCandidate:
    """A candidate metric extracted from a SQL aggregation expression."""
    expression: str                # e.g. "SUM(o.total_amount)"
    source_table: str              # resolved table name
    source_column: str             # resolved column name
    agg_function: str              # SUM | COUNT | AVG | MIN | MAX | COUNT_DISTINCT
    filter_expr: str | None        # e.g. "status = 'completed'" if WHERE applied
    seen_in_queries: list[str]     # query hashes where this appeared
    execution_count: int           # total executions across all queries

@dataclass
class DimensionCandidate:
    """A candidate dimension from GROUP BY and WHERE columns."""
    column: str
    table: str
    is_time: bool                  # True if column type is time/date
    seen_in_queries: list[str]

@dataclass
class JoinCandidate:
    """A candidate join relationship extracted from JOIN clauses."""
    from_table: str
    to_table: str
    from_column: str
    to_column: str
    join_type: str                 # LEFT | INNER | etc.
    seen_in_queries: list[str]

def extract_candidates(
    query: RawQuery,
    schemas: dict[str, TableSchema],
    dialect: str = "duckdb"
) -> tuple[list[AggregationCandidate],
           list[DimensionCandidate],
           list[JoinCandidate]]:
    """
    Parse a single SQL query and extract all candidates.

    Implementation notes:
    - Use sqlglot.parse_one(sql, dialect=dialect)
    - Use sqlglot.optimizer.qualify to resolve table aliases
      before extracting column references
    - For aggregations: find all exp.AggFunc nodes, resolve
      their column arguments to fully-qualified table.column
    - For dimensions: find exp.Group nodes, extract all column
      references, mark is_time=True if column data_type is "time"
    - For joins: find exp.Join nodes, extract ON clause column pairs
    - Use schemas dict to resolve and validate that extracted
      table/column names actually exist in the warehouse
    - If a column cannot be resolved → log warning, skip candidate
      (never emit unresolvable column names to the LLM)
    """
    ...
```

**Key implementation constraint:** Every column name emitted by the extractor
MUST exist in the `schemas` dict. This is the primary anti-hallucination
guardrail — the LLM proposer only sees real column names.

#### 3.8 Evidence bundle (`evidence_bundle.py`)

Aggregates candidates across all queries into a ranked bundle per table.

```python
@dataclass
class MetricEvidence:
    """All evidence supporting a single metric candidate."""
    expression: str                # canonical SQL expression
    source_table: str
    execution_count: int           # total across all queries
    distinct_users: int
    last_seen_at: str
    source_types: list[str]        # ["query_log", "dbt", "looker"]
    is_certified: bool             # defined in dbt with passing tests
    filter_variants: list[str]     # different WHERE clauses seen with this expr

@dataclass
class EvidenceBundle:
    """Complete evidence for one table, ready for OntoRank + LLM."""
    table: TableSchema
    metric_candidates: list[MetricEvidence]
    dimension_candidates: list[DimensionCandidate]
    join_candidates: list[JoinCandidate]

def build_evidence_bundle(
    table: TableSchema,
    agg_candidates: list[AggregationCandidate],
    dim_candidates: list[DimensionCandidate],
    join_candidates: list[JoinCandidate],
    dbt_metrics: list[dict] | None = None,
) -> EvidenceBundle:
    """
    Merge and deduplicate candidates by expression.
    Group identical expressions (modulo whitespace/alias) together.
    Sum execution_counts, union source_types, pick latest last_seen_at.
    Mark is_certified=True if expression appears in dbt_metrics.
    """
    ...
```

---

### Layer 3 — Ranking (`canoniq/ranking/`)

#### 3.9 OntoRank scorer (`ontorank.py`)

```python
from datetime import datetime, timezone
from math import log

SOURCE_AUTHORITY = {
    "dbt_metric":      1.00,   # explicit human-authored definition
    "dbt_model":       0.85,   # dbt model column description
    "looker":          0.80,   # Looker measure in a dashboard
    "tableau":         0.78,
    "query_log_complex": 0.60, # analytical query (≥2 agg functions)
    "query_log_simple":  0.40, # single aggregation
    "notion":          0.50,
    "slack":           0.30,
    "ad_hoc":          0.20,
}

@dataclass
class OntoRankScore:
    total: float                   # 0.0–1.0 weighted sum
    source_authority: float
    usage_frequency: float
    cross_source_agreement: float
    recency: float
    certification_status: float
    evidence_summary: str          # human-readable justification

def score(
    evidence: MetricEvidence,
    weights: OntoRankWeights,
    max_execution_count: int,      # normalize against dataset max
) -> OntoRankScore:
    """
    Compute OntoRank trust score for a metric candidate.

    Signal 1: source_authority
        max(SOURCE_AUTHORITY[s] for s in evidence.source_types)

    Signal 2: usage_frequency
        log(1 + evidence.execution_count) / log(1 + max_execution_count)
        (log-normalized so a 1000-count metric isn't 100x a 10-count one)

    Signal 3: cross_source_agreement
        len(evidence.source_types) / 4.0  (capped at 1.0)
        (appears in dbt AND query_log AND looker → 0.75)

    Signal 4: recency
        days_since = (now - last_seen_at).days
        1.0 if days_since <= 7
        0.8 if days_since <= 30
        0.5 if days_since <= 90
        0.2 if days_since <= 365
        0.0 otherwise

    Signal 5: certification_status
        1.0 if evidence.is_certified else 0.0

    total = sum(signal_i * weight_i for each signal)
    """
    ...
```

---

### Layer 4 — Proposer (`canoniq/proposer/`)

#### 3.10 Pydantic models (`models.py`)

```python
from pydantic import BaseModel, Field, validator

class EvidenceItem(BaseModel):
    source: str                    # "query_log", "dbt", "looker", etc.
    description: str               # human-readable evidence description
    execution_count: int
    trust_contribution: float      # this source's contribution to total score

class Conflict(BaseModel):
    conflicting_expression: str    # alternative formula seen elsewhere
    source: str
    trust_score: float
    description: str               # plain English description of the conflict

class MetricProposal(BaseModel):
    name: str = Field(
        description="snake_case metric name, no spaces, descriptive",
        pattern=r"^[a-z][a-z0-9_]*$"
    )
    description: str = Field(
        description="Plain English definition, 1-2 sentences. Include "
                    "caveats (e.g. 'excludes cancelled orders'). "
                    "Do not start with 'This metric'."
    )
    expression: str = Field(
        description="SQL aggregation expression using only column names "
                    "confirmed to exist in the schema. No invented columns."
    )
    metric_type: str = Field(
        description="One of: sum | count | average | ratio | derived"
    )
    synonyms: list[str] = Field(
        description="Other names people use for this metric in "
                    "meetings, dashboards, or emails. 2-5 terms."
    )
    evidence: list[EvidenceItem]
    trust_score: float
    conflicts: list[Conflict] = Field(default_factory=list)

    @validator("expression")
    def expression_must_not_invent_columns(cls, v, values):
        # Validated downstream against schema; placeholder hook
        return v

class DimensionProposal(BaseModel):
    name: str
    column: str
    table: str
    is_time: bool
    description: str
    synonyms: list[str]

class EntityProposal(BaseModel):
    name: str
    column: str
    table: str
    entity_type: str               # primary | foreign | unique
    description: str

class SemanticModelProposal(BaseModel):
    """Top-level proposal for one table's semantic model."""
    dataset_name: str
    source_table: str
    grain_description: str
    primary_key: list[str]
    entities: list[EntityProposal]
    dimensions: list[DimensionProposal]
    metrics: list[MetricProposal]
    joins: list[dict]              # from JoinCandidates
    overall_trust_score: float
    review_required: bool          # True if any metric below auto_merge threshold
```

#### 3.11 LLM proposer (`llm.py`)

```python
import anthropic
import instructor
from canoniq.proposer.models import SemanticModelProposal

def build_system_prompt() -> str:
    return """
You are a semantic layer architect. Your job is to propose named, described
metric and dimension definitions from SQL evidence.

CRITICAL RULES:
1. NEVER invent column names. Only use column names from the provided schema.
2. NEVER invent table names. Only use table names from the provided schema.
3. ALL expressions must use only columns confirmed to exist in the schema.
4. Descriptions must be plain English. No technical jargon. No "This metric".
5. Synonyms must reflect how business users actually talk about this number
   in meetings — not technical aliases.
6. If evidence is ambiguous or conflicting, surface the conflict in the
   conflicts field with both alternatives ranked by trust score.
7. metric_type must be one of: sum | count | average | ratio | derived.

You are generating YAML that will be compiled by the dbt MetricFlow compiler.
Output must be parseable and correct.
"""

def propose(
    evidence_bundle: EvidenceBundle,
    scored_metrics: list[tuple[MetricEvidence, OntoRankScore]],
    config: Config,
) -> SemanticModelProposal:
    """
    Ground the LLM in real schema evidence, then propose the semantic model.

    Prompt construction:
    1. Include the full TableSchema (name, columns, types, PKs)
    2. Include the top-N metric candidates (by trust score) with their
       evidence summaries — do NOT include all candidates, only those
       above the drop threshold
    3. Include dimension candidates grouped by is_time
    4. Include join candidates with relationship type hints
    5. Ask the model to return a SemanticModelProposal (Instructor enforces schema)

    Use instructor.patch(anthropic.Anthropic()) for structured output.
    Set max_tokens=4096.
    Temperature=0 for deterministic output (this is a generation task,
    not a creative one).
    """
    client = instructor.from_anthropic(anthropic.Anthropic())

    prompt = _build_user_prompt(evidence_bundle, scored_metrics)

    proposal = client.messages.create(
        model=config.llm_model,
        max_tokens=4096,
        temperature=0,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
        response_model=SemanticModelProposal,
    )
    return proposal

def _build_user_prompt(
    bundle: EvidenceBundle,
    scored_metrics: list[tuple[MetricEvidence, OntoRankScore]],
) -> str:
    """
    Build the grounded user prompt. Structure:

    === TABLE SCHEMA ===
    Table: {fully_qualified_name}
    Grain: {row_count_approx} rows
    Columns:
      - order_id (string, NOT NULL, high cardinality) — sample: [1001, 1002, ...]
      - order_date (time, NOT NULL)
      - status (string) — sample: ['completed', 'cancelled', 'pending']
      - total_amount (number, NOT NULL)

    === METRIC EVIDENCE (ranked by trust score) ===
    1. SUM(total_amount) — trust: 0.92
       Sources: dbt_metric (certified), query_log (312 executions), looker (47 dashboards)
       Last seen: 2026-06-15
       Filter variants: ["status = 'completed'", None]

    2. COUNT(DISTINCT order_id) — trust: 0.81
       ...

    === DIMENSION EVIDENCE ===
    Time dimensions: order_date (312 queries), created_at (41 queries)
    Categorical: status (278 queries), region (156 queries)

    === JOIN EVIDENCE ===
    orders.customer_id → customers.customer_id (312 occurrences, LEFT JOIN)

    === YOUR TASK ===
    Propose a complete semantic model for this table. For each metric:
    - Give it a clear snake_case name
    - Write a plain-English description (include caveats if filter variants differ)
    - Use ONLY the column names shown above
    - List 2-5 synonyms business users would use
    - If you see conflicting definitions, surface them in conflicts[]
    """
    ...
```

---

### Layer 5 — Emitters (`canoniq/emitters/`)

#### 3.12 MetricFlow emitter (`metricflow.py`)

Converts a `SemanticModelProposal` into dbt MetricFlow YAML.

Target output format:

```yaml
# Auto-generated by canoniq — review before committing
# Trust score: 0.91 | Generated: 2026-07-01T14:32:00Z

semantic_models:
  - name: orders
    description: "One row per customer order"
    model: ref('orders')

    entities:
      - name: order_id
        type: primary
        expr: order_id
      - name: customer
        type: foreign
        expr: customer_id

    dimensions:
      - name: order_date
        type: time
        type_params:
          time_granularity: day
        expr: order_date
        description: "Date the order was placed"

      - name: status
        type: categorical
        expr: status
        description: "Order status: completed, cancelled, pending"

    measures:
      - name: total_amount_sum
        agg: sum
        expr: total_amount
        description: "Sum of order amounts (before tax)"
        create_metric: false      # metric defined separately below

metrics:
  - name: total_revenue
    description: "Total order revenue, before tax. Excludes cancelled orders."
    type: simple
    type_params:
      measure: total_amount_sum
    filter: "{{ Dimension('orders__status') }} = 'completed'"
    meta:
      canoniq_trust_score: 0.92
      canoniq_evidence: "dbt_metric (certified), 312 query-log runs, 47 Looker dashboards"
      canoniq_synonyms: ["total sales", "revenue", "gross sales"]

  - name: order_count
    description: "Number of distinct orders"
    type: simple
    type_params:
      measure: order_id_count
    meta:
      canoniq_trust_score: 0.81
      canoniq_synonyms: ["number of orders", "order volume"]
```

Implementation notes:
- Use PyYAML with custom Dumper for clean indentation
- Include `meta.canoniq_trust_score` and `meta.canoniq_evidence` on every
  metric so the human reviewer can see why each was proposed
- Sort metrics by trust_score descending (highest confidence first)
- Add a `# REVIEW REQUIRED` comment on metrics below the auto_merge threshold
- The `model: ref('{name}')` assumes a dbt model with the same name exists;
  emit a warning if no dbt manifest was provided

#### 3.13 OSI emitter (`osi.py`)

Already prototyped (from existing excel_to_osi.py work). Adapt to accept
`SemanticModelProposal` as input instead of Excel rows.

Target output format follows OSI v1.0 spec:

```yaml
# yaml-language-server: $schema=../core-spec/osi-schema.json
version: "0.1.1"

semantic_model:
  - name: orders_model
    description: "Order-level sales analytics"
    ai_context:
      instructions: "Use this model to answer questions about orders and revenue."

    datasets:
      - name: orders
        source: sales.public.orders
        description: "One row per customer order"
        primary_key: [order_id]
        fields:
          - name: order_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_id
            description: "Unique order identifier"

          - name: order_date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_date
            dimension:
              is_time: true
            description: "Date the order was placed"

    relationships: []

    metrics:
      - name: total_revenue
        expression:
          - dialect: ANSI_SQL
            expression: "SUM(orders.order_amount)"
        description: "Total order revenue, before tax"
        ai_context:
          synonyms: ["total sales", "revenue", "gross sales"]
```

---

### Layer 6 — Validation loop (`canoniq/validation/`)

#### 3.14 LangGraph validation loop (`loop.py`)

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict

class ValidationState(TypedDict):
    proposal: SemanticModelProposal
    yaml_output: str                   # current generated YAML
    validation_errors: list[str]       # errors from last mf validate run
    attempt: int
    passed: bool

def build_validation_graph(config: Config) -> StateGraph:
    """
    Nodes:
      emit        → generate YAML from proposal (MetricFlow emitter)
      validate    → run `mf validate` or `dbt parse`, capture errors
      repair      → if errors: feed back to LLM with error context, regenerate proposal
      accept      → write YAML to output dir, mark passed=True

    Edges:
      emit → validate
      validate → accept (if no errors or attempt >= max_retries)
      validate → repair (if errors and attempt < max_retries)
      repair → emit
      accept → END

    The repair node passes the full error message back to the LLM proposer
    with the instruction: "The following YAML failed dbt MetricFlow validation
    with these errors. Fix only the failing definitions. Keep everything else."
    """
    graph = StateGraph(ValidationState)

    graph.add_node("emit", emit_node)
    graph.add_node("validate", validate_node)
    graph.add_node("repair", repair_node)
    graph.add_node("accept", accept_node)

    graph.set_entry_point("emit")
    graph.add_edge("emit", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda s: "accept" if (s["passed"] or s["attempt"] >= config.llm_max_retries)
                  else "repair"
    )
    graph.add_edge("repair", "emit")
    graph.add_edge("accept", END)

    return graph.compile()

def validate_node(state: ValidationState) -> ValidationState:
    """
    Run MetricFlow validation against generated YAML.

    Preferred: subprocess call to `mf validate-configs --select {model}`
    Fallback (if mf not installed): parse YAML and check structural validity
    against MetricFlow's expected schema using jsonschema.

    Capture stdout/stderr. Any non-zero exit code = validation failure.
    Parse error messages to extract the specific field that failed.
    """
    ...
```

---

### Layer 7 — Evals (`canoniq/evals/`)

#### 3.15 Eval harness (`harness.py`)

```python
@dataclass
class EvalResult:
    question: str
    expected_sql: str
    generated_metric: str | None   # which canoniq metric was used
    result_matches: bool
    error: str | None

def run_eval(
    semantic_model_path: str,      # path to generated MetricFlow YAML
    warehouse_conn,                # DuckDB connection with TPC-DS loaded
    gold_queries: list[GoldQuery],
) -> list[EvalResult]:
    """
    For each gold query:
    1. Run the gold SQL directly → gold_result
    2. Find the closest canoniq metric by semantic similarity
    3. Run: mf query --metrics {metric} --group-by {dims}
    4. Compare results: row count, column values, aggregated totals
    5. Record pass/fail + error if any

    Score: accuracy = passing / total
    Report: print a table of results, save to canoniq_output/eval_results.json
    """
    ...
```

#### 3.16 TPC-DS gold queries (`tpcds_gold.py`)

Start with 10 representative TPC-DS questions mapped to expected MetricFlow
queries. These are the gold standard for the LinkedIn demo.

```python
GOLD_QUERIES = [
    GoldQuery(
        question="Total net sales by year",
        sql="SELECT d_year, SUM(ss_net_profit) FROM store_sales "
            "JOIN date_dim ON ss_sold_date_sk = d_date_sk GROUP BY d_year",
        expected_metric="total_net_sales",
        expected_dimensions=["sold_date__year"],
    ),
    GoldQuery(
        question="Number of distinct customers",
        sql="SELECT COUNT(DISTINCT ss_customer_sk) FROM store_sales",
        expected_metric="customer_count",
        expected_dimensions=[],
    ),
    # ... 8 more covering avg, ratio, time-based patterns
]
```

---

## 4. CLI (`canoniq/cli.py`)

```
canoniq run [--config canoniq.yaml] [--watch]
    Run the full pipeline once (or continuously with --watch).
    Outputs YAML to output.dir.

canoniq mine [--config canoniq.yaml]
    Run ingest + mining + ranking only. Print evidence bundle.
    Useful for debugging what signals were found.

canoniq propose [--config canoniq.yaml] [--table TABLE]
    Run mining + ranking + LLM proposer for one table.
    Print the proposal without emitting files.

canoniq emit [--config canoniq.yaml] [--format metricflow|osi|all]
    Take an existing proposal (from canoniq propose output) and emit YAML.

canoniq eval [--config canoniq.yaml] [--output eval_results.json]
    Run the eval harness against generated YAML. Print accuracy report.

canoniq validate [--yaml PATH]
    Run MetricFlow validation on an existing YAML file.
```

---

## 5. Pydantic models summary (all models in one place)

```python
# canoniq/proposer/models.py — complete list

EvidenceItem(source, description, execution_count, trust_contribution)
Conflict(conflicting_expression, source, trust_score, description)
MetricProposal(name, description, expression, metric_type, synonyms,
               evidence, trust_score, conflicts)
DimensionProposal(name, column, table, is_time, description, synonyms)
EntityProposal(name, column, table, entity_type, description)
SemanticModelProposal(dataset_name, source_table, grain_description,
                      primary_key, entities, dimensions, metrics,
                      joins, overall_trust_score, review_required)

# canoniq/ingest/base.py
ColumnSchema(name, data_type, is_nullable, sample_values, cardinality_approx)
TableSchema(fully_qualified_name, columns, primary_keys, row_count_approx)
RawQuery(sql, execution_count, distinct_users, last_executed_at, source)

# canoniq/mining/sql_extractor.py
AggregationCandidate(expression, source_table, source_column, agg_function,
                     filter_expr, seen_in_queries, execution_count)
DimensionCandidate(column, table, is_time, seen_in_queries)
JoinCandidate(from_table, to_table, from_column, to_column,
              join_type, seen_in_queries)

# canoniq/mining/evidence_bundle.py
MetricEvidence(expression, source_table, execution_count, distinct_users,
               last_seen_at, source_types, is_certified, filter_variants)
EvidenceBundle(table, metric_candidates, dimension_candidates, join_candidates)

# canoniq/ranking/ontorank.py
OntoRankScore(total, source_authority, usage_frequency,
              cross_source_agreement, recency, certification_status,
              evidence_summary)

# canoniq/evals/harness.py
GoldQuery(question, sql, expected_metric, expected_dimensions)
EvalResult(question, expected_sql, generated_metric,
           result_matches, error)
```

---

## 6. Dependencies (`pyproject.toml`)

```toml
[project]
name = "canoniq"
version = "0.1.0"
description = "Open-source semantic layer authoring agent"
requires-python = ">=3.11"

dependencies = [
    # Core parsing
    "sqlglot>=25.0.0",

    # Warehouse connectors
    "duckdb>=0.10.0",
    "snowflake-connector-python>=3.0.0",   # optional, for Snowflake

    # LLM + structured output
    "anthropic>=0.30.0",
    "instructor>=1.3.0",

    # Agent loop
    "langgraph>=0.1.0",

    # YAML + validation
    "pyyaml>=6.0",
    "jsonschema>=4.0",

    # CLI
    "click>=8.0",
    "rich>=13.0",                          # for pretty console output

    # Config
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "ruff",
    "mypy",
]

[project.scripts]
canoniq = "canoniq.cli:main"
```

---

## 7. Build order for Claude Code

Build in this exact order. Each step is independently testable.

### Step 1 — Project scaffold
- Create pyproject.toml, directory structure, __init__.py files
- Set up ruff + mypy config
- Verify: `pip install -e .` works

### Step 2 — Config layer
- Implement `canoniq/config.py` with Config dataclass
- Implement `canoniq.yaml` loading with validation
- Verify: `canoniq.yaml.example` loads without errors

### Step 3 — Ingest: warehouse connector
- Implement `canoniq/ingest/warehouse.py`
- Load TPC-DS schema into DuckDB from `tests/fixtures/tpcds_schema.sql`
- Verify: `get_schemas()` returns correct TableSchema objects for all TPC-DS tables
- Test: `tests/test_warehouse_connector.py`

### Step 4 — Ingest: query log connector
- Implement `canoniq/ingest/query_log.py` (file mode)
- Load `tests/fixtures/tpcds_queries.sql`
- Verify: 99 TPC-DS queries parse, parameterize, and group correctly
- Test: `tests/test_query_log.py`

### Step 5 — Mining: signal classifier
- Implement `canoniq/mining/signal_classifier.py`
- Verify: DDL/DML/simple SELECTs → NOISE, aggregation SELECTs → ANALYTICAL
- Test: `tests/test_signal_classifier.py`

### Step 6 — Mining: SQL extractor (most complex step)
- Implement `canoniq/mining/sql_extractor.py`
- Use sqlglot's `qualify` optimizer to resolve aliases before extraction
- Verify on TPC-DS query 1: correctly extracts SUM, COUNT, GROUP BY columns, JOINs
- Test: `tests/test_sql_extractor.py` with 5 representative TPC-DS queries

### Step 7 — Mining: evidence bundle
- Implement `canoniq/mining/evidence_bundle.py`
- Run extractor across all 99 TPC-DS queries, build bundles per table
- Verify: store_sales table has >5 metric candidates, all with execution_count > 1
- Test: `tests/test_evidence_bundle.py`

### Step 8 — Ranking: OntoRank
- Implement `canoniq/ranking/ontorank.py`
- Score all candidates from Step 7
- Verify: is_certified metrics score > 0.85, single-run ad-hoc < 0.50
- Test: `tests/test_ontorank.py`

### Step 9 — Proposer: models + LLM
- Implement `canoniq/proposer/models.py` (Pydantic models)
- Implement `canoniq/proposer/llm.py` (Instructor + Claude)
- Test with store_sales EvidenceBundle → SemanticModelProposal
- Verify: no invented column names in any emitted expression
- Test: `tests/test_proposer.py` (mock LLM call for unit test)

### Step 10 — Emitters: MetricFlow + OSI
- Implement `canoniq/emitters/metricflow.py`
- Implement `canoniq/emitters/osi.py` (adapt from existing excel_to_osi.py)
- Verify: emitted MetricFlow YAML matches expected schema structure
- Test: `tests/test_emitters.py`

### Step 11 — Validation loop
- Implement `canoniq/validation/loop.py`
- Verify: deliberately malformed YAML triggers repair and corrects itself
- Test: `tests/test_validation_loop.py`

### Step 12 — CLI
- Implement `canoniq/cli.py` with all commands
- Wire together all layers
- End-to-end test: `canoniq run --config examples/tpcds_duckdb/canoniq.yaml`

### Step 13 — Evals
- Implement `canoniq/evals/harness.py`
- Implement `canoniq/evals/tpcds_gold.py` with 10 gold queries
- Run eval, capture accuracy score for LinkedIn post

### Step 14 — Continuous watcher (stretch goal for week 2)
- Implement `canoniq/ingest/watcher.py`
- Wire into CLI `--watch` flag

---

## 8. Testing strategy

### Unit tests (each module independently)
- `test_sql_extractor.py` — 10 SQL inputs → expected candidates
- `test_ontorank.py` — score invariants (certified > non-certified, etc.)
- `test_emitters.py` — proposal → YAML string comparison
- `test_validation_loop.py` — mock mf validate, verify retry behaviour

### Integration test (end-to-end)
- Load TPC-DS into DuckDB
- Run full `canoniq run` pipeline
- Assert: output YAML exists, passes `mf validate` (or structural check),
  eval score > 0.0

### What NOT to test with real LLM calls
- Mock the LLM in all unit tests (use `instructor` mock or saved fixture)
- Only call real LLM in integration test or manual demo runs

---

## 9. Environment variables

```bash
ANTHROPIC_API_KEY=sk-ant-...          # required for proposer
SNOWFLAKE_USER=...                    # optional, Snowflake connector
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_ACCOUNT=...
DBT_PROJECT_DIR=./                    # optional, for manifest discovery
```

---

## 10. Key design constraints for Claude Code

1. **Never emit column names the LLM invented.** Every column in every
   emitted expression must be validated against the schema before the LLM
   prompt is constructed. If a column can't be resolved → skip the candidate,
   log a warning, never pass it to the LLM.

2. **sqlglot qualify before extracting.** Always run
   `sqlglot.optimizer.qualify.qualify(tree, schema=schemas)` before
   extracting column references. Bare column names without table context
   are ambiguous and will produce wrong join candidates.

3. **Temperature=0 for all LLM proposer calls.** This is a structured
   generation task, not a creative one. Determinism matters.

4. **Instructor, not raw JSON parsing.** Use `instructor.from_anthropic()`
   for all LLM calls. Never parse raw JSON from LLM output manually.

5. **Validation loop has a hard retry cap.** `max_retries` from config,
   default 3. After the cap, emit the best-attempt YAML with a
   `# VALIDATION FAILED — manual review required` header comment.

6. **Evidence cards on every metric.** Every emitted metric must have
   `meta.canoniq_trust_score` and `meta.canoniq_evidence` populated.
   This is the human reviewer's primary audit trail.

7. **OSI emitter reuses existing work.** The `excel_to_osi.py` conversion
   script already handles the OSI v1.0 YAML format. The OSI emitter
   adapts this, taking `SemanticModelProposal` instead of Excel rows.
