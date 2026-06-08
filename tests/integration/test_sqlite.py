from collections.abc import AsyncGenerator

import pytest

from dbelveder.drivers.sqlite import SQLiteDriver
from dbelveder.protocol import ExploreItem


@pytest.fixture
async def driver() -> AsyncGenerator[SQLiteDriver, None]:
    d = SQLiteDriver({"database": ":memory:"})
    await d.connect()
    yield d
    await d.disconnect()


class TestExecute:
    async def test_should_return_columns_and_rows(self, driver: SQLiteDriver) -> None:
        cols, rows = await driver.execute("SELECT 1 AS n, 'a' AS s", [])
        assert cols == ["n", "s"]
        assert rows == [[1, "a"]]

    async def test_should_persist_inserts_within_connection(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        await driver.execute("INSERT INTO t VALUES (?, ?)", [1, "hello"])
        cols, rows = await driver.execute("SELECT * FROM t", [])
        assert cols == ["id", "val"]
        assert rows == [[1, "hello"]]


class TestExploreList:
    async def test_should_list_tables_at_top_level(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE users (id INTEGER)", [])
        items = await driver.explore_list([])
        assert ExploreItem(name="users", type="table", expandable=True) in items

    async def test_should_return_column_and_index_groups_for_table(self, driver: SQLiteDriver) -> None:
        items = await driver.explore_list(["any_table"])
        assert {i.name for i in items} == {"columns", "indices"}
        assert all(i.expandable for i in items)

    async def test_should_list_columns_for_table(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        items = await driver.explore_list(["t", "columns"])
        assert [i.name for i in items] == ["id", "val"]
        assert all(not i.expandable for i in items)

    async def test_should_return_empty_when_path_is_too_deep(self, driver: SQLiteDriver) -> None:
        assert await driver.explore_list(["a", "b", "c", "d"]) == []


class TestExploreDescribe:
    async def test_should_return_column_info_for_table(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        desc = await driver.explore_describe(["t"])
        assert desc["table"] == "t"
        assert [c["name"] for c in desc["columns"]] == ["id", "val"]

    async def test_should_return_empty_when_path_is_invalid(self, driver: SQLiteDriver) -> None:
        assert await driver.explore_describe([]) == {}
