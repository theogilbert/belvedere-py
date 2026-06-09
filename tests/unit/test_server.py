import io
import logging

import pytest

from dbelveder.protocol import Result
from dbelveder.server import Server, _LOG_CAP, _redact, _truncate


class TestTruncate:
    def test_should_return_string_unchanged_when_under_cap(self) -> None:
        text = "x" * _LOG_CAP
        assert _truncate(text) == text

    def test_should_truncate_and_append_ellipsis_when_over_cap(self) -> None:
        text = "x" * (_LOG_CAP + 10)
        result = _truncate(text)
        assert len(result) == _LOG_CAP + 1  # cap chars + ellipsis character
        assert result.endswith("…")

    def test_should_truncate_at_cap_boundary(self) -> None:
        text = "x" * (_LOG_CAP + 1)
        result = _truncate(text)
        assert result == "x" * _LOG_CAP + "…"


class TestRedact:
    def test_should_redact_password(self) -> None:
        assert _redact({"password": "secret"}) == {"password": "***"}

    def test_should_preserve_non_sensitive_keys(self) -> None:
        params = {"driver": "postgres", "host": "localhost", "user": "alice"}
        assert _redact(params) == params

    def test_should_redact_password_while_preserving_other_keys(self) -> None:
        params = {"host": "localhost", "user": "alice", "password": "secret"}
        result = _redact(params)
        assert result["password"] == "***"
        assert result["host"] == "localhost"
        assert result["user"] == "alice"

    def test_should_return_empty_dict_unchanged(self) -> None:
        assert _redact({}) == {}


class TestServerLogging:
    async def test_should_log_sent_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        server = Server(out=io.BytesIO())
        with caplog.at_level(logging.DEBUG, logger="dbelveder.server"):
            await server._send(Result(id=1, result={"ok": True}, error=None))
        assert any("Sent" in r.message for r in caplog.records)
