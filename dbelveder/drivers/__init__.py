from .base import BaseDriver


def get_driver(name: str) -> type[BaseDriver]:
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver

            return SQLiteDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")
