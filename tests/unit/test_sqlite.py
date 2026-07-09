"""Unit tests for SQLiteDriver — no live database required."""

import asyncio
import sqlite3

from belvedere.drivers.base import DriverSettings
from belvedere.drivers.sqlite import SQLiteDriver, _render_lob
from belvedere.protocol import LobPlaceholder, ReadResult


def _make_driver() -> SQLiteDriver:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    return SQLiteDriver({}, conn, DriverSettings())


class TestRenderLob:
    def test_passes_through_non_binary_values(self) -> None:
        assert _render_lob("hello") == "hello"
        assert _render_lob(42) == 42
        assert _render_lob(None) is None

    def test_renders_bytes_as_byte_count(self) -> None:
        assert _render_lob(b"\x01\x02\x03") == LobPlaceholder(text="BLOB (3 bytes)")

    def test_renders_bytearray_as_byte_count(self) -> None:
        assert _render_lob(bytearray(5)) == LobPlaceholder(text="BLOB (5 bytes)")


class TestExecuteRendersLobs:
    def test_replaces_blob_values_with_placeholders(self) -> None:
        driver = _make_driver()
        asyncio.run(driver.execute("CREATE TABLE t (id INTEGER, data BLOB)", []))
        asyncio.run(driver.execute("INSERT INTO t VALUES (?, ?)", [1, b"\x00\x01"]))
        result = asyncio.run(driver.execute("SELECT id, data FROM t", []))
        assert isinstance(result, ReadResult)
        assert result.rows == [[1, LobPlaceholder(text="BLOB (2 bytes)")]]
