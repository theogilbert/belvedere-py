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
        case "neo4j":
            try:
                import neo4j  # noqa: F401
            except ImportError:
                raise RuntimeError(
                    "neo4j not installed — run: pip install neo4j"
                )
            from .neo4j import Neo4jDriver

            return Neo4jDriver
        case _:
            raise ValueError(f"Unknown driver: {name!r}")


def list_drivers() -> list[Driver]:
    """Return capabilities for every driver available in the current environment."""
    from .sqlite import SQLiteDriver

    techs = [Driver(driver="sqlite", label="SQLite", params=SQLiteDriver.PARAMS)]
    try:
        import mssql_python  # noqa: F401
        from .sqlserver import SQLServerDriver

        techs.append(Driver(driver="sqlserver", label="SQL Server", params=SQLServerDriver.PARAMS))
    except ImportError:
        pass
    try:
        import neo4j  # noqa: F401
        from .neo4j import Neo4jDriver

        techs.append(Driver(driver="neo4j", label="Neo4j", params=Neo4jDriver.PARAMS))
    except ImportError:
        pass
    return techs
