"""
End-to-end tests: feed requests through Server._handle and verify the JSON
written to the server's stdout buffer.
"""

import io
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

from dbelveder.drivers.base import ConnectionLostError
from dbelveder.protocol import Request, SelectResult
from dbelveder.server import Server


@pytest.fixture
def out() -> io.BytesIO:
    return io.BytesIO()


@pytest.fixture
def server(out: io.BytesIO, tmp_path: pathlib.Path) -> Server:
    return Server(out=out, cache_dir=tmp_path)


@pytest.fixture
def mock_get_driver(monkeypatch: MonkeyPatch) -> MagicMock:
    import dbelveder.dispatcher as pkg

    mock = MagicMock()
    monkeypatch.setattr(pkg, "get_driver", mock)
    return mock


def all_responses(out: io.BytesIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


async def send(server: Server, out: io.BytesIO, **kwargs: Any) -> dict[str, Any]:
    await server._handle(Request(**kwargs))
    return all_responses(out)[-1]


class TestHandle:
    async def test_should_return_error_when_method_is_unknown(
        self, server: Server, out: io.BytesIO
    ) -> None:
        r = await send(server, out, id=1, method="no_such", params={})
        assert r["id"] == 1
        assert r["error"] is not None
        assert r["result"] is None

    async def test_should_return_connection_id_when_connect_succeeds(
        self, server: Server, out: io.BytesIO
    ) -> None:
        r = await send(
            server,
            out,
            id=1,
            method="connect",
            params={"driver": "sqlite", "database": ":memory:"},
        )
        assert r["error"] is None
        assert "connection_id" in r["result"]

    async def test_should_return_query_results_when_execute_succeeds(
        self, server: Server, out: io.BytesIO
    ) -> None:
        r1 = await send(
            server,
            out,
            id=1,
            method="connect",
            params={"driver": "sqlite", "database": ":memory:"},
        )
        conn_id = r1["result"]["connection_id"]
        r2 = await send(
            server,
            out,
            id=2,
            method="execute",
            params={"connection_id": conn_id, "sql": "SELECT 42 AS n"},
        )
        assert r2["error"] is None
        assert r2["result"] == {"columns": ["n"], "rows": [[42]]}

    async def test_should_return_error_when_execute_is_called_after_disconnect(
        self, server: Server, out: io.BytesIO
    ) -> None:
        r1 = await send(
            server,
            out,
            id=1,
            method="connect",
            params={"driver": "sqlite", "database": ":memory:"},
        )
        conn_id = r1["result"]["connection_id"]
        await send(server, out, id=2, method="disconnect", params={"connection_id": conn_id})
        r3 = await send(
            server,
            out,
            id=3,
            method="execute",
            params={"connection_id": conn_id, "sql": "SELECT 1"},
        )
        assert r3["error"] is not None

    async def test_should_list_created_tables_when_explore_list_is_called(
        self, server: Server, out: io.BytesIO
    ) -> None:
        r1 = await send(
            server,
            out,
            id=1,
            method="connect",
            params={"driver": "sqlite", "database": ":memory:"},
        )
        conn_id = r1["result"]["connection_id"]
        await send(
            server,
            out,
            id=2,
            method="execute",
            params={"connection_id": conn_id, "sql": "CREATE TABLE t (id INTEGER)"},
        )
        r3 = await send(
            server,
            out,
            id=3,
            method="explore.list",
            params={"connection_id": conn_id, "path": []},
        )
        assert r3["error"] is None
        assert any(item["name"] == "t" for item in r3["result"]["items"])

    async def test_should_emit_progress_messages_before_result_when_reconnecting(
        self, server: Server, out: io.BytesIO, mock_get_driver: MagicMock
    ) -> None:
        mock_driver = AsyncMock()
        mock_driver.execute.side_effect = [ConnectionLostError(), SelectResult(columns=["n"], rows=[[1]])]
        driver_cls = AsyncMock()
        driver_cls.create = AsyncMock(return_value=mock_driver)
        mock_get_driver.return_value = driver_cls

        r1 = await send(server, out, id=1, method="connect", params={"driver": "mock"})
        conn_id = r1["result"]["connection_id"]

        await send(
            server,
            out,
            id=2,
            method="execute",
            params={"connection_id": conn_id, "sql": "SELECT 1"},
        )
        msgs = all_responses(out)
        assert any("progress" in m for m in msgs)
        assert msgs[-1]["result"] == {"columns": ["n"], "rows": [[1]]}
