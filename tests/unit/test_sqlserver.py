"""Unit tests for SQLServerDriver — no live database required."""

import asyncio
from unittest.mock import MagicMock

from belvedere.drivers.base import DriverSettings
from belvedere.drivers.sqlserver import SQLServerDriver, _render_lob
from belvedere.protocol import LobPlaceholder, ReadResult


def _make_driver(cur: MagicMock) -> SQLServerDriver:
    conn = MagicMock()
    conn.execute.return_value = cur
    return SQLServerDriver({}, conn, DriverSettings())


class TestRenderLob:
    def test_passes_through_non_binary_values(self) -> None:
        assert _render_lob("hello") == "hello"
        assert _render_lob(42) == 42
        assert _render_lob(None) is None

    def test_renders_bytes_as_byte_count(self) -> None:
        assert _render_lob(b"\x01\x02\x03") == LobPlaceholder(
            text="VARBINARY (3 bytes)"
        )

    def test_renders_bytearray_as_byte_count(self) -> None:
        assert _render_lob(bytearray(5)) == LobPlaceholder(text="VARBINARY (5 bytes)")


class TestExecuteRendersLobs:
    def test_replaces_binary_values_with_placeholders(self) -> None:
        cur = MagicMock()
        cur.description = [("ID",), ("DATA",)]
        cur.fetchall.return_value = [(1, b"\x00\x01")]
        driver = _make_driver(cur)
        result = asyncio.run(driver.execute("SELECT id, data FROM t", []))
        assert isinstance(result, ReadResult)
        assert result.rows == [[1, LobPlaceholder(text="VARBINARY (2 bytes)")]]
