from .base import BaseDriver
from ..protocol import Driver


def get_driver_help(name: str) -> str:
    """Return the HELP string for a named driver without importing its optional package."""
    match name.lower():
        case "sqlite":
            from .sqlite import SQLiteDriver
            return SQLiteDriver.HELP
        case "sqlserver":
            from .sqlserver import SQLServerDriver
            return SQLServerDriver.HELP
        case "neo4j":
            from .neo4j import Neo4jDriver
            return Neo4jDriver.HELP
        case "oracle":
            from .oracle import OracleDriver
            return OracleDriver.HELP
        case "mongodb":
            from .mongodb import MongoDriver
            return MongoDriver.HELP
        case _:
            raise ValueError(f"Unknown driver: {name!r}")


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
        case "oracle":
            try:
                import oracledb  # noqa: F401
            except ImportError:
                raise RuntimeError(
                    "oracledb not installed — run: pip install oracledb"
                )
            from .oracle import OracleDriver

            return OracleDriver
        case "mongodb":
            try:
                import pymongo  # noqa: F401
            except ImportError:
                raise RuntimeError(
                    "pymongo not installed — run: pip install pymongo"
                )
            from .mongodb import MongoDriver

            return MongoDriver
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
    try:
        import oracledb  # noqa: F401
        from .oracle import OracleDriver

        techs.append(Driver(driver="oracle", label="Oracle", params=OracleDriver.PARAMS))
    except ImportError:
        pass
    try:
        import pymongo  # noqa: F401
        from .mongodb import MongoDriver

        techs.append(Driver(driver="mongodb", label="MongoDB", params=MongoDriver.PARAMS))
    except ImportError:
        pass
    return techs
