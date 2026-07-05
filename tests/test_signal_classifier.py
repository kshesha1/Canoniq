import pytest

from canoniq.mining.signal_classifier import SignalClass, classify_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(x) FROM t",
        "SELECT COUNT(DISTINCT x) FROM t",
        "SELECT AVG(price) FROM t",
        "SELECT MIN(x), MAX(x) FROM t",
        "SELECT d_year, SUM(ss_net_profit) FROM store_sales "
        "JOIN date_dim ON ss_sold_date_sk = d_date_sk GROUP BY d_year",
    ],
)
def test_aggregation_selects_are_analytical(sql: str) -> None:
    assert classify_sql(sql) == SignalClass.ANALYTICAL


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",
        "SELECT a, b FROM t WHERE a = 1",
        "SELECT a FROM t ORDER BY a LIMIT 10",
    ],
)
def test_non_aggregation_selects_are_noise(sql: str) -> None:
    assert classify_sql(sql) == SignalClass.NOISE


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t (a INT)",
        "INSERT INTO t VALUES (1)",
        "DELETE FROM t",
        "UPDATE t SET x = 1",
    ],
)
def test_ddl_dml_is_structural(sql: str) -> None:
    assert classify_sql(sql) == SignalClass.STRUCTURAL


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "SELECT 'unterminated",
        "not even sql &&&",
    ],
)
def test_unparseable_sql_is_noise(sql: str) -> None:
    assert classify_sql(sql) == SignalClass.NOISE


def test_window_aggregation_is_analytical() -> None:
    sql = "SELECT SUM(x) OVER (PARTITION BY y) FROM t"
    assert classify_sql(sql) == SignalClass.ANALYTICAL
