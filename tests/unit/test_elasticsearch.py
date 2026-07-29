"""Unit tests for ElasticsearchDriver — no live server required."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grannos.drivers.base import DriverError, DriverSettings
from grannos.drivers.elasticsearch import ElasticsearchDriver
from grannos.protocol import ReadResult


async def _hosts(params: dict) -> list[str]:
    with patch("elasticsearch.AsyncElasticsearch") as mock_cls:
        await ElasticsearchDriver.create(params, DriverSettings())
        return mock_cls.call_args.kwargs["hosts"]


def _driver_with_response(response: object) -> ElasticsearchDriver:
    client = MagicMock()
    client.perform_request = AsyncMock(return_value=SimpleNamespace(body=response))
    return ElasticsearchDriver({"query_mode": "dev_tools"}, client, DriverSettings())


class TestOpen:
    async def test_default_protocol_is_https(self) -> None:
        assert await _hosts({"host": "myhost", "port": 9200}) == ["https://myhost:9200"]

    async def test_http_protocol(self) -> None:
        assert await _hosts({"host": "myhost", "port": 9200, "protocol": "http"}) == [
            "http://myhost:9200"
        ]

    async def test_https_protocol_explicit(self) -> None:
        assert await _hosts({"host": "myhost", "port": 9200, "protocol": "https"}) == [
            "https://myhost:9200"
        ]


class TestParseBody:
    def test_empty_body(self) -> None:
        assert ElasticsearchDriver._parse_body("") == (None, None)

    def test_single_json_object(self) -> None:
        body, headers = ElasticsearchDriver._parse_body('{"query": {"match_all": {}}}')
        assert body == {"query": {"match_all": {}}}
        assert headers is not None
        assert headers["Content-Type"] == "application/json"

    def test_multiline_single_json_object(self) -> None:
        body, headers = ElasticsearchDriver._parse_body(
            '{\n  "query": {\n    "match_all": {}\n  }\n}'
        )
        assert body == {"query": {"match_all": {}}}
        assert headers is not None
        assert headers["Content-Type"] == "application/json"

    def test_multiple_json_objects_returns_ndjson(self) -> None:
        body, headers = ElasticsearchDriver._parse_body(
            '{"index": {"_id": "1"}}\n{"name": "Widget"}'
        )
        assert body == b'{"index": {"_id": "1"}}\n{"name": "Widget"}\n'
        assert headers is not None
        assert headers["Content-Type"] == "application/x-ndjson"

    def test_msearch_with_multiline_query_doc(self) -> None:
        payload = '{}\n{\n  "query": {"match_all": {}},\n  "size": 5\n}'
        body, headers = ElasticsearchDriver._parse_body(payload)
        assert body == b'{}\n{"query": {"match_all": {}}, "size": 5}\n'
        assert headers is not None
        assert headers["Content-Type"] == "application/x-ndjson"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(DriverError, match="Invalid request body"):
            ElasticsearchDriver._parse_body("not valid json")


class TestExecuteEsql:
    async def test_returns_columns_and_rows(self) -> None:
        client = MagicMock()
        client.esql.query = AsyncMock(
            return_value={
                "columns": [
                    {"name": "status", "type": "keyword"},
                    {"name": "total", "type": "long"},
                ],
                "values": [["open", 50], ["closed", 30]],
            }
        )
        driver = ElasticsearchDriver({"query_mode": "esql"}, client, DriverSettings())
        result = await driver.execute('FROM orders | WHERE status == "open"', [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["status", "total"]
        assert result.rows == [["open", 50], ["closed", 30]]
        assert result.rows_total == 2
        client.esql.query.assert_awaited_once_with(
            query='FROM orders | WHERE status == "open"', format="json"
        )

    async def test_empty_result(self) -> None:
        client = MagicMock()
        client.esql.query = AsyncMock(
            return_value={
                "columns": [{"name": "status", "type": "keyword"}],
                "values": [],
            }
        )
        driver = ElasticsearchDriver({"query_mode": "esql"}, client, DriverSettings())
        result = await driver.execute("FROM orders | LIMIT 0", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["status"]
        assert result.rows == []
        assert result.rows_total == 0


class TestExecuteDevToolsErrors:
    async def test_raises_on_error_response(self) -> None:
        driver = _driver_with_response(
            {
                "error": {
                    "type": "security_exception",
                    "reason": "missing credentials",
                },
                "status": 401,
            }
        )
        with pytest.raises(DriverError, match="security_exception"):
            await driver.execute("PUT /products\n{}", [])

    async def test_raises_on_string_error(self) -> None:
        driver = _driver_with_response({"error": "index_not_found", "status": 404})
        with pytest.raises(DriverError, match="index_not_found"):
            await driver.execute("GET /missing/_search", [])
