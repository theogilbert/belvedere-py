"""Unit tests for ElasticsearchDriver — no live server required."""

from unittest.mock import MagicMock, patch

from belvedere.drivers.elasticsearch import ElasticsearchDriver


def _open(params: dict) -> MagicMock:
    with patch("elasticsearch.AsyncElasticsearch") as mock_cls:
        ElasticsearchDriver._open(params)
        return mock_cls.call_args


class TestOpen:
    def test_default_protocol_is_https(self) -> None:
        call = _open({"host": "myhost", "port": 9200})
        assert call.kwargs["hosts"] == ["https://myhost:9200"]

    def test_http_protocol(self) -> None:
        call = _open({"host": "myhost", "port": 9200, "protocol": "http"})
        assert call.kwargs["hosts"] == ["http://myhost:9200"]

    def test_https_protocol_explicit(self) -> None:
        call = _open({"host": "myhost", "port": 9200, "protocol": "https"})
        assert call.kwargs["hosts"] == ["https://myhost:9200"]
