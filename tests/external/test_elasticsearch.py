"""
Integration tests for the Elasticsearch driver.

Requires a running Elasticsearch instance. Configure via environment variables:
  ELASTICSEARCH_HOST  (default: localhost)
  ELASTICSEARCH_PORT  (default: 9200)

Tests are skipped automatically when elasticsearch is not installed or the
server is unreachable.
"""

import os
from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.base import DriverError, DriverSettings
from belvedere.drivers.elasticsearch import ElasticsearchDriver
from belvedere.protocol import ReadResult, TableDescription

pytestmark = pytest.mark.external

_INDEX = "belvedere_test"


def _params() -> dict:
    return {
        "host": os.environ.get("ELASTICSEARCH_HOST", "localhost"),
        "port": int(os.environ.get("ELASTICSEARCH_PORT", "9200")),
        "protocol": os.environ.get("ELASTICSEARCH_PROTOCOL", "https"),
    }


@pytest.fixture
async def driver() -> AsyncGenerator[ElasticsearchDriver, None]:
    pytest.importorskip("elasticsearch")
    try:
        d = await ElasticsearchDriver.create(_params(), DriverSettings())
    except Exception as exc:
        pytest.skip(f"Elasticsearch not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture
async def dev_tools_driver() -> AsyncGenerator[ElasticsearchDriver, None]:
    pytest.importorskip("elasticsearch")
    try:
        d = await ElasticsearchDriver.create(
            {**_params(), "query_mode": "dev_tools"}, DriverSettings()
        )
    except Exception as exc:
        pytest.skip(f"Elasticsearch not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture(autouse=True)
async def clean_index(driver: ElasticsearchDriver) -> AsyncGenerator[None, None]:
    await driver._client.indices.delete(index=_INDEX, ignore_unavailable=True)
    await driver._client.indices.create(
        index=_INDEX,
        body={
            "mappings": {
                "properties": {
                    "name": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "total": {"type": "float"},
                }
            }
        },
    )
    yield
    await driver._client.indices.delete(index=_INDEX, ignore_unavailable=True)


async def _index_docs(driver: ElasticsearchDriver, docs: list[dict]) -> None:
    for doc in docs:
        await driver._client.index(index=_INDEX, document=doc)
    await driver._client.indices.refresh(index=_INDEX)


class TestExecute:
    async def test_returns_columns_and_rows(self, driver: ElasticsearchDriver) -> None:
        await _index_docs(
            driver, [{"name": "Alice", "status": "active", "total": 10.0}]
        )
        result = await driver.execute(f"{_INDEX} | *", [])
        assert isinstance(result, ReadResult)
        assert "name" in result.columns
        assert any("Alice" in row for row in result.rows)

    async def test_returns_empty_for_no_matches(
        self, driver: ElasticsearchDriver
    ) -> None:
        result = await driver.execute(f"{_INDEX} | status:nonexistent", [])
        assert isinstance(result, ReadResult)
        assert result.columns == []
        assert result.rows == []

    async def test_filters_by_field(self, driver: ElasticsearchDriver) -> None:
        await _index_docs(
            driver,
            [
                {"name": "Alice", "status": "active"},
                {"name": "Bob", "status": "inactive"},
            ],
        )
        result = await driver.execute(f"{_INDEX} | status:active", [])
        assert isinstance(result, ReadResult)
        names = [row[result.columns.index("name")] for row in result.rows]
        assert names == ["Alice"]

    async def test_raises_for_missing_separator(
        self, driver: ElasticsearchDriver
    ) -> None:
        with pytest.raises(DriverError, match="index.*query"):
            await driver.execute("no separator here", [])


class TestExecuteDSL:
    async def test_match_all_returns_rows(
        self, dev_tools_driver: ElasticsearchDriver
    ) -> None:
        await _index_docs(dev_tools_driver, [{"name": "Alice", "status": "active"}])
        result = await dev_tools_driver.execute(
            f"GET /{_INDEX}/_search\n" + '{"query": {"match_all": {}}}', []
        )
        assert isinstance(result, ReadResult)
        assert "name" in result.columns

    async def test_filters_by_field(
        self, dev_tools_driver: ElasticsearchDriver
    ) -> None:
        await _index_docs(
            dev_tools_driver,
            [
                {"name": "Alice", "status": "active"},
                {"name": "Bob", "status": "inactive"},
            ],
        )
        result = await dev_tools_driver.execute(
            f"GET /{_INDEX}/_search\n" + '{"query": {"term": {"status": "active"}}}', []
        )
        assert isinstance(result, ReadResult)
        names = [row[result.columns.index("name")] for row in result.rows]
        assert names == ["Alice"]

    async def test_non_search_endpoint_returns_flat_row(
        self, dev_tools_driver: ElasticsearchDriver
    ) -> None:
        result = await dev_tools_driver.execute(f"GET /{_INDEX}/_count", [])
        assert isinstance(result, ReadResult)
        assert "count" in result.columns

    async def test_raises_for_missing_method_path(
        self, dev_tools_driver: ElasticsearchDriver
    ) -> None:
        with pytest.raises(DriverError, match="Kibana Dev Tools"):
            await dev_tools_driver.execute("just a query with no method", [])


class TestExploreList:
    async def test_root_lists_indices(self, driver: ElasticsearchDriver) -> None:
        items = await driver.explore_list([])
        names = [i.name for i in items]
        assert _INDEX in names

    async def test_root_hides_system_indices(self, driver: ElasticsearchDriver) -> None:
        items = await driver.explore_list([])
        assert all(not i.name.startswith(".") for i in items)

    async def test_index_lists_groups(self, driver: ElasticsearchDriver) -> None:
        items = await driver.explore_list([_INDEX])
        names = [i.name for i in items]
        assert "mappings" in names
        assert "aliases" in names

    async def test_mappings_lists_fields(self, driver: ElasticsearchDriver) -> None:
        items = await driver.explore_list([_INDEX, "mappings"])
        names = [i.name for i in items]
        assert "name" in names
        assert "status" in names
        assert "total" in names

    async def test_unknown_path_returns_empty(
        self, driver: ElasticsearchDriver
    ) -> None:
        items = await driver.explore_list([_INDEX, "mappings", "extra"])
        assert items == []


class TestExploreDescribe:
    async def test_returns_table_description_for_index(
        self, driver: ElasticsearchDriver
    ) -> None:
        result = await driver.explore_describe([_INDEX])
        assert isinstance(result, TableDescription)
        assert result.table == _INDEX
        assert result.schema is None
        field_names = [c.name for c in result.columns]
        assert "name" in field_names
        assert "status" in field_names

    async def test_returns_none_for_unknown_path(
        self, driver: ElasticsearchDriver
    ) -> None:
        result = await driver.explore_describe([_INDEX, "mappings"])
        assert result is None
