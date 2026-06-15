"""
Integration tests for the MongoDB driver.

Requires a running MongoDB instance. Configure via environment variables:
  MONGODB_URI       (default: mongodb://localhost:27017)
  MONGODB_DATABASE  (default: belvedere_test)

Tests are skipped automatically when pymongo is not installed or the
server is unreachable.
"""

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from belvedere.drivers.mongodb import MongoDriver
from belvedere.protocol import DMLResult, ExploreItem, SelectResult

pytestmark = pytest.mark.external


def _params() -> dict:
    return {
        "uri": os.environ.get("MONGODB_URI", "mongodb://localhost:27017"),
        "database": os.environ.get("MONGODB_DATABASE", "belvedere_test"),
    }


@pytest.fixture
async def driver() -> AsyncGenerator[MongoDriver, None]:
    pytest.importorskip("pymongo")
    try:
        d = await MongoDriver.create(_params())
    except Exception as exc:
        pytest.skip(f"MongoDB not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture(autouse=True)
async def clean_db(driver: MongoDriver) -> AsyncGenerator[None, None]:
    db_name = _params()["database"]
    driver._client[db_name].drop_collection("users")
    driver._client[db_name].drop_collection("orders")
    yield
    driver._client[db_name].drop_collection("users")
    driver._client[db_name].drop_collection("orders")


def _cmd(**kwargs: Any) -> str:
    return json.dumps({"db": _params()["database"], **kwargs})


class TestExecuteFind:
    async def test_returns_columns_and_rows(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"name": "Alice", "age": 30})
        result = await driver.execute(_cmd(find="users", filter={"name": "Alice"}), [])
        assert isinstance(result, SelectResult)
        row = dict(zip(result.columns, result.rows[0]))
        assert row["name"] == "Alice"
        assert row["age"] == "30"

    async def test_flattens_nested_documents(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"name": "Bob", "address": {"city": "NYC", "zip": "10001"}})
        result = await driver.execute(_cmd(find="users", filter={"name": "Bob"}), [])
        assert isinstance(result, SelectResult)
        row = dict(zip(result.columns, result.rows[0]))
        assert row["address.city"] == "NYC"
        assert row["address.zip"] == "10001"

    async def test_serializes_object_id(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"name": "Carol"})
        result = await driver.execute(_cmd(find="users"), [])
        assert isinstance(result, SelectResult)
        row = dict(zip(result.columns, result.rows[0]))
        assert "_id" in row
        assert row["_id"] is not None

    async def test_returns_empty_result_for_no_matches(self, driver: MongoDriver) -> None:
        result = await driver.execute(_cmd(find="users", filter={"name": "Ghost"}), [])
        assert isinstance(result, SelectResult)
        assert result.rows == []

    async def test_respects_limit(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([{"n": i} for i in range(20)])
        result = await driver.execute(_cmd(find="users", limit=5), [])
        assert isinstance(result, SelectResult)
        assert len(result.rows) == 5


class TestExecuteAggregate:
    async def test_aggregate_groups_correctly(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["orders"].insert_many([
            {"status": "open", "amount": 10},
            {"status": "open", "amount": 20},
            {"status": "closed", "amount": 5},
        ])
        result = await driver.execute(_cmd(
            aggregate="orders",
            pipeline=[
                {"$group": {"_id": "$status", "total": {"$sum": "$amount"}}},
                {"$sort": {"_id": 1}},
            ],
        ), [])
        assert isinstance(result, SelectResult)
        rows = [dict(zip(result.columns, r)) for r in result.rows]
        by_status = {r["_id"]: r["total"] for r in rows}
        assert by_status["closed"] == "5"
        assert by_status["open"] == "30"


class TestExecuteDML:
    async def test_insert_one_returns_rows_affected(self, driver: MongoDriver) -> None:
        result = await driver.execute(_cmd(insertOne="users", document={"name": "Alice"}), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 1

    async def test_insert_many_returns_rows_affected(self, driver: MongoDriver) -> None:
        result = await driver.execute(_cmd(insertMany="users", documents=[{"name": "Alice"}, {"name": "Bob"}]), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 2

    async def test_update_one_returns_rows_affected(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([{"name": "Alice", "active": False}])
        result = await driver.execute(_cmd(
            updateOne="users",
            filter={"name": "Alice"},
            update={"$set": {"active": True}},
        ), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 1

    async def test_update_many_returns_rows_affected(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([{"role": "admin"}, {"role": "admin"}])
        result = await driver.execute(_cmd(
            updateMany="users",
            filter={"role": "admin"},
            update={"$set": {"active": True}},
        ), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 2

    async def test_delete_one_returns_rows_affected(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([{"name": "Alice"}, {"name": "Bob"}])
        result = await driver.execute(_cmd(deleteOne="users", filter={"name": "Alice"}), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 1

    async def test_delete_many_returns_rows_affected(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([{"role": "admin"}, {"role": "admin"}, {"role": "user"}])
        result = await driver.execute(_cmd(deleteMany="users", filter={"role": "admin"}), [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 2


class TestExploreList:
    async def test_root_lists_databases(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"seed": True})
        items = await driver.explore_list([])
        names = [i.name for i in items]
        assert db in names
        assert all(i.type == "database" for i in items)
        assert all(i.expandable for i in items)

    async def test_database_lists_collections(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"x": 1})
        driver._client[db]["orders"].insert_one({"x": 1})
        items = await driver.explore_list([db])
        names = [i.name for i in items]
        assert "users" in names
        assert "orders" in names
        assert all(i.type == "collection" for i in items)
        assert all(i.expandable for i in items)

    async def test_collection_lists_fields_and_indexes_groups(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"x": 1})
        items = await driver.explore_list([db, "users"])
        assert items == [
            ExploreItem(name="fields", type="group", expandable=True),
            ExploreItem(name="indexes", type="group", expandable=True),
        ]

    async def test_fields_samples_top_level_keys(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_many([
            {"name": "Alice", "age": 30},
            {"name": "Bob", "email": "b@b.com"},
        ])
        items = await driver.explore_list([db, "users", "fields"])
        names = [i.name for i in items]
        assert "name" in names
        assert "age" in names
        assert "email" in names
        assert all(i.type == "field" for i in items)
        assert all(not i.expandable for i in items)

    async def test_indexes_lists_index_names(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({"name": "Alice"})
        driver._client[db]["users"].create_index("name", name="name_idx")
        items = await driver.explore_list([db, "users", "indexes"])
        names = [i.name for i in items]
        assert "_id_" in names
        assert "name_idx" in names
        assert all(i.type == "index" for i in items)
        assert all(not i.expandable for i in items)

    async def test_fields_empty_for_empty_collection(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        driver._client[db]["users"].insert_one({})
        driver._client[db]["users"].delete_many({})
        items = await driver.explore_list([db, "users", "fields"])
        assert items == []

    async def test_unknown_path_returns_empty(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        assert await driver.explore_list([db, "users", "fields", "extra"]) == []


class TestExploreDescribe:
    async def test_returns_none_for_all_paths(self, driver: MongoDriver) -> None:
        db = _params()["database"]
        assert await driver.explore_describe([]) is None
        assert await driver.explore_describe([db]) is None
        assert await driver.explore_describe([db, "users"]) is None
