"""TPC-DS-style gold queries for the LinkedIn demo eval.

These are synthetic, schema-accurate gold questions matched to the same
tables/columns used throughout canoniq's own TPC-DS test fixtures
(tests/fixtures/tpcds_schema.sql, tests/fixtures/tpcds_queries.sql), not a
verbatim transcription of the official 99-query TPC-DS benchmark — see the
note on tests/fixtures/tpcds_queries.sql for why.
"""

from dataclasses import dataclass


@dataclass
class GoldQuery:
    question: str
    sql: str
    expected_metric: str
    expected_dimensions: list[str]


GOLD_QUERIES: list[GoldQuery] = [
    GoldQuery(
        question="Total net profit by year",
        sql=(
            "SELECT d_year, SUM(ss_net_profit) FROM store_sales "
            "JOIN date_dim ON ss_sold_date_sk = d_date_sk GROUP BY d_year"
        ),
        expected_metric="total_net_profit",
        expected_dimensions=["d_year"],
    ),
    GoldQuery(
        question="Number of distinct customers",
        sql="SELECT COUNT(DISTINCT ss_customer_sk) FROM store_sales",
        expected_metric="distinct_customers",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Average sales price",
        sql="SELECT AVG(ss_sales_price) FROM store_sales",
        expected_metric="average_sales_price",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Total quantity sold by store",
        sql="SELECT ss_store_sk, SUM(ss_quantity) FROM store_sales GROUP BY ss_store_sk",
        expected_metric="total_quantity",
        expected_dimensions=["ss_store_sk"],
    ),
    GoldQuery(
        question="Count of high profit orders",
        sql="SELECT COUNT(*) FROM store_sales WHERE ss_net_profit > 10",
        expected_metric="high_profit_order_count",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Total catalog sales profit",
        sql="SELECT SUM(cs_net_profit) FROM catalog_sales",
        expected_metric="total_catalog_profit",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Total web sales profit",
        sql="SELECT SUM(ws_net_profit) FROM web_sales",
        expected_metric="total_web_profit",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Total store returns amount",
        sql="SELECT SUM(sr_return_amt) FROM store_returns",
        expected_metric="total_returns",
        expected_dimensions=[],
    ),
    GoldQuery(
        question="Total net profit by state",
        sql=(
            "SELECT store.s_state, SUM(store_sales.ss_net_profit) FROM store_sales "
            "JOIN store ON store_sales.ss_store_sk = store.s_store_sk GROUP BY store.s_state"
        ),
        expected_metric="state_profit",
        expected_dimensions=["s_state"],
    ),
    GoldQuery(
        question="Total net profit by year and month",
        sql=(
            "SELECT date_dim.d_year, date_dim.d_moy, SUM(store_sales.ss_net_profit) "
            "FROM store_sales JOIN date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
            "GROUP BY date_dim.d_year, date_dim.d_moy"
        ),
        expected_metric="monthly_profit",
        expected_dimensions=["d_year", "d_moy"],
    ),
]
