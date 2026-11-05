"""Track B hybrid joins: DuckDB work over Parquet joined to tables in the server's own row store.

Joining data DuckDB computes from Parquet with ordinary database tables is the pattern the Track B
integrations are built for. Each item is written three times, with the same meaning:

* VillageSQL: the DuckDB part runs in ``duckdb_query()``, ``JSON_TABLE`` turns its JSON result
  into rows and MySQL joins them to InnoDB tables (the pattern the extension documents). DuckDB
  cannot see the InnoDB tables, so only the DuckDB part's result crosses over, and every filter
  on an InnoDB column applies after it has crossed.
* pg_duckdb: one PostgreSQL statement mixes ``read_parquet()`` with heap tables (its documented
  pattern); DuckDB plans the whole statement and reads the heap tables itself.
* reference: the same result computed by plain DuckDB with every table read from Parquet.

MariaDB's ``run_in_duckdb()`` returns text that SQL cannot join, so its items are ``n/a``.
The dimension tables live in the server (``HYBRID_TABLES``, row-store schema with its indexes);
``lineitem`` and ``orders`` stay Parquet-only.

Items:

* ``join_innodb``: customers and their balance per nation joined to the 25-row ``nation``.
* ``join_innodb_top_customers`` (TPC-H Q10 shape): the 20 customers with the most revenue from
  returned items in Q4 1993, looked up in ``customer`` and ``nation``. Small result.
* ``join_innodb_supplier_region`` (Q5 shape): the 100 suppliers with the most 1994 revenue,
  joined to ``supplier``, ``nation`` and ``region``, kept for ASIA and summed per nation.
* ``join_innodb_segment_filter`` (Q3 shape): unshipped revenue per customer (one row per
  customer with an open order, millions at SF100), joined to ``customer`` and kept for the
  BUILDING segment. The filter is on the server's table: in VillageSQL the whole per-customer
  result has to cross first, and a result over ``vsql_duckdb.max_result_bytes`` is recorded as
  ``error`` (``extra.kind = "result_cap"``).
"""
from __future__ import annotations

from dataclasses import dataclass

from dbench.queries.trackb import default_globs
from dbench.schema import HYBRID_TABLES
from dbench.systems.villagesql import NativeStatement, escape_sql_literal

__all__ = ["HYBRID_ITEMS", "HYBRID_JOINS", "HYBRID_TABLES", "HybridJoin"]
REVENUE = "sum(l_extendedprice * (1 - l_discount))"
# pg_duckdb reads Parquet columns as r['col'] and needs their types spelled out.
PG_REVENUE = "sum(l['l_extendedprice']::numeric(15,2) * (1 - l['l_discount']::numeric(15,2)))"


@dataclass(frozen=True)
class HybridJoin:
    item: str
    villagesql: NativeStatement
    pg_duckdb: NativeStatement
    reference: str


def _pq(table: str) -> str:
    return f"read_parquet('{default_globs()[table]}')"


def _villagesql(duckdb_part: str, columns: str, rest: str) -> NativeStatement:
    """``SELECT .. FROM JSON_TABLE(duckdb_query(<duckdb_part>)) AS t <rest>``."""
    head, _, tail = rest.partition("FROM t")
    return NativeStatement(
        f"{head}FROM JSON_TABLE(duckdb_query({escape_sql_literal(duckdb_part)}), '$[*]' "
        f"COLUMNS ({columns})) AS t{tail}")


def _join_innodb() -> HybridJoin:
    duck = (f"SELECT c_nationkey, count(*) AS customers, sum(c_acctbal) AS balance "
            f"FROM {_pq('customer')} AS customer GROUP BY c_nationkey")
    return HybridJoin(
        "join_innodb",
        _villagesql(duck, "c_nationkey INT PATH '$.c_nationkey', customers BIGINT PATH "
                          "'$.customers', balance DECIMAL(38,2) PATH '$.balance'",
                    "SELECT n.n_name AS nation, t.customers, t.balance FROM t "
                    "JOIN nation n ON n.n_nationkey = t.c_nationkey ORDER BY n.n_name"),
        NativeStatement(
            "SELECT n.n_name AS nation, t.customers, t.balance FROM ("
            "SELECT r['c_nationkey']::int AS c_nationkey, count(*) AS customers, "
            f"sum(r['c_acctbal']::numeric(15,2)) AS balance FROM {_pq('customer')} r "
            "GROUP BY 1) t JOIN nation n ON n.n_nationkey = t.c_nationkey ORDER BY n.n_name"),
        "SELECT n_name AS nation, count(*) AS customers, sum(c_acctbal) AS balance "
        f"FROM {_pq('customer')} AS customer JOIN {_pq('nation')} AS nation "
        "ON n_nationkey = c_nationkey GROUP BY n_name ORDER BY n_name")


_TOP_CUSTOMERS_FILTER = ("o_orderdate >= DATE '1993-10-01' AND o_orderdate < DATE '1994-01-01' "
                         "AND l_returnflag = 'R'")


def _top_customers() -> HybridJoin:
    duck = (f"SELECT o_custkey AS custkey, {REVENUE} AS revenue "
            f"FROM {_pq('orders')} AS orders JOIN {_pq('lineitem')} AS lineitem "
            f"ON l_orderkey = o_orderkey WHERE {_TOP_CUSTOMERS_FILTER} "
            "GROUP BY o_custkey ORDER BY revenue DESC, custkey LIMIT 20")
    select = ("SELECT c.c_custkey, c.c_name, t.revenue, c.c_acctbal, n.n_name, c.c_phone FROM t "
              "JOIN customer c ON c.c_custkey = t.custkey "
              "JOIN nation n ON n.n_nationkey = c.c_nationkey "
              "ORDER BY t.revenue DESC, c.c_custkey")
    return HybridJoin(
        "join_innodb_top_customers",
        _villagesql(duck, "custkey BIGINT PATH '$.custkey', revenue DECIMAL(38,4) PATH "
                          "'$.revenue'", select),
        NativeStatement(select.replace("FROM t", (
            "FROM (SELECT o['o_custkey']::int AS custkey, "
            f"{PG_REVENUE} AS revenue FROM {_pq('orders')} o JOIN {_pq('lineitem')} l "
            "ON l['l_orderkey'] = o['o_orderkey'] "
            "WHERE o['o_orderdate'] >= '1993-10-01'::date "
            "AND o['o_orderdate'] < '1994-01-01'::date "
            "AND l['l_returnflag'] = 'R' "
            "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20) t"), 1)),
        "SELECT c_custkey, c_name, t.revenue, c_acctbal, n_name, c_phone "
        f"FROM ({duck}) AS t JOIN {_pq('customer')} AS customer ON c_custkey = t.custkey "
        f"JOIN {_pq('nation')} AS nation ON n_nationkey = c_nationkey "
        "ORDER BY t.revenue DESC, c_custkey")


def _supplier_region() -> HybridJoin:
    duck = (f"SELECT l_suppkey AS suppkey, {REVENUE} AS revenue "
            f"FROM {_pq('lineitem')} AS lineitem JOIN {_pq('orders')} AS orders "
            "ON o_orderkey = l_orderkey "
            "WHERE o_orderdate >= DATE '1994-01-01' AND o_orderdate < DATE '1995-01-01' "
            "GROUP BY l_suppkey ORDER BY revenue DESC, suppkey LIMIT 100")
    select = ("SELECT n.n_name, count(*) AS suppliers, sum(t.revenue) AS revenue FROM t "
              "JOIN supplier s ON s.s_suppkey = t.suppkey "
              "JOIN nation n ON n.n_nationkey = s.s_nationkey "
              "JOIN region r ON r.r_regionkey = n.n_regionkey "
              "WHERE r.r_name = 'ASIA' GROUP BY n.n_name ORDER BY revenue DESC, n.n_name")
    return HybridJoin(
        "join_innodb_supplier_region",
        _villagesql(duck, "suppkey BIGINT PATH '$.suppkey', revenue DECIMAL(38,4) PATH "
                          "'$.revenue'", select),
        NativeStatement(select.replace("FROM t", (
            "FROM (SELECT l['l_suppkey']::int AS suppkey, "
            f"{PG_REVENUE} AS revenue FROM {_pq('lineitem')} l JOIN {_pq('orders')} o "
            "ON o['o_orderkey'] = l['l_orderkey'] "
            "WHERE o['o_orderdate'] >= '1994-01-01'::date "
            "AND o['o_orderdate'] < '1995-01-01'::date "
            "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 100) t"), 1)),
        "SELECT n_name, count(*) AS suppliers, sum(t.revenue) AS revenue "
        f"FROM ({duck}) AS t JOIN {_pq('supplier')} AS supplier ON s_suppkey = t.suppkey "
        f"JOIN {_pq('nation')} AS nation ON n_nationkey = s_nationkey "
        f"JOIN {_pq('region')} AS region ON r_regionkey = n_regionkey "
        "WHERE r_name = 'ASIA' GROUP BY n_name ORDER BY revenue DESC, n_name")


def _segment_filter() -> HybridJoin:
    duck = (f"SELECT o_custkey AS custkey, {REVENUE} AS revenue "
            f"FROM {_pq('orders')} AS orders JOIN {_pq('lineitem')} AS lineitem "
            "ON l_orderkey = o_orderkey "
            "WHERE o_orderdate < DATE '1995-03-15' AND l_shipdate > DATE '1995-03-15' "
            "GROUP BY o_custkey")
    select = ("SELECT n.n_name, count(*) AS customers, sum(t.revenue) AS revenue FROM t "
              "JOIN customer c ON c.c_custkey = t.custkey "
              "JOIN nation n ON n.n_nationkey = c.c_nationkey "
              "WHERE c.c_mktsegment = 'BUILDING' GROUP BY n.n_name ORDER BY n.n_name")
    return HybridJoin(
        "join_innodb_segment_filter",
        _villagesql(duck, "custkey BIGINT PATH '$.custkey', revenue DECIMAL(38,4) PATH "
                          "'$.revenue'", select),
        NativeStatement(select.replace("FROM t", (
            "FROM (SELECT o['o_custkey']::int AS custkey, "
            f"{PG_REVENUE} AS revenue FROM {_pq('orders')} o JOIN {_pq('lineitem')} l "
            "ON l['l_orderkey'] = o['o_orderkey'] "
            "WHERE o['o_orderdate'] < '1995-03-15'::date AND l['l_shipdate'] > '1995-03-15'::date "
            "GROUP BY 1) t"), 1)),
        "SELECT n_name, count(*) AS customers, sum(t.revenue) AS revenue "
        f"FROM ({duck}) AS t JOIN {_pq('customer')} AS customer ON c_custkey = t.custkey "
        f"JOIN {_pq('nation')} AS nation ON n_nationkey = c_nationkey "
        "WHERE c_mktsegment = 'BUILDING' GROUP BY n_name ORDER BY n_name")


HYBRID_JOINS: tuple[HybridJoin, ...] = (
    _join_innodb(), _top_customers(), _supplier_region(), _segment_filter())
HYBRID_ITEMS = tuple(j.item for j in HYBRID_JOINS)
