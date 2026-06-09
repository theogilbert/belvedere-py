from .base import BaseDriver


def get_driver(name: str) -> type[BaseDriver]:
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver

            return SQLiteDriver
        case "sqlserver":
            from .sqlserver import SQLServerDriver

            return SQLServerDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")
