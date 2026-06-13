import asyncio
import pathlib

import pytest
from unittest.mock import AsyncMock, patch

from dbelveder.dispatcher import CacheStore, Connection, Dispatcher, IdleTimer
from dbelveder.drivers.base import ConnectionLostError
from dbelveder.protocol import (
    ColumnInfo,
    DMLResult,
    ExploreItem,
    SelectResult,
    TableDescription,
)


async def noop_progress(status: str, message: str) -> None:
    pass


class TestConnection:
    async def test_context_manager_grants_access(self) -> None:
        conn = Connection("1", AsyncMock(), max_concurrency=1)
        async with conn as c:
            assert c is conn

    async def test_limits_concurrency(self) -> None:
        conn = Connection("1", AsyncMock(), max_concurrency=1)
        order: list[str] = []
        gate = asyncio.Event()

        async def task(label: str) -> None:
            async with conn:
                order.append(f"start:{label}")
                await gate.wait()
                order.append(f"end:{label}")

        t1 = asyncio.create_task(task("a"))
        t2 = asyncio.create_task(task("b"))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(t1, t2)
        assert order == ["start:a", "end:a", "start:b", "end:b"]


class TestCacheStore:
    def test_open_creates_cache_accessible_by_conn_id(self, tmp_path: pathlib.Path) -> None:
        store = CacheStore(tmp_path)
        store.open("1", {"driver": "sqlite"})
        assert store["1"] is not None

    def test_close_removes_cache(self, tmp_path: pathlib.Path) -> None:
        store = CacheStore(tmp_path)
        store.open("1", {"driver": "sqlite"})
        store.close("1")
        with pytest.raises(KeyError):
            _ = store["1"]

    def test_close_is_noop_for_unknown_conn(self, tmp_path: pathlib.Path) -> None:
        store = CacheStore(tmp_path)
        store.close("unknown")  # must not raise

    def test_cache_files_are_scoped_to_cache_dir(self, tmp_path: pathlib.Path) -> None:
        store = CacheStore(tmp_path)
        store.open("1", {"driver": "sqlite", "database": "a.db"})
        store.open("2", {"driver": "sqlite", "database": "b.db"})
        assert store["1"] is not store["2"]


class TestIdleTimer:
    async def test_calls_on_expire_after_timeout(self) -> None:
        expired: list[tuple[str, float]] = []

        async def on_expire(conn_id: str, timeout: float) -> None:
            expired.append((conn_id, timeout))

        timer = IdleTimer(on_expire)
        timer.start("a", 0.05)
        await asyncio.sleep(0.15)
        assert expired == [("a", 0.05)]

    async def test_reset_restarts_countdown(self) -> None:
        expired: list[str] = []

        async def on_expire(conn_id: str, timeout: float) -> None:
            expired.append(conn_id)

        timer = IdleTimer(on_expire)
        timer.start("a", 0.1)
        await asyncio.sleep(0.07)
        timer.reset("a")
        await asyncio.sleep(0.07)
        assert expired == []
        await asyncio.sleep(0.1)
        assert expired == ["a"]

    async def test_cancel_prevents_expiry(self) -> None:
        expired: list[str] = []

        async def on_expire(conn_id: str, timeout: float) -> None:
            expired.append(conn_id)

        timer = IdleTimer(on_expire)
        timer.start("a", 0.05)
        timer.cancel("a")
        await asyncio.sleep(0.15)
        assert expired == []

    async def test_reset_is_noop_for_unknown_conn(self) -> None:
        timer = IdleTimer(AsyncMock())
        timer.reset("unknown")  # must not raise

    async def test_cancel_is_noop_for_unknown_conn(self) -> None:
        timer = IdleTimer(AsyncMock())
        timer.cancel("unknown")  # must not raise


@pytest.fixture
def dispatcher(tmp_path: pathlib.Path) -> Dispatcher:
    return Dispatcher(cache_dir=tmp_path)


@pytest.fixture
def mock_driver() -> AsyncMock:
    d = AsyncMock()
    d.execute.return_value = SelectResult(columns=[], rows=[])
    d.explore_list.return_value = []
    d.explore_describe.return_value = None
    return d


def _driver_class(mock_driver: AsyncMock) -> AsyncMock:
    cls = AsyncMock()
    cls.create = AsyncMock(return_value=mock_driver)
    return cls


@pytest.fixture
async def connected(
    dispatcher: Dispatcher, mock_driver: AsyncMock
) -> tuple[Dispatcher, str, AsyncMock]:
    with patch(
        "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
    ):
        result = await dispatcher.dispatch("connect", {"driver": "mock"}, noop_progress)
    return dispatcher, result["connection_id"], mock_driver


class TestCapabilities:
    async def test_should_return_server_name(self, dispatcher: Dispatcher) -> None:
        result = await dispatcher.dispatch("capabilities", {}, noop_progress)
        assert result["server"] == "dbelveder"

    async def test_should_always_include_sqlite(self, dispatcher: Dispatcher) -> None:
        result = await dispatcher.dispatch("capabilities", {}, noop_progress)
        drivers = [t.driver for t in result["drivers"]]
        assert "sqlite" in drivers

    async def test_params_have_required_fields(self, dispatcher: Dispatcher) -> None:
        result = await dispatcher.dispatch("capabilities", {}, noop_progress)
        for tech in result["drivers"]:
            assert len(tech.params) > 0
            for p in tech.params:
                assert p.key
                assert p.type
                assert p.label


class TestDispatch:
    async def test_should_raise_when_method_is_unknown(
        self, dispatcher: Dispatcher
    ) -> None:
        with pytest.raises(ValueError, match="Unknown method"):
            await dispatcher.dispatch("no_such", {}, noop_progress)


class TestExecute:
    async def test_should_return_columns_and_rows(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        driver.execute.return_value = SelectResult(columns=["id"], rows=[[1], [2]])
        result = await disp.dispatch(
            "execute", {"connection_id": conn_id, "query": "SELECT 1"}, noop_progress
        )
        assert result == {"columns": ["id"], "rows": [[1], [2]]}

    async def test_should_return_rows_affected_for_dml(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        driver.execute.return_value = DMLResult(rows_affected=3)
        result = await disp.dispatch(
            "execute", {"connection_id": conn_id, "query": "DELETE FROM t"}, noop_progress
        )
        assert result == {"rows_affected": 3}

    async def test_should_raise_when_connection_id_is_unknown(
        self, dispatcher: Dispatcher
    ) -> None:
        with pytest.raises(KeyError):
            await dispatcher.dispatch(
                "execute", {"connection_id": "x", "query": "SELECT 1"}, noop_progress
            )

    async def test_should_reconnect_and_retry_when_connection_is_lost(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        driver.execute.side_effect = [
            ConnectionLostError(),
            SelectResult(columns=["n"], rows=[[42]]),
        ]
        progress_calls: list[tuple[str, str]] = []

        async def capture(status: str, message: str) -> None:
            progress_calls.append((status, message))

        result = await disp.dispatch(
            "execute", {"connection_id": conn_id, "query": "SELECT 1"}, capture
        )
        assert result == {"columns": ["n"], "rows": [[42]]}
        assert driver.reconnect.await_count == 1
        assert any("reconnect" in s for s, _ in progress_calls)


class TestDisconnect:
    async def test_should_return_ok(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, _ = connected
        result = await disp.dispatch(
            "disconnect", {"connection_id": conn_id}, noop_progress
        )
        assert result == {"ok": True}

    async def test_should_succeed_when_connection_id_is_unknown(
        self, dispatcher: Dispatcher
    ) -> None:
        result = await dispatcher.dispatch(
            "disconnect", {"connection_id": "x"}, noop_progress
        )
        assert result == {"ok": True}


class TestExploreList:
    async def test_should_return_items_from_driver(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        driver.explore_list.return_value = [
            ExploreItem(name="t", type="table", expandable=True)
        ]
        result = await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        assert result == {
            "items": [ExploreItem(name="t", type="table", expandable=True)]
        }

    async def test_should_cache_result_on_repeated_calls(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        driver.explore_list.assert_awaited_once()

    async def test_should_cache_separately_per_path(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": ["dbo"]}, noop_progress
        )
        assert driver.explore_list.await_count == 2

    async def test_should_refresh_cache_when_reset_cache_is_true(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        await disp.dispatch(
            "explore.list",
            {"connection_id": conn_id, "path": [], "reset_cache": True},
            noop_progress,
        )
        assert driver.explore_list.await_count == 2

    async def test_reset_cache_clears_all_paths(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": ["dbo"]}, noop_progress
        )
        # reset on one path clears all; both paths re-fetched
        await disp.dispatch(
            "explore.list",
            {"connection_id": conn_id, "path": [], "reset_cache": True},
            noop_progress,
        )
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": ["dbo"]}, noop_progress
        )
        assert (
            driver.explore_list.await_count == 4
        )  # 2 initial + re-fetch [] + re-fetch ["dbo"]

    async def test_should_keep_separate_caches_per_connection(
        self, dispatcher: Dispatcher, mock_driver: AsyncMock
    ) -> None:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
        ):
            r1 = await dispatcher.dispatch("connect", {"driver": "mock"}, noop_progress)
            r2 = await dispatcher.dispatch("connect", {"driver": "mock"}, noop_progress)
        conn_a, conn_b = r1["connection_id"], r2["connection_id"]
        await dispatcher.dispatch(
            "explore.list", {"connection_id": conn_a, "path": []}, noop_progress
        )
        await dispatcher.dispatch(
            "explore.list", {"connection_id": conn_b, "path": []}, noop_progress
        )
        # resetting conn_a does not affect conn_b's cache
        await dispatcher.dispatch(
            "explore.list",
            {"connection_id": conn_a, "path": [], "reset_cache": True},
            noop_progress,
        )
        await dispatcher.dispatch(
            "explore.list", {"connection_id": conn_b, "path": []}, noop_progress
        )
        assert (
            mock_driver.explore_list.await_count == 3
        )  # a, b, a-reset; b still cached


class TestExploreDescribe:
    async def test_should_return_details_from_driver(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        td = TableDescription(
            table="t", columns=[ColumnInfo(name="id", type="INTEGER")]
        )
        driver.explore_describe.return_value = td
        result = await disp.dispatch(
            "explore.describe", {"connection_id": conn_id, "path": ["t"]}, noop_progress
        )
        assert result == {"details": td}

    async def test_should_cache_result_on_repeated_calls(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.describe", {"connection_id": conn_id, "path": ["t"]}, noop_progress
        )
        await disp.dispatch(
            "explore.describe", {"connection_id": conn_id, "path": ["t"]}, noop_progress
        )
        driver.explore_describe.assert_awaited_once()

    async def test_should_refresh_cache_when_reset_cache_is_true(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.describe", {"connection_id": conn_id, "path": ["t"]}, noop_progress
        )
        await disp.dispatch(
            "explore.describe",
            {"connection_id": conn_id, "path": ["t"], "reset_cache": True},
            noop_progress,
        )
        assert driver.explore_describe.await_count == 2

    async def test_reset_cache_is_shared_with_explore_list(
        self, connected: tuple[Dispatcher, str, AsyncMock]
    ) -> None:
        disp, conn_id, driver = connected
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        # resetting via explore.describe also clears explore.list cache
        await disp.dispatch(
            "explore.describe",
            {"connection_id": conn_id, "path": ["t"], "reset_cache": True},
            noop_progress,
        )
        await disp.dispatch(
            "explore.list", {"connection_id": conn_id, "path": []}, noop_progress
        )
        assert driver.explore_list.await_count == 2


class TestConcurrency:
    async def _connect(self, dispatcher: Dispatcher, driver: AsyncMock) -> str:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(driver)
        ):
            result = await dispatcher.dispatch(
                "connect", {"driver": "mock"}, noop_progress
            )
        return result["connection_id"]

    async def test_should_serialise_concurrent_requests_on_same_connection(
        self, tmp_path: pathlib.Path
    ) -> None:
        dispatcher = Dispatcher(cache_dir=tmp_path, max_concurrency=1)
        order: list[str] = []
        gate = asyncio.Event()

        driver = AsyncMock()

        async def slow_execute(*_: object) -> SelectResult:
            order.append("start")
            await gate.wait()
            order.append("end")
            return SelectResult(columns=[], rows=[])

        driver.execute.side_effect = slow_execute
        conn_id = await self._connect(dispatcher, driver)

        t1 = asyncio.create_task(
            dispatcher.dispatch(
                "execute", {"connection_id": conn_id, "query": "SELECT 1"}, noop_progress
            )
        )
        t2 = asyncio.create_task(
            dispatcher.dispatch(
                "execute", {"connection_id": conn_id, "query": "SELECT 2"}, noop_progress
            )
        )
        await asyncio.sleep(0)  # let both tasks reach the semaphore
        gate.set()
        await asyncio.gather(t1, t2)

        # second request must have started only after the first finished
        assert order == ["start", "end", "start", "end"]

    async def test_should_allow_concurrent_requests_on_different_connections(
        self, tmp_path: pathlib.Path
    ) -> None:
        dispatcher = Dispatcher(cache_dir=tmp_path, max_concurrency=1)
        started: list[str] = []
        gate = asyncio.Event()

        async def slow_execute_a(*_: object) -> SelectResult:
            started.append("a")
            await gate.wait()
            return SelectResult(columns=[], rows=[])

        async def slow_execute_b(*_: object) -> SelectResult:
            started.append("b")
            await gate.wait()
            return SelectResult(columns=[], rows=[])

        driver_a, driver_b = AsyncMock(), AsyncMock()
        driver_a.execute.side_effect = slow_execute_a
        driver_b.execute.side_effect = slow_execute_b

        conn_a = await self._connect(dispatcher, driver_a)
        conn_b = await self._connect(dispatcher, driver_b)

        t1 = asyncio.create_task(
            dispatcher.dispatch(
                "execute", {"connection_id": conn_a, "query": "SELECT 1"}, noop_progress
            )
        )
        t2 = asyncio.create_task(
            dispatcher.dispatch(
                "execute", {"connection_id": conn_b, "query": "SELECT 2"}, noop_progress
            )
        )
        await asyncio.sleep(0)  # let both tasks proceed
        assert set(started) == {"a", "b"}  # both started concurrently
        gate.set()
        await asyncio.gather(t1, t2)


class TestIdleTimeout:
    async def test_connection_closed_after_idle_timeout(
        self, dispatcher: Dispatcher, mock_driver: AsyncMock
    ) -> None:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
        ):
            r = await dispatcher.dispatch(
                "connect", {"driver": "mock", "idle_timeout": 0.05}, noop_progress
            )
        conn_id = r["connection_id"]
        await asyncio.sleep(0.15)
        mock_driver.disconnect.assert_awaited_once()
        with pytest.raises(KeyError):
            await dispatcher.dispatch(
                "execute", {"connection_id": conn_id, "query": "SELECT 1"}, noop_progress
            )

    async def test_timer_resets_on_request(
        self, dispatcher: Dispatcher, mock_driver: AsyncMock
    ) -> None:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
        ):
            r = await dispatcher.dispatch(
                "connect", {"driver": "mock", "idle_timeout": 0.1}, noop_progress
            )
        conn_id = r["connection_id"]
        await asyncio.sleep(0.07)
        await dispatcher.dispatch(
            "execute", {"connection_id": conn_id, "query": "SELECT 1"}, noop_progress
        )
        await asyncio.sleep(0.07)
        mock_driver.disconnect.assert_not_awaited()
        await asyncio.sleep(0.1)
        mock_driver.disconnect.assert_awaited_once()

    async def test_default_timeout_does_not_fire_immediately(
        self, dispatcher: Dispatcher, mock_driver: AsyncMock
    ) -> None:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
        ):
            await dispatcher.dispatch("connect", {"driver": "mock"}, noop_progress)
        await asyncio.sleep(0.1)
        mock_driver.disconnect.assert_not_awaited()

    async def test_explicit_disconnect_cancels_timer(
        self, dispatcher: Dispatcher, mock_driver: AsyncMock
    ) -> None:
        with patch(
            "dbelveder.dispatcher.get_driver", return_value=_driver_class(mock_driver)
        ):
            r = await dispatcher.dispatch(
                "connect", {"driver": "mock", "idle_timeout": 0.1}, noop_progress
            )
        conn_id = r["connection_id"]
        await dispatcher.dispatch(
            "disconnect", {"connection_id": conn_id}, noop_progress
        )
        mock_driver.disconnect.assert_awaited_once()
        await asyncio.sleep(0.15)
        # disconnect should not be called a second time by the watchdog
        assert mock_driver.disconnect.await_count == 1
