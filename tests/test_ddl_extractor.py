"""Tests for DDL extractor."""

import os
import tempfile

from canoniq.ingest.ddl_extractor import _infer_role, parse_ddl_file


def test_infer_role_measure():
    assert _infer_role("total_amt", "number") == (
        "measure_input",
        "suffix '_amt' + numeric type → measure_input",
    )


def test_infer_role_time():
    assert _infer_role("created_dt", "string")[0] == "dimension"


def test_infer_role_identifier():
    assert _infer_role("customer_id", "number")[0] == "identifier"


def test_infer_role_flag():
    assert _infer_role("is_active", "boolean")[0] == "flag"


def test_parse_ddl_file_basic():
    ddl = """
    CREATE TABLE orders (
        order_id     BIGINT PRIMARY KEY,
        customer_id  BIGINT NOT NULL,
        order_date   DATE,
        order_amount DECIMAL(10,2),
        status       VARCHAR(20) CHECK (status IN ('pending','completed','cancelled'))
    );
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(ddl)
        path = f.name
    try:
        results = parse_ddl_file(path)
        assert len(results) == 1
        evidence = results[0]
        assert evidence.table_name == "orders"
        assert "order_id" in evidence.pk_columns
        assert len(evidence.check_constraints) >= 1
        roles = {c.name: c.inferred_role for c in evidence.column_candidates}
        assert roles["order_id"] == "identifier"
        assert roles["order_amount"] == "measure_input"
        assert roles["order_date"] == "dimension"
    finally:
        os.unlink(path)
