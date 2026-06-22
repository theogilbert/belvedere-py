import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from belvedere.dispatcher import Dispatcher
from belvedere.explore_cache import cache_file
from belvedere.protocol import ExploreItem, Method, TableDescription


async def noop_progress(status: str, message: str) -> None:
    pass


@pytest.fixture
def mock_driver() -> AsyncMock:
    d = AsyncMock()
    d.explore_list.return_value = [ExploreItem(name="t", type="table", expandable=True)]
    d.explore_describe.return_value = TableDescription(table="t", columns=[])
    return d


def _driver_class(driver: AsyncMock) -> AsyncMock:
    cls = AsyncMock()
    cls.create = AsyncMock(return_value=driver)
    return cls


async def connect(dispatcher: Dispatcher, driver: AsyncMock, params: dict) -> str:
    with patch("belvedere.dispatcher.get_driver", return_value=_driver_class(driver)):
        result = await dispatcher.dispatch(Method.CONNECT, params, noop_progress)
    return result["connection_id"]


PARAMS = {"driver": "sqlite", "database": ":memory:"}


class TestDiskCache:
    async def test_should_persist_cache_to_disk_on_miss(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        disp = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp, mock_driver, PARAMS)
        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )
        assert any(tmp_path.iterdir())

    async def test_should_load_cache_from_disk_on_reconnect(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        # first session: populate and persist
        disp1 = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp1, mock_driver, PARAMS)
        await disp1.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )
        await disp1.dispatch(
            Method.DISCONNECT, {"connection_id": conn_id}, noop_progress
        )

        # second session: cache loaded from disk, driver not called again
        disp2 = Dispatcher(cache_dir=tmp_path)
        conn_id2 = await connect(disp2, mock_driver, PARAMS)
        await disp2.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id2, "path": []}, noop_progress
        )

        mock_driver.explore_list.assert_awaited_once()

    async def test_should_recreate_disk_file_after_reset_cache(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        disp = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp, mock_driver, PARAMS)
        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )

        cache_path = cache_file(PARAMS, tmp_path)
        assert cache_path.exists()

        await disp.dispatch(
            Method.EXPLORE_LIST,
            {"connection_id": conn_id, "path": [], "reset_cache": True},
            noop_progress,
        )

        assert cache_path.exists()
        assert mock_driver.explore_list.await_count == 2

    async def test_reset_cache_forces_fresh_data_in_next_session(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        fresh_item = ExploreItem(name="new_table", type="table", expandable=True)

        # first session: populate cache
        disp1 = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp1, mock_driver, PARAMS)
        await disp1.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )

        # reset and return new data
        mock_driver.explore_list.return_value = [fresh_item]
        await disp1.dispatch(
            Method.EXPLORE_LIST,
            {"connection_id": conn_id, "path": [], "reset_cache": True},
            noop_progress,
        )
        await disp1.dispatch(
            Method.DISCONNECT, {"connection_id": conn_id}, noop_progress
        )

        # second session: should load the post-reset data, not the original
        disp2 = Dispatcher(cache_dir=tmp_path)
        conn_id2 = await connect(disp2, mock_driver, PARAMS)
        result = await disp2.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id2, "path": []}, noop_progress
        )

        assert result["items"] == [fresh_item]
        assert (
            mock_driver.explore_list.await_count == 2
        )  # initial + reset; second session hits cache

    async def test_should_survive_corruptcache_file(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        cache_file(PARAMS, tmp_path).write_text("not valid json{{{")
        disp = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp, mock_driver, PARAMS)
        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )
        mock_driver.explore_list.assert_awaited_once()

    async def test_should_not_write_password_tocache_file(
        self, mock_driver: AsyncMock, tmp_path: pathlib.Path
    ) -> None:
        params = {**PARAMS, "password": "s3cr3t"}
        disp = Dispatcher(cache_dir=tmp_path)
        conn_id = await connect(disp, mock_driver, params)
        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_id, "path": []}, noop_progress
        )

        cache_path = cache_file(params, tmp_path)
        assert "s3cr3t" not in cache_path.read_text()

    async def test_should_keep_separate_files_for_different_connections(
        self, tmp_path: pathlib.Path
    ) -> None:
        driver_a, driver_b = AsyncMock(), AsyncMock()
        driver_a.explore_list.return_value = [
            ExploreItem(name="a", type="table", expandable=True)
        ]
        driver_b.explore_list.return_value = [
            ExploreItem(name="b", type="table", expandable=True)
        ]

        params_a = {"driver": "sqlite", "database": "a.db"}
        params_b = {"driver": "sqlite", "database": "b.db"}

        disp = Dispatcher(cache_dir=tmp_path)
        conn_a = await connect(disp, driver_a, params_a)
        conn_b = await connect(disp, driver_b, params_b)

        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_a, "path": []}, noop_progress
        )
        await disp.dispatch(
            Method.EXPLORE_LIST, {"connection_id": conn_b, "path": []}, noop_progress
        )

        assert cache_file(params_a, tmp_path) != cache_file(params_b, tmp_path)
        assert len(list(tmp_path.iterdir())) == 2
