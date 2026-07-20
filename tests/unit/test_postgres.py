"""Unit tests for PostgresDriver — no live database required."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg import sql

from grannos.drivers.base import ConnectionLostError, DriverError, DriverSettings
from grannos.drivers.postgres.copy import (
    CopyToCommand,
    build_copy_to_statement,
    parse_copy_to,
)
from grannos.drivers.postgres.driver import (
    PostgresDriver,
    _maybe_raise_connection_lost,
)
from grannos.drivers.postgres.queries import render_lob
from grannos.protocol import ExploreItem, LobPlaceholder, ReadResult, WriteResult


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


class _FakeAsyncCopy:
    """Mimics psycopg's Copy object: an async context manager and async iterator."""

    def __init__(
        self, chunks: list[bytes] | None = None, enter_exc: Exception | None = None
    ) -> None:
        self._chunks = list(chunks or [])
        self._enter_exc = enter_exc

    async def __aenter__(self) -> "_FakeAsyncCopy":
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self) -> "_FakeAsyncCopy":
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _make_copy_driver(
    chunks: list[bytes] | None = None,
    rowcount: int = 0,
    enter_exc: Exception | None = None,
) -> tuple[PostgresDriver, MagicMock]:
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.copy = MagicMock(return_value=_FakeAsyncCopy(chunks, enter_exc))
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

    def test_replaces_bytea_values_with_placeholders(self) -> None:
        driver, _ = _make_driver(
            description=[SimpleNamespace(name="id"), SimpleNamespace(name="data")],
            rows=[(1, b"\x00\x01")],
        )
        result = asyncio.run(driver.execute("SELECT id, data FROM t"))
        assert isinstance(result, ReadResult)
        assert result.rows == [[1, LobPlaceholder(text="BYTEA (2 bytes)")]]


class TestRenderLob:
    def test_passes_through_non_binary_values(self) -> None:
        assert render_lob("hello") == "hello"
        assert render_lob(42) == 42
        assert render_lob(None) is None

    def test_renders_bytes_as_byte_count(self) -> None:
        assert render_lob(b"\x01\x02\x03") == LobPlaceholder(text="BYTEA (3 bytes)")


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


class TestParseCopyTo:
    def test_returns_none_for_non_copy_query(self) -> None:
        assert parse_copy_to("SELECT 1") is None

    def test_parses_table_source(self) -> None:
        cmd = parse_copy_to("\\copy orders TO '/tmp/orders.csv'")
        assert cmd == CopyToCommand(source="orders", path="/tmp/orders.csv", options="")

    def test_parses_schema_qualified_table_source(self) -> None:
        cmd = parse_copy_to("\\copy public.orders TO '/tmp/orders.csv'")
        assert cmd is not None
        assert cmd.source == "public.orders"

    def test_parses_query_source(self) -> None:
        cmd = parse_copy_to(
            "\\copy (SELECT * FROM orders WHERE status = 'open') TO '/tmp/o.csv'"
        )
        assert cmd is not None
        assert cmd.source == "(SELECT * FROM orders WHERE status = 'open')"
        assert cmd.path == "/tmp/o.csv"

    def test_parses_trailing_options(self) -> None:
        cmd = parse_copy_to(
            "\\copy orders TO '/tmp/orders.csv' WITH (FORMAT csv, HEADER)"
        )
        assert cmd is not None
        assert cmd.options == "WITH (FORMAT csv, HEADER)"

    def test_unescapes_doubled_quotes_in_path(self) -> None:
        cmd = parse_copy_to("\\copy orders TO '/tmp/o''brien.csv'")
        assert cmd is not None
        assert cmd.path == "/tmp/o'brien.csv"

    def test_case_insensitive(self) -> None:
        assert parse_copy_to("\\COPY orders TO '/tmp/orders.csv'") is not None

    def test_ignores_from_direction(self) -> None:
        assert parse_copy_to("\\copy orders FROM '/tmp/orders.csv'") is None


class TestBuildCopyToStatement:
    def test_without_options(self) -> None:
        cmd = CopyToCommand(source="orders", path="/tmp/o.csv", options="")
        assert build_copy_to_statement(cmd) == "COPY orders TO STDOUT"

    def test_with_options(self) -> None:
        cmd = CopyToCommand(
            source="orders", path="/tmp/o.csv", options="WITH (FORMAT csv, HEADER)"
        )
        assert (
            build_copy_to_statement(cmd)
            == "COPY orders TO STDOUT WITH (FORMAT csv, HEADER)"
        )


class TestExecuteCopyTo:
    def test_streams_result_to_local_file(self, tmp_path) -> None:
        dest = tmp_path / "orders.csv"
        driver, cur = _make_copy_driver(
            chunks=[b"id,status\n", b"1,open\n"], rowcount=1
        )
        result = asyncio.run(
            driver.execute(f"\\copy orders TO '{dest}' (FORMAT csv, HEADER)")
        )
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1
        assert dest.read_bytes() == b"id,status\n1,open\n"
        cur.copy.assert_called_once()

        assert cur.copy.call_args[0][0] == sql.SQL(
            "COPY orders TO STDOUT (FORMAT csv, HEADER)"
        )

    def test_negative_rowcount_defaults_to_zero(self, tmp_path) -> None:
        dest = tmp_path / "orders.csv"
        driver, _ = _make_copy_driver(chunks=[], rowcount=-1)
        result = asyncio.run(driver.execute(f"\\copy orders TO '{dest}'"))
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 0

    def test_write_failure_raises_driver_error(self) -> None:
        driver, _ = _make_copy_driver(chunks=[b"data"])
        with pytest.raises(DriverError, match="could not write"):
            asyncio.run(
                driver.execute("\\copy orders TO '/nonexistent-dir/orders.csv'")
            )

    def test_database_error_raises_driver_error(self) -> None:
        exc = psycopg.errors.SyntaxError('relation "orders" does not exist')
        driver, _ = _make_copy_driver(enter_exc=exc)
        with pytest.raises(DriverError, match="does not exist"):
            asyncio.run(driver.execute("\\copy orders TO '/tmp/orders.csv'"))

    def test_operational_error_raises_connection_lost(self) -> None:
        exc = psycopg.OperationalError("server closed the connection unexpectedly")
        driver, _ = _make_copy_driver(enter_exc=exc)
        with pytest.raises(ConnectionLostError):
            asyncio.run(driver.execute("\\copy orders TO '/tmp/orders.csv'"))


class TestTableGroupListing:
    def test_returns_fixed_groups(self) -> None:
        driver, _ = _make_driver()
        items = asyncio.run(driver.explore_list(["public", "orders"]))
        assert {i.name for i in items} == {"columns", "indexes"}
        assert all(i.type == "group" and i.expandable for i in items)
