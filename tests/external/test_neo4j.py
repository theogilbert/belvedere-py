"""
Integration tests for the Neo4j driver.

Requires a running Neo4j instance. Configure via environment variables:
  NEO4J_URI       (default: bolt://localhost:7687)
  NEO4J_USER      (default: neo4j)
  NEO4J_PASSWORD  (required — no default)
  NEO4J_DATABASE  (default: neo4j)

Tests are skipped automatically when neo4j is not installed or the
server is unreachable.
"""

import os
from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.base import DriverSettings
from belvedere.drivers.neo4j import Neo4jDriver
from belvedere.protocol import ExploreItem, IndexDescription, ReadResult, WriteResult

pytestmark = pytest.mark.external


def _params() -> dict:
    return {
        "uri": os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.environ.get("NEO4J_USER", "neo4j"),
        "password": os.environ.get("NEO4J_PASSWORD", ""),
        "database": os.environ.get("NEO4J_DATABASE", "neo4j"),
    }


async def _drop_user_indexes(driver: Neo4jDriver) -> None:
    """Drop all non-LOOKUP indexes (and their owning constraints) before each test."""
    result = await driver.execute(
        "SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name", []
    )
    if isinstance(result, ReadResult):
        for row in result.rows:
            name = row[0]
            await driver.execute(f"DROP CONSTRAINT `{name}` IF EXISTS", [])
            await driver.execute(f"DROP INDEX `{name}` IF EXISTS", [])


@pytest.fixture
async def driver() -> AsyncGenerator[Neo4jDriver, None]:
    pytest.importorskip("neo4j")
    try:
        d = await Neo4jDriver.create(_params(), DriverSettings())
    except Exception as exc:
        pytest.skip(f"Neo4j not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture(autouse=True)
async def clean_db(driver: Neo4jDriver) -> AsyncGenerator[None, None]:
    """Wipe all nodes, relationships, and user-defined indexes before each test."""
    await driver.execute("MATCH (n) DETACH DELETE n", [])
    await _drop_user_indexes(driver)
    yield
    await driver.execute("MATCH (n) DETACH DELETE n", [])
    await _drop_user_indexes(driver)


class TestExecute:
    async def test_should_return_columns_and_rows(self, driver: Neo4jDriver) -> None:
        result = await driver.execute("RETURN 1 AS n, 'hello' AS s", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["n", "s"]
        assert result.rows == [["1", "hello"]]

    async def test_should_support_positional_params(self, driver: Neo4jDriver) -> None:
        result = await driver.execute("RETURN $0 AS val", [42])
        assert isinstance(result, ReadResult)
        assert result.rows == [["42"]]

    async def test_should_return_dml_result_for_create(
        self, driver: Neo4jDriver
    ) -> None:
        result = await driver.execute("CREATE (n:User {name: 'Alice'})", [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected > 0

    async def test_should_return_dml_result_for_delete(
        self, driver: Neo4jDriver
    ) -> None:
        await driver.execute("CREATE (n:User {name: 'Alice'})", [])
        result = await driver.execute("MATCH (n:User) DELETE n", [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected > 0

    async def test_should_serialize_node_to_dict(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE (:User {name: 'Alice', age: 30})", [])
        result = await driver.execute("MATCH (p:User) RETURN p", [])
        assert isinstance(result, ReadResult)
        assert len(result.rows) == 1
        row = dict(zip(result.columns, result.rows[0]))
        assert row["p.name"] == "Alice"
        assert row["p.age"] == "30"
        assert row["p._labels"] == "{User}"

    async def test_should_serialize_relationship_to_dict(
        self, driver: Neo4jDriver
    ) -> None:
        await driver.execute(
            "CREATE (:User {name: 'Alice'})-[:BOUGHT {price: 9.99}]->(:Product {name: 'Book'})",
            [],
        )
        result = await driver.execute("MATCH ()-[r:BOUGHT]->() RETURN r", [])
        assert isinstance(result, ReadResult)
        row = dict(zip(result.columns, result.rows[0]))
        assert row["r._type"] == "BOUGHT"
        assert row["r.price"] == "9.99"

    async def test_should_persist_within_connection(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE (n:User {name: 'Alice'})", [])
        result = await driver.execute("MATCH (n:User) RETURN n.name AS name", [])
        assert isinstance(result, ReadResult)
        assert result.rows == [["Alice"]]


class TestExploreList:
    async def test_root_returns_entities_and_relationships(
        self, driver: Neo4jDriver
    ) -> None:
        items = await driver.explore_list([])
        assert items == [
            ExploreItem(name="entities", type="group", expandable=True),
            ExploreItem(name="relationships", type="group", expandable=True),
            ExploreItem(name="indexes", type="group", expandable=True),
        ]

    async def test_entities_lists_node_labels(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE (:User), (:Product)", [])
        items = await driver.explore_list(["entities"])
        names = [i.name for i in items]
        assert "User" in names
        assert "Product" in names
        assert all(i.expandable for i in items)

    async def test_relationships_lists_relationship_types(
        self, driver: Neo4jDriver
    ) -> None:
        await driver.execute(
            "CREATE (:User)-[:BOUGHT]->(:Product)-[:BELONGS_TO]->(:Category)", []
        )
        items = await driver.explore_list(["relationships"])
        names = [i.name for i in items]
        assert "BOUGHT" in names
        assert "BELONGS_TO" in names
        assert all(i.expandable for i in items)

    async def test_entity_properties_lists_known_properties(
        self, driver: Neo4jDriver
    ) -> None:
        await driver.execute(
            "CREATE (:User {name: 'Alice', age: 30}), (:User {name: 'Bob', email: 'b@b.com'})",
            [],
        )
        items = await driver.explore_list(["entities", "User"])
        names = [i.name for i in items]
        assert "name" in names
        assert "age" in names
        assert "email" in names
        assert all(not i.expandable for i in items)

    async def test_relationship_properties_lists_known_properties(
        self, driver: Neo4jDriver
    ) -> None:
        await driver.execute(
            "CREATE (:User)-[:BOUGHT {price: 9.99, qty: 2}]->(:Product)", []
        )
        items = await driver.explore_list(["relationships", "BOUGHT"])
        names = [i.name for i in items]
        assert "price" in names
        assert "qty" in names
        assert all(not i.expandable for i in items)

    async def test_entity_properties_empty_when_no_nodes(
        self, driver: Neo4jDriver
    ) -> None:
        assert await driver.explore_list(["entities", "Ghost"]) == []

    async def test_relationship_properties_empty_when_no_relationships(
        self, driver: Neo4jDriver
    ) -> None:
        assert await driver.explore_list(["relationships", "GHOST"]) == []

    async def test_indexes_lists_index_names(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE INDEX user_name_idx FOR (n:User) ON (n.name)", [])
        items = await driver.explore_list(["indexes"])
        names = [i.name for i in items]
        assert "user_name_idx" in names
        assert all(i.type == "index" for i in items)
        assert all(not i.expandable for i in items)

    async def test_unknown_path_returns_empty(self, driver: Neo4jDriver) -> None:
        assert await driver.explore_list(["entities", "User", "extra"]) == []


class TestExploreDescribe:
    async def test_returns_none_for_all_paths(self, driver: Neo4jDriver) -> None:
        assert await driver.explore_describe([]) is None
        assert await driver.explore_describe(["entities"]) is None
        assert await driver.explore_describe(["entities", "User"]) is None


class TestExploreDescribeIndex:
    async def test_range_index_fields_and_direction(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE INDEX test_idx FOR (n:User) ON (n.name)", [])
        desc = await driver.explore_describe(["indexes", "test_idx"])
        assert isinstance(desc, IndexDescription)
        assert desc.index == "test_idx"
        assert desc.tables == ["User"]
        assert len(desc.fields) == 1
        assert desc.fields[0].name == "name"
        assert desc.fields[0].direction == "RANGE"

    async def test_unique_index(self, driver: Neo4jDriver) -> None:
        await driver.execute(
            "CREATE CONSTRAINT test_idx_unique FOR (n:User) REQUIRE n.email IS UNIQUE",
            [],
        )
        desc = await driver.explore_describe(["indexes", "test_idx_unique"])
        assert isinstance(desc, IndexDescription)
        assert desc.unique is True

    async def test_non_unique_index(self, driver: Neo4jDriver) -> None:
        await driver.execute("CREATE INDEX test_idx FOR (n:User) ON (n.name)", [])
        desc = await driver.explore_describe(["indexes", "test_idx"])
        assert isinstance(desc, IndexDescription)
        assert desc.unique is False

    async def test_composite_index_field_order(self, driver: Neo4jDriver) -> None:
        await driver.execute(
            "CREATE INDEX test_idx_composite FOR (n:User) ON (n.last, n.first)", []
        )
        desc = await driver.explore_describe(["indexes", "test_idx_composite"])
        assert isinstance(desc, IndexDescription)
        assert [f.name for f in desc.fields] == ["last", "first"]

    async def test_lookup_index_has_no_fields(self, driver: Neo4jDriver) -> None:
        items = await driver.explore_list(["indexes"])
        lookup = next((i for i in items if "lookup" in i.name.lower()), None)
        if lookup is None:
            pytest.skip("no LOOKUP index found")
        desc = await driver.explore_describe(["indexes", lookup.name])
        assert isinstance(desc, IndexDescription)
        assert desc.fields == []

    async def test_unknown_index_returns_none(self, driver: Neo4jDriver) -> None:
        assert await driver.explore_describe(["indexes", "no_such_idx"]) is None
