from .base import BaseDriver


def get_driver(name: str) -> type[BaseDriver]:
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver

            return SQLiteDriver
        case "sqlserver":
            try:
                import mssql_python  # noqa: F401
            except ImportError:
                raise RuntimeError("mssql-python not installed — run: pip install mssql-python")
            from .sqlserver import SQLServerDriver

            return SQLServerDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")
