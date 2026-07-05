from pathlib import Path

import pytest

from canoniq.ingest.base import RawQuery
from canoniq.ingest.query_log import QueryLogFileConnector, parameterized_hash

QUERIES_FIXTURE = Path(__file__).parent / "fixtures" / "tpcds_queries.sql"


@pytest.fixture
def connector() -> QueryLogFileConnector:
    return QueryLogFileConnector(str(QUERIES_FIXTURE))


def test_all_99_statements_are_grouped(connector: QueryLogFileConnector) -> None:
    queries = connector.get_query_log()
    assert all(isinstance(q, RawQuery) for q in queries)
    assert sum(q.execution_count for q in queries) == 99


def test_query_shapes_collapse_into_groups(connector: QueryLogFileConnector) -> None:
    queries = connector.get_query_log()
    # 14 repeated shapes + 5 unique one-offs = 19 distinct groups
    assert len(queries) == 19


def test_execution_counts_match_expected_group_sizes(connector: QueryLogFileConnector) -> None:
    queries = connector.get_query_log()
    counts = sorted((q.execution_count for q in queries), reverse=True)
    assert counts == [12, 9, 9, 8, 8, 7, 7, 6, 6, 5, 5, 5, 4, 3, 1, 1, 1, 1, 1]


def test_all_queries_have_source_and_metadata(connector: QueryLogFileConnector) -> None:
    queries = connector.get_query_log()
    for q in queries:
        assert q.source == "query_log"
        assert q.distinct_users == 1
        assert q.last_executed_at  # non-empty ISO datetime string
        assert q.sql.strip()


def test_year_shape_groups_despite_different_literals(connector: QueryLogFileConnector) -> None:
    queries = connector.get_query_log()
    year_group = next(q for q in queries if "d_year" in q.sql and "GROUP BY d_year" in q.sql)
    assert year_group.execution_count == 12


def test_parameterized_hash_ignores_literal_values() -> None:
    h1 = parameterized_hash("SELECT SUM(ss_net_profit) FROM store_sales WHERE ss_store_sk = 1")
    h2 = parameterized_hash("SELECT SUM(ss_net_profit) FROM store_sales WHERE ss_store_sk = 2")
    assert h1 == h2


def test_parameterized_hash_distinguishes_different_shapes() -> None:
    h1 = parameterized_hash("SELECT SUM(ss_net_profit) FROM store_sales")
    h2 = parameterized_hash("SELECT COUNT(*) FROM store_sales")
    assert h1 != h2


def test_parameterized_hash_returns_none_for_invalid_sql() -> None:
    assert parameterized_hash("SELECT 'unterminated") is None


def test_invalid_statement_is_skipped(tmp_path: Path) -> None:
    log_file = tmp_path / "queries.sql"
    log_file.write_text("SELECT SUM(x) FROM t; SELECT 'unterminated; SELECT COUNT(*) FROM t;")
    result = QueryLogFileConnector(str(log_file)).get_query_log()
    assert len(result) == 2
    assert sum(q.execution_count for q in result) == 2


def test_get_schemas_not_implemented(connector: QueryLogFileConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.get_schemas()


def test_watch_not_implemented(connector: QueryLogFileConnector) -> None:
    with pytest.raises(NotImplementedError):
        connector.watch(lambda signal: None)
