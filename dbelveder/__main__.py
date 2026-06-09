import argparse
import asyncio
import sys
from dataclasses import dataclass

from .server import Server


def main() -> None:
    args = parse_cli_args()

    out = sys.stdout.buffer
    server = Server(out, max_concurrency=args.max_concurrency)

    asyncio.run(server.run())


@dataclass
class CliArgs:
    max_concurrency: int


def parse_cli_args() -> CliArgs:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-concurrency", type=int, default=5)
    args = parser.parse_args()

    return CliArgs(max_concurrency=args.max_concurrency)
