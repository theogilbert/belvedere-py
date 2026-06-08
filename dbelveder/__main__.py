import asyncio
from .server import Server


def main() -> None:
    asyncio.run(Server().run())
