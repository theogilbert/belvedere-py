from dataclasses import dataclass
import argparse
import asyncio

from .server import Server


def main() -> None:
    args = parse_cli_args()
    asyncio.run(Server(max_concurrency=args.max_concurrency).run())


@dataclass
class CliArgs:
    max_concurrency: int


def parse_cli_args() -> CliArgs:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-concurrency", type=int, default=5)
    args = parser.parse_args()

    return CliArgs(max_concurrency=args.max_concurrency)
