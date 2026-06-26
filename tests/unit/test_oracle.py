"""Unit tests for OracleDriver — no live database required."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import oracledb
import pytest

from belvedere.drivers.base import DriverError
from belvedere.drivers.oracle import (
    OracleDriver,
    _PRE12_SYSTEM_SCHEMAS_SQL,
    _format_db_error,
    _offset_to_line_col,
)
from belvedere.protocol import ExploreItem


def _make_driver(rows: list, has_oracle_maintained: bool) -> OracleDriver:
    cur = MagicMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(return_value=rows)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return OracleDriver({}, conn, has_oracle_maintained)


def _make_db_error(message: str, offset: int = 0) -> oracledb.DatabaseError:
    err = MagicMock()
    err.offset = offset
    err.__str__ = lambda self: message
    exc = oracledb.DatabaseError(err)
    return exc


class TestRootListing:
    def test_12c_uses_oracle_maintained_filter(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=True)
        asyncio.run(driver.explore_list([]))
        sql = driver._conn.cursor().execute.call_args[0][0]
        assert "ORACLE_MAINTAINED" in sql
        assert "NOT IN" not in sql

    def test_pre12c_uses_exclusion_list(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=False)
        asyncio.run(driver.explore_list([]))
        sql = driver._conn.cursor().execute.call_args[0][0]
        assert "ORACLE_MAINTAINED" not in sql
        assert "NOT IN" in sql

    def test_pre12c_exclusion_list_contains_known_system_schemas(self) -> None:
        for schema in ("SYS", "SYSTEM", "DBSNMP", "XDB", "OUTLN", "MDSYS"):
            assert f"'{schema}'" in _PRE12_SYSTEM_SCHEMAS_SQL

    def test_returns_schema_items(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=True)
        items = asyncio.run(driver.explore_list([]))
        assert items == [
            ExploreItem(name="ALICE", type="schema", expandable=True),
            ExploreItem(name="BOB", type="schema", expandable=True),
        ]


class TestOffsetToLineCol:
    def test_single_line_gives_line_1(self) -> None:
        assert _offset_to_line_col("SELECT FROM t", 7) == (1, 8)

    def test_second_line(self) -> None:
        query = "SELECT\nFROM t"
        assert _offset_to_line_col(query, 7) == (2, 1)

    def test_middle_of_second_line(self) -> None:
        query = "SELECT\nFROM t"
        assert _offset_to_line_col(query, 10) == (2, 4)


class TestFormatDbError:
    def test_no_offset_returns_plain_message(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=0)
        assert _format_db_error(exc, "SELECT FROM t") == "ORA-00936: missing expression"

    def test_with_offset_appends_line_col(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=7)
        result = _format_db_error(exc, "SELECT FROM t")
        assert result == "ORA-00936: missing expression (line 1, col 8)"

    def test_multiline_query_offset_shows_correct_line(self) -> None:
        query = "SELECT\nFROM t"
        exc = _make_db_error("ORA-00936: missing expression", offset=7)
        result = _format_db_error(exc, query)
        assert result == "ORA-00936: missing expression (line 2, col 1)"


class TestExecuteErrorPropagation:
    def test_db_error_without_offset_raises_driver_error(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=0)
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock()
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True)
        with pytest.raises(DriverError, match="ORA-00936"):
            asyncio.run(driver.execute("SELECT FROM t", []))

    def test_db_error_with_offset_includes_position(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=7)
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock()
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True)
        with pytest.raises(DriverError, match=r"line 1, col 8"):
            asyncio.run(driver.execute("SELECT FROM t", []))
