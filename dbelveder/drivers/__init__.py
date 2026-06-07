from .base import BaseDriver


def get_driver(name: str) -> type[BaseDriver]:
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver
            return SQLiteDriver
        case "postgres" | "postgresql":
            from .postgres import PostgresDriver
            return PostgresDriver
        case "sqlserver" | "mssql":
            from .sqlserver import SQLServerDriver
            return SQLServerDriver
        case "mongodb" | "mongo":
            from .mongodb import MongoDriver
            return MongoDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")
