import logging
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from dbelveder.__main__ import main
from dbelveder.server import Server


class TestKeyboardInterrupt:
    def test_should_print_to_stderr(self, capsys: pytest.CaptureFixture, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["dbelveder"]),
            patch("dbelveder.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()
        assert "Server interrupted" in capsys.readouterr().err

    def test_should_log(self, caplog: pytest.LogCaptureFixture, tmp_path: pathlib.Path) -> None:
        with (
            caplog.at_level(logging.INFO),
            patch("sys.argv", ["dbelveder"]),
            patch("dbelveder.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()
        assert any("Server interrupted" in r.message for r in caplog.records)

    def test_should_not_raise(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["dbelveder"]),
            patch("dbelveder.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()  # must not propagate
