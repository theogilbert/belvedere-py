"""Unit tests for OracleDriver — no live database required."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import oracledb
import pytest

from grannos.drivers.base import ConnectionLostError, DriverError, DriverSettings
from grannos.drivers.oracle.driver import (
    OracleDriver,
    _format_db_error,
    _is_explain_plan,
    _offset_to_line_col,
)
from grannos.drivers.oracle.queries import _PRE12_SYSTEM_SCHEMAS_SQL, render_lob
from grannos.protocol import ExploreItem, IndexDescription, LobPlaceholder, ReadResult


def _make_lob(type_name: str, size: int) -> MagicMock:
    lob = MagicMock()
    lob.read = AsyncMock()
    lob.type.name = type_name
    lob.size = AsyncMock(return_value=size)
    return lob


def _make_index_driver(index_row: tuple | None, col_rows: list) -> OracleDriver:
    cur = MagicMock()
    cur.execute = AsyncMock()
    cur.fetchone = AsyncMock(side_effect=[index_row, None])  # meta, then DDL
    cur.fetchall = AsyncMock(side_effect=[col_rows, []])  # fields, then join tables
    conn = MagicMock(spec=oracledb.AsyncConnection)
    conn.cursor.return_value = cur
    return OracleDriver({}, conn, True, DriverSettings())


def _make_driver(rows: list, has_oracle_maintained: bool) -> OracleDriver:
    cur = MagicMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(return_value=rows)
    conn = MagicMock(spec=oracledb.AsyncConnection)
    conn.cursor.return_value = cur
    return OracleDriver({}, conn, has_oracle_maintained, DriverSettings())


def _make_db_error(message: str, offset: int = 0) -> oracledb.DatabaseError:
    err = MagicMock()
    err.offset = offset
    err.is_session_dead = False
    err.__str__ = lambda self: message
    return oracledb.DatabaseError(err)


class TestRootListing:
    def test_12c_uses_oracle_maintained_filter(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=True)
        asyncio.run(driver.explore_list([]))
        sql = driver._conn.cursor().execute.call_args[0][0]  # ty: ignore[unresolved-attribute]
        assert "ORACLE_MAINTAINED" in sql
        assert "NOT IN" not in sql

    def test_pre12c_uses_exclusion_list(self) -> None:
        driver = _make_driver([("ALICE",), ("BOB",)], has_oracle_maintained=False)
        asyncio.run(driver.explore_list([]))
        sql = driver._conn.cursor().execute.call_args[0][0]  # ty: ignore[unresolved-attribute]
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


class TestRenderLob:
    def test_passes_through_non_lob_values(self) -> None:
        assert asyncio.run(render_lob("hello")) == "hello"
        assert asyncio.run(render_lob(42)) == 42

    def test_renders_clob_as_char_count(self) -> None:
        lob = _make_lob("DB_TYPE_CLOB", 3423)
        assert asyncio.run(render_lob(lob)) == LobPlaceholder(text="CLOB (3423 chars)")

    def test_renders_blob_as_byte_count(self) -> None:
        lob = _make_lob("DB_TYPE_BLOB", 128)
        assert asyncio.run(render_lob(lob)) == LobPlaceholder(text="BLOB (128 bytes)")


class TestExecuteRendersLobs:
    def test_replaces_lob_values_with_placeholders(self) -> None:
        lob = _make_lob("DB_TYPE_CLOB", 3423)
        cur = MagicMock()
        cur.execute = AsyncMock()
        cur.description = [("ID",), ("NOTES",)]
        cur.fetchall = AsyncMock(return_value=[(1, lob)])
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        result = asyncio.run(driver.execute("SELECT id, notes FROM t", []))
        assert isinstance(result, ReadResult)
        assert result.rows == [[1, LobPlaceholder(text="CLOB (3423 chars)")]]


class TestExecuteErrorPropagation:
    def test_db_error_without_offset_raises_driver_error(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=0)
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        with pytest.raises(DriverError, match="ORA-00936"):
            asyncio.run(driver.execute("SELECT FROM t", []))

    def test_db_error_with_offset_includes_position(self) -> None:
        exc = _make_db_error("ORA-00936: missing expression", offset=7)
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        with pytest.raises(DriverError, match=r"line 1, col 8"):
            asyncio.run(driver.execute("SELECT FROM t", []))

    def test_dead_session_raises_connection_lost(self) -> None:
        error = MagicMock()
        error.is_session_dead = True
        exc = oracledb.DatabaseError(error)
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.execute("SELECT 1 FROM DUAL", []))

    def test_explore_list_not_connected_raises_connection_lost(self) -> None:
        exc = oracledb.InterfaceError("DPY-1001: not connected to the database")
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.explore_list([]))

    def test_explore_describe_not_connected_raises_connection_lost(self) -> None:
        exc = oracledb.InterfaceError("DPY-1001: not connected to the database")
        cur = MagicMock()
        cur.execute = AsyncMock(side_effect=exc)
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = cur
        driver = OracleDriver({}, conn, True, DriverSettings())
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.explore_describe(["MYSCHEMA", "MYTABLE"]))


class TestDisconnect:
    def test_closes_connection(self) -> None:
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.close = AsyncMock()
        driver = OracleDriver({}, conn, True, DriverSettings())
        asyncio.run(driver.disconnect())
        conn.close.assert_awaited_once()

    def test_swallows_not_connected_error(self) -> None:
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.close = AsyncMock(
            side_effect=oracledb.InterfaceError(
                "DPY-1001: not connected to the database"
            )
        )
        driver = OracleDriver({}, conn, True, DriverSettings())
        asyncio.run(driver.disconnect())  # must not raise


class TestExploreDescribeIndex:
    def test_returns_index_description(self) -> None:
        driver = _make_index_driver(
            index_row=("NORMAL", "UNIQUE", "VISIBLE", "N"),
            col_rows=[("ID", "ASC"), ("NAME", "ASC")],
        )
        result = asyncio.run(
            driver.explore_describe(["MYSCHEMA", "MYTABLE", "indexes", "MY_IDX"])
        )
        assert isinstance(result, IndexDescription)
        assert result.index == "MY_IDX"
        assert result.unique is True
        assert result.tables == ["MYTABLE"]
        assert [f.name for f in result.fields] == ["ID", "NAME"]
        assert all(f.direction == "asc" for f in result.fields)

    def test_desc_direction(self) -> None:
        driver = _make_index_driver(
            index_row=("NORMAL", "NONUNIQUE", "VISIBLE", "N"),
            col_rows=[("CREATED_AT", "DESC")],
        )
        result = asyncio.run(
            driver.explore_describe(["MYSCHEMA", "MYTABLE", "indexes", "MY_IDX"])
        )
        assert isinstance(result, IndexDescription)
        assert result.fields[0].direction == "desc"

    def test_non_unique_index(self) -> None:
        driver = _make_index_driver(
            index_row=("NORMAL", "NONUNIQUE", "VISIBLE", "N"),
            col_rows=[("COL", "ASC")],
        )
        result = asyncio.run(
            driver.explore_describe(["MYSCHEMA", "MYTABLE", "indexes", "MY_IDX"])
        )
        assert isinstance(result, IndexDescription)
        assert result.unique is False

    def test_returns_none_when_index_not_found(self) -> None:
        driver = _make_index_driver(index_row=None, col_rows=[])
        result = asyncio.run(
            driver.explore_describe(["MYSCHEMA", "MYTABLE", "indexes", "NO_SUCH"])
        )
        assert result is None


class TestIsExplainPlan:
    def test_uppercase_matches(self) -> None:
        assert _is_explain_plan("EXPLAIN PLAN FOR SELECT 1 FROM DUAL") is True

    def test_lowercase_matches(self) -> None:
        assert _is_explain_plan("explain plan for SELECT 1 FROM DUAL") is True

    def test_mixed_case_matches(self) -> None:
        assert _is_explain_plan("Explain Plan For SELECT 1 FROM DUAL") is True

    def test_leading_whitespace_matches(self) -> None:
        assert _is_explain_plan("  EXPLAIN PLAN FOR SELECT 1 FROM DUAL") is True

    def test_leading_comment_line_matches(self) -> None:
        assert (
            _is_explain_plan("-- a comment\nEXPLAIN PLAN FOR SELECT 1 FROM DUAL")
            is True
        )

    def test_multiple_leading_comment_lines_match(self) -> None:
        assert _is_explain_plan("-- c1\n-- c2\nEXPLAIN PLAN FOR SELECT 1") is True

    def test_regular_select_does_not_match(self) -> None:
        assert _is_explain_plan("SELECT 1 FROM DUAL") is False

    def test_insert_does_not_match(self) -> None:
        assert _is_explain_plan("INSERT INTO t VALUES (1)") is False


class TestExplainPlanExecute:
    @pytest.fixture()
    def explain_cur(self) -> MagicMock:
        cur = MagicMock()
        cur.execute = AsyncMock()
        cur.fetchall = AsyncMock(
            return_value=[
                ("Plan hash value: 12345",),
                ("| Id | Operation |",),
                ("|  0 | SELECT STATEMENT |",),
            ]
        )
        return cur

    @pytest.fixture()
    def explain_driver(self, explain_cur: MagicMock) -> OracleDriver:
        conn = MagicMock(spec=oracledb.AsyncConnection)
        conn.cursor.return_value = explain_cur
        return OracleDriver({}, conn, True, DriverSettings())

    def test_returns_read_result(
        self, explain_driver: OracleDriver, explain_cur: MagicMock
    ) -> None:
        result = asyncio.run(
            explain_driver.execute("EXPLAIN PLAN FOR SELECT 1 FROM DUAL", [])
        )
        assert isinstance(result, ReadResult)

    def test_columns_are_plan_table_output(
        self, explain_driver: OracleDriver, explain_cur: MagicMock
    ) -> None:
        result = asyncio.run(
            explain_driver.execute("EXPLAIN PLAN FOR SELECT 1 FROM DUAL", [])
        )
        assert isinstance(result, ReadResult)
        assert result.columns == ["PLAN_TABLE_OUTPUT"]

    def test_rows_contain_plan_lines(
        self, explain_driver: OracleDriver, explain_cur: MagicMock
    ) -> None:
        result = asyncio.run(
            explain_driver.execute("EXPLAIN PLAN FOR SELECT 1 FROM DUAL", [])
        )
        assert isinstance(result, ReadResult)
        assert result.rows == [
            ["Plan hash value: 12345"],
            ["| Id | Operation |"],
            ["|  0 | SELECT STATEMENT |"],
        ]

    def test_calls_dbms_xplan_display(
        self, explain_driver: OracleDriver, explain_cur: MagicMock
    ) -> None:
        asyncio.run(explain_driver.execute("EXPLAIN PLAN FOR SELECT 1 FROM DUAL", []))
        second_call_sql = explain_cur.execute.call_args_list[1][0][0]
        assert "DBMS_XPLAN.DISPLAY" in second_call_sql
