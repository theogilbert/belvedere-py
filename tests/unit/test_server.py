import asyncio
import io
import json
import logging
import os
import pathlib
from unittest.mock import patch

import pytest

from grannos.dispatcher import Dispatcher
from grannos.drivers.base import DriverSettings
from grannos.log import LOG_CAP, truncate
from grannos.server import Server, _human_size, _redact


class TestTruncate:
    def test_should_return_string_unchanged_when_under_cap(self) -> None:
        text = "x" * LOG_CAP
        assert truncate(text) == text

    def test_should_truncate_and_append_ellipsis_when_over_cap(self) -> None:
        text = "x" * (LOG_CAP + 10)
        result = truncate(text)
        assert len(result) == LOG_CAP + 1  # cap chars + ellipsis character
        assert result.endswith("…")

    def test_should_truncate_at_cap_boundary(self) -> None:
        text = "x" * (LOG_CAP + 1)
        result = truncate(text)
        assert result == "x" * LOG_CAP + "…"


class TestHumanSize:
    def test_renders_whole_mebibytes(self) -> None:
        assert _human_size(16 * 1024 * 1024) == "16 MiB"

    def test_falls_back_to_kibibytes(self) -> None:
        assert _human_size(64 * 1024) == "64 KiB"

    def test_falls_back_to_bytes_when_not_a_whole_unit(self) -> None:
        assert _human_size(1500) == "1500 bytes"


class TestRedact:
    def test_redacts_password(self) -> None:
        assert _redact({"password": "secret"}) == {"password": "***"}

    def test_preserves_non_sensitive_keys(self) -> None:
        params = {"driver": "sqlite", "host": "localhost", "user": "alice"}
        assert _redact(params) == params

    def test_redacts_password_while_preserving_other_keys(self) -> None:
        result = _redact({"host": "localhost", "user": "alice", "password": "secret"})
        assert result == {"host": "localhost", "user": "alice", "password": "***"}

    def test_empty_dict_unchanged(self) -> None:
        assert _redact({}) == {}


async def _run_server_streaming(
    tmp_path: pathlib.Path, *lines: str, max_request_bytes: int = 1024 * 1024
) -> io.BytesIO:
    """Like _run_server, but feeds stdin from a thread so a line may exceed the pipe buffer."""
    r, w = os.pipe()
    stdin_r = os.fdopen(r, "rb", buffering=0)
    stdin_w = os.fdopen(w, "wb", buffering=0)

    def _feed() -> None:
        for line in lines:
            stdin_w.write(line.encode())
        stdin_w.close()

    out = io.BytesIO()
    srv = Server(
        stdin=stdin_r,
        stdout=out,
        cache_dir=tmp_path,
        driver_settings=DriverSettings(),
        max_request_bytes=max_request_bytes,
    )
    await asyncio.gather(asyncio.to_thread(_feed), srv.run())
    return out


async def _run_server(tmp_path: pathlib.Path, *lines: str) -> io.BytesIO:
    """Write lines to a pipe, run the server to EOF, return the output buffer."""
    r, w = os.pipe()
    stdin_r = os.fdopen(r, "rb", buffering=0)
    stdin_w = os.fdopen(w, "wb", buffering=0)
    for line in lines:
        stdin_w.write(line.encode())
    stdin_w.close()
    out = io.BytesIO()
    srv = Server(
        stdin=stdin_r, stdout=out, cache_dir=tmp_path, driver_settings=DriverSettings()
    )
    await srv.run()
    return out


def _req(**kwargs: object) -> str:
    return json.dumps(kwargs) + "\n"


class TestRunLoop:
    async def test_non_dict_params_produces_error_response_and_logs_warning(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="grannos.server"):
            out = await _run_server(
                tmp_path, _req(id=1, method="capabilities", params=[])
            )
        msg = json.loads(out.getvalue())
        assert msg["id"] == 1
        assert msg["error"] is not None
        assert any("params must be a JSON object" in r.message for r in caplog.records)

    async def test_loop_continues_after_non_dict_params(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server(
            tmp_path,
            _req(id=1, method="capabilities", params=42),
            _req(id=2, method="capabilities", params={}),
        )
        ids = [json.loads(line)["id"] for line in out.getvalue().splitlines()]
        assert ids == [1, 2]

    async def test_accepts_request_longer_than_asyncio_default_limit(
        self, tmp_path: pathlib.Path
    ) -> None:
        query = "-- " + "x" * (128 * 1024)
        out = await _run_server_streaming(
            tmp_path,
            _req(id=1, method="cancel", params={"request_id": 99, "pad": query}),
        )
        msg = json.loads(out.getvalue())
        assert msg == {"id": 1, "result": {"ok": True}, "error": None}

    async def test_oversized_request_is_rejected_without_killing_the_loop(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="grannos.server"):
            out = await _run_server_streaming(
                tmp_path,
                _req(id=1, method="execute", params={"query": "x" * 4096}),
                _req(id=2, method="capabilities", params={}),
                max_request_bytes=256,
            )
        messages = [json.loads(line) for line in out.getvalue().splitlines()]
        assert len(messages) == 2, "the discarded tail must not produce a second error"
        assert messages[0]["id"] == 1
        assert "too large" in messages[0]["error"]
        assert messages[1]["id"] == 2
        assert messages[1]["error"] is None
        assert any("oversized" in r.message.lower() for r in caplog.records)

    async def test_oversized_request_reports_no_id_when_id_is_not_the_first_key(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server_streaming(
            tmp_path,
            json.dumps({"method": "execute", "params": {"query": "x" * 4096}, "id": 1})
            + "\n",
            max_request_bytes=256,
        )
        msg = json.loads(out.getvalue())
        assert msg["id"] is None
        assert "too large" in msg["error"]

    async def test_oversized_request_id_is_not_taken_from_the_query_text(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server_streaming(
            tmp_path,
            _req(method="execute", params={"query": '{"id": 7,' + "x" * 4096}),
            max_request_bytes=256,
        )
        assert json.loads(out.getvalue())["id"] is None


class TestServerLogging:
    async def test_should_log_sent_message(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="grannos.server"):
            await _run_server(tmp_path, _req(id=1, method="capabilities", params={}))
        assert any("Sent" in r.message for r in caplog.records)

    async def test_should_log_ready_on_start(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="grannos.server"):
            await _run_server(tmp_path)
        assert any("ready" in r.message.lower() for r in caplog.records)

    async def test_should_log_exit_on_eof(
        self,
        caplog: pytest.LogCaptureFixture,
        tmp_path: pathlib.Path,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="grannos.server"):
            await _run_server(tmp_path)
        assert any("exit" in r.message.lower() for r in caplog.records)


class TestCancel:
    async def test_returns_ok_for_unknown_request_id(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server(
            tmp_path, _req(id=1, method="cancel", params={"request_id": 99})
        )
        msg = json.loads(out.getvalue())
        assert msg == {"id": 1, "result": {"ok": True}, "error": None}

    async def test_returns_error_when_request_id_is_missing(
        self, tmp_path: pathlib.Path
    ) -> None:
        out = await _run_server(tmp_path, _req(id=1, method="cancel", params={}))
        msg = json.loads(out.getvalue())
        assert msg["id"] == 1
        assert msg["error"] is not None

    async def test_cancelled_task_receives_cancelled_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        gate = asyncio.Event()

        async def slow_dispatch(
            self_d: Dispatcher, method: object, params: object, send_progress: object
        ) -> dict:
            await gate.wait()
            return {}

        with patch.object(Dispatcher, "dispatch", slow_dispatch):
            out = await _run_server(
                tmp_path,
                _req(
                    id=1, method="execute", params={"connection_id": "0", "query": "q"}
                ),
                _req(id=2, method="cancel", params={"request_id": 1}),
            )

        messages = {
            json.loads(line)["id"]: json.loads(line)
            for line in out.getvalue().splitlines()
        }
        assert messages[1]["error"] == "cancelled"
        assert messages[2] == {"id": 2, "result": {"ok": True}, "error": None}
