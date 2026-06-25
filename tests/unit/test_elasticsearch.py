"""Unit tests for ElasticsearchDriver — no live server required."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from belvedere.drivers.base import DriverError
from belvedere.drivers.elasticsearch import ElasticsearchDriver


async def _hosts(params: dict) -> list[str]:
    with patch("elasticsearch.AsyncElasticsearch") as mock_cls:
        await ElasticsearchDriver.create(params)
        return mock_cls.call_args.kwargs["hosts"]


def _driver_with_transport(response: object) -> ElasticsearchDriver:
    client = MagicMock()
    client.transport.perform_request = AsyncMock(
        return_value=SimpleNamespace(body=response)
    )
    return ElasticsearchDriver({"query_mode": "dev_tools"}, client)


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


class TestExecuteDevToolsErrors:
    async def test_raises_on_error_response(self) -> None:
        driver = _driver_with_transport(
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
        driver = _driver_with_transport({"error": "index_not_found", "status": 404})
        with pytest.raises(DriverError, match="index_not_found"):
            await driver.execute("GET /missing/_search", [])
