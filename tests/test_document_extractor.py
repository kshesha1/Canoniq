"""Tests for document extractor — approval detection and grounding."""

from canoniq.ingest.base import ColumnSchema, TableSchema
from canoniq.ingest.document_extractor import (
    _ground_candidate,
    _RawExtractedMetric,
    has_approval_signal,
)


def _make_schema():
    return [
        TableSchema(
            fully_qualified_name="main.orders",
            primary_keys=["order_id"],
            row_count_approx=1000,
            columns=[
                ColumnSchema("order_id", "number", False, [], 1000),
                ColumnSchema("order_amount", "number", True, [], 500),
                ColumnSchema("status", "string", True, ["completed", "pending"], 3),
            ],
        )
    ]


def test_approval_signal_detected():
    assert has_approval_signal("Approved by: John Smith, CFO") is True
    assert has_approval_signal("Just a regular paragraph") is False


def test_grounding_finds_column():
    raw = _RawExtractedMetric(
        raw_name="Total Revenue",
        raw_definition="sum of order_amount for completed orders",
        raw_filter="completed orders",
    )
    expr, table, conf = _ground_candidate(raw, _make_schema())
    assert expr is not None
    assert "order_amount" in expr
    assert conf > 0.0


def test_grounding_no_match():
    raw = _RawExtractedMetric(
        raw_name="EBITDA",
        raw_definition="earnings before interest taxes depreciation",
    )
    expr, table, conf = _ground_candidate(raw, _make_schema())
    assert expr is None
    assert conf == 0.0
