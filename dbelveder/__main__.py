import argparse
import asyncio
import logging
import os
import pathlib
import sys
from dataclasses import dataclass

from .server import Server


logger = logging.getLogger()


def main() -> None:
    args = parse_cli_args()

    if args.log:
        log_path = _log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=log_path,
            level=logging.DEBUG,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )

    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    server = Server(cache_dir=cache_dir, max_concurrency=args.max_concurrency)

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("Server interrupted")
        print("Server interrupted", file=sys.stderr)
    except Exception as e:
        logger.exception(e)


def _cache_dir() -> pathlib.Path:
    cache_home = os.environ.get(
        "XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")
    )
    return pathlib.Path(cache_home) / "dbelveder"


def _log_path() -> pathlib.Path:
    state_home = os.environ.get(
        "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
    )
    return pathlib.Path(state_home) / "dbelveder" / "server.log"


@dataclass
class CliArgs:
    max_concurrency: int
    log: bool


def parse_cli_args() -> CliArgs:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=5,
        help="Define the max number of requests that can be executed at the same time per connection.",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        default=False,
        help=(
            "If set, all requests and responses will be logged to a file. "
            "Logs will be saved under `~/.local/state`."
        ),
    )
    args = parser.parse_args()

    return CliArgs(max_concurrency=args.max_concurrency, log=args.log)
