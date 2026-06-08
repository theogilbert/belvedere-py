import pytest
from unittest.mock import AsyncMock, patch

from dbelveder.dispatcher import Dispatcher
from dbelveder.drivers.base import ConnectionLostError
from dbelveder.protocol import ExploreItem


async def noop_progress(status: str, message: str) -> None:
    pass


@pytest.fixture
def dispatcher() -> Dispatcher:
    return Dispatcher()


@pytest.fixture
def mock_driver() -> AsyncMock:
    d = AsyncMock()
    d.execute.return_value = ([], [])
    d.explore_list.return_value = []
    d.explore_describe.return_value = {}
    return d


@pytest.fixture
async def connected(dispatcher: Dispatcher, mock_driver: AsyncMock) -> tuple[Dispatcher, str, AsyncMock]:
    with patch("dbelveder.dispatcher.get_driver", return_value=lambda _: mock_driver):
        result = await dispatcher.dispatch("connect", {"driver": "mock"}, noop_progress)
    return dispatcher, result["connection_id"], mock_driver


class TestDispatch:
    async def test_should_raise_when_method_is_unknown(self, dispatcher: Dispatcher) -> None:
        with pytest.raises(ValueError, match="Unknown method"):
            await dispatcher.dispatch("no_such", {}, noop_progress)


class TestExecute:
    async def test_should_return_columns_and_rows(self, connected: tuple[Dispatcher, str, AsyncMock]) -> None:
        disp, conn_id, driver = connected
        driver.execute.return_value = (["id"], [[1], [2]])
        result = await disp.dispatch("execute", {"connection_id": conn_id, "sql": "SELECT 1"}, noop_progress)
        assert result == {"columns": ["id"], "rows": [[1], [2]]}

    async def test_should_raise_when_connection_id_is_unknown(self, dispatcher: Dispatcher) -> None:
        with pytest.raises(KeyError):
            await dispatcher.dispatch("execute", {"connection_id": "x", "sql": "SELECT 1"}, noop_progress)

    async def test_should_reconnect_and_retry_when_connection_is_lost(self, connected: tuple[Dispatcher, str, AsyncMock]) -> None:
        disp, conn_id, driver = connected
        driver.execute.side_effect = [ConnectionLostError(), (["n"], [[42]])]
        progress_calls: list[tuple[str, str]] = []

        async def capture(status: str, message: str) -> None:
            progress_calls.append((status, message))

        result = await disp.dispatch("execute", {"connection_id": conn_id, "sql": "SELECT 1"}, capture)
        assert result == {"columns": ["n"], "rows": [[42]]}
        assert driver.connect.await_count == 2  # once on initial connect, once on reconnect
        assert any("reconnect" in s for s, _ in progress_calls)


class TestDisconnect:
    async def test_should_return_ok(self, connected: tuple[Dispatcher, str, AsyncMock]) -> None:
        disp, conn_id, _ = connected
        result = await disp.dispatch("disconnect", {"connection_id": conn_id}, noop_progress)
        assert result == {"ok": True}

    async def test_should_succeed_when_connection_id_is_unknown(self, dispatcher: Dispatcher) -> None:
        result = await dispatcher.dispatch("disconnect", {"connection_id": "x"}, noop_progress)
        assert result == {"ok": True}


class TestExploreList:
    async def test_should_return_items_from_driver(self, connected: tuple[Dispatcher, str, AsyncMock]) -> None:
        disp, conn_id, driver = connected
        driver.explore_list.return_value = [ExploreItem(name="t", type="table", expandable=True)]
        result = await disp.dispatch("explore.list", {"connection_id": conn_id, "path": []}, noop_progress)
        assert result == {"items": [ExploreItem(name="t", type="table", expandable=True)]}


class TestExploreDescribe:
    async def test_should_return_details_from_driver(self, connected: tuple[Dispatcher, str, AsyncMock]) -> None:
        disp, conn_id, driver = connected
        driver.explore_describe.return_value = {"table": "t", "columns": []}
        result = await disp.dispatch("explore.describe", {"connection_id": conn_id, "path": ["t"]}, noop_progress)
        assert result == {"details": {"table": "t", "columns": []}}
