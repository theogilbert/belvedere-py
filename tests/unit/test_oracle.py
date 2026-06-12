"""Unit tests for OracleDriver — no live database required."""

from unittest.mock import MagicMock

import pytest

from dbelveder.drivers.oracle import OracleDriver, _PRE12_SYSTEM_SCHEMAS_SQL
from dbelveder.protocol import ExploreItem


def _make_driver(rows: list, has_oracle_maintained: bool) -> OracleDriver:
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return OracleDriver({}, conn, has_oracle_maintained)


class TestRootListing:
    def test_12c_uses_oracle_maintained_filter(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=True)
        items = driver._explore_list_sync([])
        sql = driver._conn.cursor().execute.call_args[0][0]
        assert "ORACLE_MAINTAINED" in sql
        assert "NOT IN" not in sql

    def test_pre12c_uses_exclusion_list(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=False)
        items = driver._explore_list_sync([])
        sql = driver._conn.cursor().execute.call_args[0][0]
        assert "ORACLE_MAINTAINED" not in sql
        assert "NOT IN" in sql

    def test_pre12c_exclusion_list_contains_known_system_schemas(self) -> None:
        for schema in ("SYS", "SYSTEM", "DBSNMP", "XDB", "OUTLN", "MDSYS"):
            assert f"'{schema}'" in _PRE12_SYSTEM_SCHEMAS_SQL

    def test_returns_schema_items(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=True)
        items = driver._explore_list_sync([])
        assert items == [
            ExploreItem(name="ALICE", type="schema", expandable=True),
            ExploreItem(name="BOB", type="schema", expandable=True),
        ]
