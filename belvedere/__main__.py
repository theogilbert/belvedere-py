import argparse
import asyncio
import faulthandler
import logging
import os
import pathlib
import sys
from dataclasses import dataclass

from belvedere.drivers.base import DriverSettings

from . import log
from .server import Server

logger = logging.getLogger()


def main() -> None:
    args = parse_cli_args()

    _enable_faulthandler()

    if args.log:
        log_path = _log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log.configure(log_path, logging.DEBUG if args.verbose else logging.INFO)

    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    server = Server(
        cache_dir=cache_dir,
        max_concurrency=args.max_concurrency,
        driver_settings=args.driver_settings,
    )

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
    return pathlib.Path(cache_home) / "belvedere"


def _log_path() -> pathlib.Path:
    state_home = os.environ.get(
        "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
    )
    return pathlib.Path(state_home) / "belvedere" / "server.log"


def _enable_faulthandler() -> None:
    """Dump the C stack to a file on fatal signals (segfaults in native drivers)."""
    crash_path = _log_path().with_name("crash.log")
    crash_path.parent.mkdir(parents=True, exist_ok=True)
    crash_file = crash_path.open("a")
    faulthandler.enable(file=crash_file)


@dataclass
class CliArgs:
    max_concurrency: int
    log: bool
    verbose: bool
    driver_settings: DriverSettings


def parse_cli_args() -> CliArgs:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=5,
        help="Define the max number of requests that can be executed at the same time per connection.",
    )

    logs_grp = parser.add_argument_group(
        "Logging settings",
        description="Settings affecting how logs are emitted",
    )
    logs_grp.add_argument(
        "--log",
        action="store_true",
        default=False,
        help=(
            "If set, all requests and responses will be logged to a file. "
            "Logs will be saved under `~/.local/state`."
        ),
    )
    logs_grp.add_argument(
        "-v",
        action="store_true",
        dest="verbose",
        default=False,
        help="Log at DEBUG level. Without this flag, only INFO and above are logged.",
    )

    driver_stgs_grp = parser.add_argument_group(
        "Driver settings",
        description="Settings affecting the behavior of individual drivers",
    )
    driver_stgs_grp.add_argument(
        "--column-sample-size",
        type=int,
        default=3,
        help="Number of distinct non-null values sampled per column in describe results.",
    )
    driver_stgs_grp.add_argument(
        "--column-sample-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for each column sample query before returning no sample.",
    )

    args = parser.parse_args()

    return CliArgs(
        max_concurrency=args.max_concurrency,
        log=args.log,
        verbose=args.verbose,
        driver_settings=DriverSettings(
            column_sample_size=args.column_sample_size,
            column_sample_timeout=args.column_sample_timeout,
        ),
    )
