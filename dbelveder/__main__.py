import asyncio
from .server import Server


def main() -> None:
    asyncio.run(Server().run())


if __name__ == "__main__":
    main()
