import pathlib
from unittest.mock import AsyncMock

import pytest

from belvedere.explore_cache import CachingDriver, ConnectionCache
from belvedere.protocol import ExploreItem, IndexDescription, TableDescription


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> ConnectionCache:
    return ConnectionCache(
        {"driver": "sqlite", "database": ":memory:"}, tmp_path / "c.json"
    )


@pytest.fixture
def inner() -> AsyncMock:
    d = AsyncMock()
    d.explore_list.return_value = [ExploreItem(name="t", type="table", expandable=True)]
    d.explore_describe.return_value = TableDescription(table="t", columns=[])
    return d


@pytest.fixture
def driver(inner: AsyncMock, cache: ConnectionCache) -> CachingDriver:
    return CachingDriver(inner, cache)


class TestExploreListCaching:
    async def test_miss_calls_inner_and_caches(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        items = await driver.explore_list([])
        assert items == inner.explore_list.return_value
        inner.explore_list.assert_awaited_once_with([])

    async def test_hit_returns_cached_without_calling_inner(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_list([])
        await driver.explore_list([])
        inner.explore_list.assert_awaited_once()

    async def test_different_paths_cached_independently(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="a", type="schema", expandable=True)],
            [ExploreItem(name="b", type="table", expandable=False)],
        ]
        result_a = await driver.explore_list(["a"])
        result_b = await driver.explore_list(["b"])
        assert result_a[0].name == "a"
        assert result_b[0].name == "b"
        assert inner.explore_list.await_count == 2
        await driver.explore_list(["a"])
        await driver.explore_list(["b"])
        assert inner.explore_list.await_count == 2


class TestExploreDescribeCaching:
    async def test_miss_calls_inner_and_caches(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        desc = await driver.explore_describe(["s", "t"])
        assert desc == inner.explore_describe.return_value
        inner.explore_describe.assert_awaited_once_with(["s", "t"])

    async def test_hit_returns_cached_without_calling_inner(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_describe(["s", "t"])
        await driver.explore_describe(["s", "t"])
        inner.explore_describe.assert_awaited_once()

    async def test_none_result_not_cached(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = None
        await driver.explore_describe(["s", "t"])
        await driver.explore_describe(["s", "t"])
        assert inner.explore_describe.await_count == 2

    async def test_index_description_cached(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = IndexDescription(
            index="idx", fields=[], unique=False
        )
        await driver.explore_describe(["s", "t", "indices", "idx"])
        result = await driver.explore_describe(["s", "t", "indices", "idx"])
        assert isinstance(result, IndexDescription)
        inner.explore_describe.assert_awaited_once()


class TestResetCache:
    async def test_reset_clears_list_cache(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_list([])
        driver.reset_cache([])
        await driver.explore_list([])
        assert inner.explore_list.await_count == 2

    async def test_reset_clears_describe_cache(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_describe(["s", "t"])
        driver.reset_cache([])
        await driver.explore_describe(["s", "t"])
        assert inner.explore_describe.await_count == 2

    async def test_reset_with_path_clears_subtree(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="a", type="schema", expandable=True)],
            [ExploreItem(name="b", type="table", expandable=False)],
            [ExploreItem(name="a2", type="schema", expandable=True)],
            [ExploreItem(name="b2", type="table", expandable=False)],
        ]
        await driver.explore_list(["s"])
        await driver.explore_list(["s", "t"])
        driver.reset_cache(["s"])
        # both ["s"] and ["s", "t"] should be re-fetched
        await driver.explore_list(["s"])
        await driver.explore_list(["s", "t"])
        assert inner.explore_list.await_count == 4

    async def test_reset_with_path_leaves_siblings_intact(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="s", type="schema", expandable=True)],
            [ExploreItem(name="other", type="schema", expandable=True)],
        ]
        await driver.explore_list(["s"])
        await driver.explore_list(["other"])
        driver.reset_cache(["s"])
        # ["other"] was not under ["s"], so it should still be cached
        await driver.explore_list(["other"])
        assert inner.explore_list.await_count == 2


class TestDelegation:
    async def test_execute_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.execute("SELECT 1", [])
        inner.execute.assert_awaited_once_with("SELECT 1", [])

    async def test_reconnect_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.reconnect()
        inner.reconnect.assert_awaited_once()

    async def test_disconnect_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.disconnect()
        inner.disconnect.assert_awaited_once()
