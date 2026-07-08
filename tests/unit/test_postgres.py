"""Unit tests for PostgresDriver — no live database required."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

from belvedere.drivers.base import ConnectionLostError, DriverError, DriverSettings
from belvedere.drivers.postgres.driver import (
    PostgresDriver,
    _maybe_raise_connection_lost,
)
from belvedere.protocol import ExploreItem, ReadResult, WriteResult


def _make_driver(
    rows: list | None = None,
    description: list | None = None,
    rowcount: int = 0,
    execute_side_effect: Exception | None = None,
) -> tuple[PostgresDriver, MagicMock]:
    cur = MagicMock()
    cur.execute = AsyncMock(side_effect=execute_side_effect)
    cur.fetchall = AsyncMock(return_value=rows or [])
    cur.description = description
    cur.rowcount = rowcount
    conn = MagicMock(spec=psycopg.AsyncConnection)
    conn.cursor.return_value = cur
    return PostgresDriver({}, conn, DriverSettings()), cur


class TestExecuteParams:
    def test_no_binds_passes_none_as_params(self) -> None:
        driver, cur = _make_driver(description=None, rowcount=0)
        asyncio.run(driver.execute("DELETE FROM t WHERE x LIKE '%foo%'"))
        assert cur.execute.call_args[0][1] is None

    def test_empty_binds_list_passes_none_as_params(self) -> None:
        driver, cur = _make_driver(description=None, rowcount=0)
        asyncio.run(driver.execute("DELETE FROM t", []))
        assert cur.execute.call_args[0][1] is None

    def test_binds_are_forwarded(self) -> None:
        driver, cur = _make_driver(
            description=[SimpleNamespace(name="val")], rows=[(42,)]
        )
        asyncio.run(driver.execute("SELECT %s AS val", [42]))
        assert cur.execute.call_args[0][1] == [42]


class TestExecuteResults:
    def test_returns_read_result_for_select(self) -> None:
        driver, _ = _make_driver(
            description=[SimpleNamespace(name="n"), SimpleNamespace(name="s")],
            rows=[(1, "hello")],
        )
        result = asyncio.run(driver.execute("SELECT 1 AS n, 'hello' AS s"))
        assert isinstance(result, ReadResult)
        assert result.columns == ["n", "s"]
        assert result.rows == [[1, "hello"]]

    def test_returns_write_result_for_dml(self) -> None:
        driver, _ = _make_driver(description=None, rowcount=3)
        result = asyncio.run(driver.execute("DELETE FROM t"))
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 3

    def test_negative_rowcount_defaults_to_zero(self) -> None:
        driver, _ = _make_driver(description=None, rowcount=-1)
        result = asyncio.run(driver.execute("CREATE TABLE t (id integer)"))
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 0


class TestExecuteErrorPropagation:
    def test_database_error_raises_driver_error(self) -> None:
        exc = psycopg.errors.SyntaxError("syntax error at or near")
        driver, _ = _make_driver(execute_side_effect=exc)
        with pytest.raises(DriverError, match="syntax error"):
            asyncio.run(driver.execute("SELEC 1"))

    def test_operational_error_raises_connection_lost(self) -> None:
        exc = psycopg.OperationalError("server closed the connection unexpectedly")
        driver, _ = _make_driver(execute_side_effect=exc)
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.execute("SELECT 1"))

    def test_interface_error_raises_connection_lost(self) -> None:
        exc = psycopg.InterfaceError("the connection is closed")
        driver, _ = _make_driver(execute_side_effect=exc)
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.execute("SELECT 1"))

    def test_explore_list_not_connected_raises_connection_lost(self) -> None:
        exc = psycopg.OperationalError("connection lost")
        driver, _ = _make_driver(execute_side_effect=exc)
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.explore_list([]))

    def test_explore_describe_not_connected_raises_connection_lost(self) -> None:
        exc = psycopg.OperationalError("connection lost")
        driver, _ = _make_driver(execute_side_effect=exc)
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.explore_describe(["myschema", "mytable"]))


class TestMaybeRaiseConnectionLost:
    def test_operational_error_raises(self) -> None:
        with pytest.raises(ConnectionLostError):
            _maybe_raise_connection_lost(psycopg.OperationalError("down"))

    def test_interface_error_raises(self) -> None:
        with pytest.raises(ConnectionLostError):
            _maybe_raise_connection_lost(psycopg.InterfaceError("closed"))

    def test_other_error_does_not_raise(self) -> None:
        _maybe_raise_connection_lost(psycopg.errors.SyntaxError("bad syntax"))


class TestRootListing:
    def test_returns_schema_items(self) -> None:
        driver, _ = _make_driver(rows=[("alice",), ("bob",)])
        items = asyncio.run(driver.explore_list([]))
        assert items == [
            ExploreItem(name="alice", type="schema", expandable=True),
            ExploreItem(name="bob", type="schema", expandable=True),
        ]


class TestTableGroupListing:
    def test_returns_fixed_groups(self) -> None:
        driver, _ = _make_driver()
        items = asyncio.run(driver.explore_list(["public", "orders"]))
        assert {i.name for i in items} == {"columns", "indexes", "constraints"}
        assert all(i.type == "group" and i.expandable for i in items)
