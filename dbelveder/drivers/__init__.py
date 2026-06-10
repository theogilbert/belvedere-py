from .base import BaseDriver
from ..protocol import Driver


def get_driver(name: str) -> type[BaseDriver]:
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver

            return SQLiteDriver
        case "sqlserver":
            try:
                import mssql_python  # noqa: F401
            except ImportError:
                raise RuntimeError(
                    "mssql-python not installed — run: pip install mssql-python"
                )
            from .sqlserver import SQLServerDriver

            return SQLServerDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")


def list_drivers() -> list[Driver]:
    """Return capabilities for every driver available in the current environment."""
    from .sqlite import SQLiteDriver

    techs = [Driver(driver="sqlite", params=SQLiteDriver.PARAMS)]
    try:
        import mssql_python  # noqa: F401
        from .sqlserver import SQLServerDriver

        techs.append(Driver(driver="sqlserver", params=SQLServerDriver.PARAMS))
    except ImportError:
        pass
    return techs
