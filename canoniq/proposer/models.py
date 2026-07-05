"""Pydantic models for LLM-proposed semantic model elements.

These are the structured-output schema Instructor enforces on the Claude
response. Field descriptions double as instructions to the model, so keep
them precise.
"""

from pydantic import BaseModel, Field, field_validator


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
        pattern=r"^[a-z][a-z0-9_]*$",
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

    @field_validator("expression")
    @classmethod
    def expression_must_not_invent_columns(cls, v: str) -> str:
        # Real validation happens downstream against the warehouse schema
        # (see canoniq.proposer.llm.validate_proposal) — a Pydantic
        # validator has no access to the schema, so this is a hook only.
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
