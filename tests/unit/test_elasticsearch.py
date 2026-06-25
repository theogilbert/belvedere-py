"""Unit tests for ElasticsearchDriver — no live server required."""

from unittest.mock import patch

from belvedere.drivers.elasticsearch import ElasticsearchDriver


async def _hosts(params: dict) -> list[str]:
    with patch("elasticsearch.AsyncElasticsearch") as mock_cls:
        await ElasticsearchDriver.create(params)
        return mock_cls.call_args.kwargs["hosts"]


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
