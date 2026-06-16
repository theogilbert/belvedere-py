from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.sqlite import SQLiteDriver
from belvedere.protocol import WriteResult, ExploreItem, ReadResult


@pytest.fixture
async def driver() -> AsyncGenerator[SQLiteDriver, None]:
    d = await SQLiteDriver.create({"database": ":memory:"})
    yield d
    await d.disconnect()


class TestExecute:
    async def test_should_return_columns_and_rows(self, driver: SQLiteDriver) -> None:
        result = await driver.execute("SELECT 1 AS n, 'a' AS s", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["n", "s"]
        assert result.rows == [[1, "a"]]

    async def test_should_return_rows_affected_for_insert(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        result = await driver.execute("INSERT INTO t VALUES (?, ?)", [1, "hello"])
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_should_return_rows_affected_for_update(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        await driver.execute("INSERT INTO t VALUES (1, 'a')", [])
        await driver.execute("INSERT INTO t VALUES (2, 'b')", [])
        result = await driver.execute("UPDATE t SET val = 'x'", [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 2

    async def test_should_return_rows_affected_for_delete(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER)", [])
        await driver.execute("INSERT INTO t VALUES (1)", [])
        await driver.execute("INSERT INTO t VALUES (2)", [])
        result = await driver.execute("DELETE FROM t WHERE id = 1", [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_should_persist_inserts_within_connection(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        await driver.execute("INSERT INTO t VALUES (?, ?)", [1, "hello"])
        result = await driver.execute("SELECT * FROM t", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["id", "val"]
        assert result.rows == [[1, "hello"]]


class TestExploreList:
    async def test_should_list_tables_at_top_level(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE users (id INTEGER)", [])
        items = await driver.explore_list([])
        assert ExploreItem(name="users", type="table", expandable=True) in items

    async def test_should_list_views_at_top_level(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER)", [])
        await driver.execute("CREATE VIEW v AS SELECT * FROM t", [])
        items = await driver.explore_list([])
        names = {i.name for i in items}
        assert "t" in names
        assert "v" in names
        view = next(i for i in items if i.name == "v")
        assert view.type == "view"

    async def test_should_return_groups_for_table(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER)", [])
        items = await driver.explore_list(["t"])
        assert {i.name for i in items} == {"columns", "indices", "foreign_keys"}
        assert all(i.type == "group" and i.expandable for i in items)

    async def test_should_list_columns_in_definition_order(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        items = await driver.explore_list(["t", "columns"])
        assert [i.name for i in items] == ["id", "val"]
        assert all(not i.expandable for i in items)

    async def test_should_list_index_by_name(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        await driver.execute("CREATE INDEX idx_val ON t(val)", [])
        items = await driver.explore_list(["t", "indices"])
        assert any(i.name == "idx_val" for i in items)
        assert all(not i.expandable for i in items)

    async def test_should_return_empty_indices_when_none_exist(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER)", [])
        assert await driver.explore_list(["t", "indices"]) == []

    async def test_should_list_foreign_key_reference(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)", [])
        await driver.execute(
            "CREATE TABLE child (id INTEGER, parent_id INTEGER REFERENCES parent(id))",
            [],
        )
        items = await driver.explore_list(["child", "foreign_keys"])
        assert len(items) == 1
        assert items[0].name == "parent_id → parent.id"
        assert items[0].type == "foreign_key"
        assert not items[0].expandable

    async def test_should_return_empty_foreign_keys_when_none_exist(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER)", [])
        assert await driver.explore_list(["t", "foreign_keys"]) == []

    async def test_should_return_empty_when_path_is_too_deep(
        self, driver: SQLiteDriver
    ) -> None:
        assert await driver.explore_list(["a", "b", "c", "d"]) == []


class TestExploreDescribe:
    async def test_should_return_column_names_and_types(
        self, driver: SQLiteDriver
    ) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER, val TEXT)", [])
        desc = await driver.explore_describe(["t"])
        assert desc is not None
        assert desc.table == "t"
        assert [c.name for c in desc.columns] == ["id", "val"]
        assert [c.type for c in desc.columns] == ["INTEGER", "TEXT"]

    async def test_should_return_nullable_flag(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (a INTEGER NOT NULL, b INTEGER)", [])
        desc = await driver.explore_describe(["t"])
        assert desc is not None
        by_name = {c.name: c for c in desc.columns}
        assert by_name["a"].nullable is False
        assert by_name["b"].nullable is True

    async def test_should_return_pk_flag(self, driver: SQLiteDriver) -> None:
        await driver.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)", [])
        desc = await driver.explore_describe(["t"])
        assert desc is not None
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].pk is True
        assert by_name["val"].pk is False

    async def test_should_return_none_when_path_is_invalid(
        self, driver: SQLiteDriver
    ) -> None:
        assert await driver.explore_describe([]) is None
