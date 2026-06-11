import io
import json
import logging
import os
import pathlib

import pytest

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


async def _run_server(tmp_path: pathlib.Path, *lines: str) -> io.BytesIO:
    """Write lines to a pipe, run the server to EOF, return the output buffer."""
    r, w = os.pipe()
    stdin_r = os.fdopen(r, "rb", buffering=0)
    stdin_w = os.fdopen(w, "wb", buffering=0)
    for line in lines:
        stdin_w.write(line.encode())
    stdin_w.close()
    out = io.BytesIO()
    await Server(stdin=stdin_r, stdout=out, cache_dir=tmp_path).run()
    return out


def _req(**kwargs: object) -> str:
    return json.dumps(kwargs) + "\n"


class TestRunLoop:
    async def test_non_dict_params_produces_no_response_and_logs_warning(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbelveder.server"):
            out = await _run_server(
                tmp_path, _req(id=1, method="capabilities", params=[])
            )
        assert out.getvalue() == b""
        assert any("params must be an object" in r.message for r in caplog.records)

    async def test_loop_continues_after_non_dict_params(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server(
            tmp_path,
            _req(id=1, method="capabilities", params=42),
            _req(id=2, method="capabilities", params={}),
        )
        ids = [json.loads(line)["id"] for line in out.getvalue().splitlines()]
        assert ids == [2]


class TestServerLogging:
    async def test_should_log_sent_message(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="dbelveder.server"):
            await _run_server(tmp_path, _req(id=1, method="capabilities", params={}))
        assert any("Sent" in r.message for r in caplog.records)

    async def test_should_log_ready_on_start(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="dbelveder.server"):
            await _run_server(tmp_path)
        assert any("ready" in r.message.lower() for r in caplog.records)

    async def test_should_log_exit_on_eof(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="dbelveder.server"):
            await _run_server(tmp_path)
        assert any("exit" in r.message.lower() for r in caplog.records)
