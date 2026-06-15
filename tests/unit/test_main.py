import logging
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from belvedere.__main__ import main, parse_cli_args
from belvedere.server import Server


class TestVerboseFlag:
    def test_default_log_level_is_info(self) -> None:
        with patch("sys.argv", ["belvedere", "--log"]):
            args = parse_cli_args()
        assert args.verbose is False

    def test_verbose_flag_sets_verbose(self) -> None:
        with patch("sys.argv", ["belvedere", "--log", "-v"]):
            args = parse_cli_args()
        assert args.verbose is True

    def test_log_level_is_debug_when_verbose(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["belvedere", "--log", "-v"]),
            patch("belvedere.__main__._cache_dir", return_value=tmp_path),
            patch("belvedere.__main__._log_path", return_value=tmp_path / "server.log"),
            patch("logging.basicConfig") as mock_basicConfig,
            patch.object(Server, "run", new_callable=AsyncMock),
        ):
            main()
        mock_basicConfig.assert_called_once()
        assert mock_basicConfig.call_args.kwargs["level"] == logging.DEBUG

    def test_log_level_is_info_without_verbose(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["belvedere", "--log"]),
            patch("belvedere.__main__._cache_dir", return_value=tmp_path),
            patch("belvedere.__main__._log_path", return_value=tmp_path / "server.log"),
            patch("logging.basicConfig") as mock_basicConfig,
            patch.object(Server, "run", new_callable=AsyncMock),
        ):
            main()
        mock_basicConfig.assert_called_once()
        assert mock_basicConfig.call_args.kwargs["level"] == logging.INFO


class TestKeyboardInterrupt:
    def test_should_print_to_stderr(self, capsys: pytest.CaptureFixture, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["belvedere"]),
            patch("belvedere.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()
        assert "Server interrupted" in capsys.readouterr().err

    def test_should_log(self, caplog: pytest.LogCaptureFixture, tmp_path: pathlib.Path) -> None:
        with (
            caplog.at_level(logging.INFO),
            patch("sys.argv", ["belvedere"]),
            patch("belvedere.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()
        assert any("Server interrupted" in r.message for r in caplog.records)

    def test_should_not_raise(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sys.argv", ["belvedere"]),
            patch("belvedere.__main__._cache_dir", return_value=tmp_path),
            patch.object(Server, "run", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        ):
            main()  # must not propagate
