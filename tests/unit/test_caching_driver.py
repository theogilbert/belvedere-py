import pathlib
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from grannos.explore_cache import CachingDriver, ConnectionCache
from grannos.protocol import (
    ColumnDescription,
    ColumnsDescription,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    TableDescription,
)

PARAMS = {"driver": "sqlite", "database": ":memory:"}


def _columns_desc() -> ColumnsDescription:
    idx = IndexDescription(
        index="i1", fields=[IndexKeyField(name="ID", direction="asc")], unique=True
    )
    return ColumnsDescription(
        columns=[
            ColumnDescription(
                name="ID", data_type="NUMBER", pk=True, exclusive_indices=[idx]
            ),
            ColumnDescription(name="VAL", data_type="VARCHAR2", sample=["a", "b"]),
        ]
    )


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> ConnectionCache:
    return ConnectionCache(PARAMS, tmp_path / "c.json")


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


class TestColumnsFanOut:
    async def test_columns_result_populates_per_column_entries(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _columns_desc()
        await driver.explore_describe(["s", "t", "columns"])
        col = await driver.explore_describe(["s", "t", "columns", "ID"])
        assert col == _columns_desc().columns[0]
        inner.explore_describe.assert_awaited_once()

    async def test_all_columns_served_from_fan_out(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _columns_desc()
        await driver.explore_describe(["s", "t", "columns"])
        val = await driver.explore_describe(["s", "t", "columns", "VAL"])
        assert val == _columns_desc().columns[1]
        inner.explore_describe.assert_awaited_once()

    async def test_reset_clears_fanned_out_entries(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _columns_desc()
        await driver.explore_describe(["s", "t", "columns"])
        driver.reset_cache(["s", "t", "columns"])
        await driver.explore_describe(["s", "t", "columns", "ID"])
        assert inner.explore_describe.await_count == 2


class TestDescribeDiskRoundTrip:
    def test_columns_description_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        ConnectionCache(PARAMS, path).set_describe(
            ["s", "t", "columns"], _columns_desc()
        )
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t", "columns"]) == _columns_desc()

    def test_column_description_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        col = _columns_desc().columns[0]
        ConnectionCache(PARAMS, path).set_describe(["s", "t", "columns", "ID"], col)
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t", "columns", "ID"]) == col

    def test_columns_entry_does_not_poison_other_entries(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "c.json"
        cache = ConnectionCache(PARAMS, path)
        cache.set_describe(["s", "t"], TableDescription(table="t", columns=[]))
        cache.set_describe(["s", "t", "columns"], _columns_desc())
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t"]) == TableDescription(
            table="t", columns=[]
        )

    def test_non_json_sample_values_persist(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        col = ColumnDescription(
            name="TS", data_type="DATE", sample=[datetime(2024, 1, 1), Decimal("1.5")]
        )
        ConnectionCache(PARAMS, path).set_describe(["s", "t", "columns", "TS"], col)
        reloaded = ConnectionCache(PARAMS, path).get_describe(
            ["s", "t", "columns", "TS"]
        )
        assert isinstance(reloaded, ColumnDescription)
        assert reloaded.sample == ["2024-01-01T00:00:00", 1.5]

    def test_table_comment_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        desc = TableDescription(table="t", columns=[], comment="a comment")
        ConnectionCache(PARAMS, path).set_describe(["s", "t"], desc)
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t"]) == desc


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
