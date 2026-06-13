"""Oracle driver — requires: pip install oracledb"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from ..protocol import (
    ColumnInfo,
    DMLResult,
    ExploreItem,
    SelectResult,
    TableDescription,
    DriverParam,
)
from .base import BaseDriver, ConnectionLostError

T = TypeVar("T")

_CONSTRAINT_TYPE = {"P": "primary_key", "U": "unique", "C": "check", "R": "foreign_key"}

# Pre-12c fallback: ORACLE_MAINTAINED column doesn't exist before 12.1, so we
# exclude known system schemas by name instead.
_PRE12_SYSTEM_SCHEMAS_SQL = ", ".join(
    f"'{u}'" for u in sorted({
        "ANONYMOUS", "APEX_030200", "APEX_040000", "APPQOSSYS", "AUDSYS",
        "CTXSYS", "DBSFWUSER", "DBSNMP", "DIP", "DVF", "DVSYS", "EXFSYS",
        "FLOWS_FILES", "GGSYS", "GSMADMIN_INTERNAL", "LBACSYS", "MDDATA",
        "MDSYS", "OJVMSYS", "OLAPSYS", "ORACLE_OCM", "ORDDATA", "ORDPLUGINS",
        "ORDSYS", "OUTLN", "SI_INFORMTN_SCHEMA", "SYS", "SYSBACKUP", "SYSDG",
        "SYSKM", "SYSRAC", "SYSTEM", "WMSYS", "XDB", "XS$NULL",
    })
)


class OracleDriver(BaseDriver):
    """Oracle driver backed by python-oracledb (thin mode, no Instant Client required).

    Args:
        params: Connect request fields (``host``, ``port``, ``service_name``, ``user``, ``password``).
        conn: Open oracledb connection. Use :meth:`create` instead of constructing directly.
        has_oracle_maintained: True when connected to Oracle 12c+.
    """

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type="string", label="Host", default="localhost"),
        DriverParam(key="port", type="integer", label="Port", default=1521),
        DriverParam(key="service_name", type="string", label="Service Name", default="FREEPDB1"),
        DriverParam(key="user", type="string", label="User"),
        DriverParam(key="password", type="string", label="Password", secret=True),
    ]

    HELP: str = """\
## Oracle

**Install:** `pip install oracledb` — thin mode, no Oracle Instant Client required.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | no | `localhost` | Server hostname or IP |
| `port` | no | `1521` | Listener port |
| `service_name` | no | `FREEPDB1` | Database service name |
| `user` | no | — | Username |
| `password` | no | — | Password (masked) |

**Queries:** Standard SQL. Positional bind parameters use `:1`, `:2`, … placeholders.

```sql
SELECT * FROM employees WHERE department_id = :1 AND hire_date > :2
```

**Explore tree:**

```
(root)  ← non-system schemas (ALL_USERS where ORACLE_MAINTAINED = 'N')
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indexes      → name, index type
        └── constraints  → name, type (primary_key, unique, check, foreign_key)
```

`explore.describe` is supported on `[schema, table]` paths and returns full
column metadata (name, type, nullability, primary key flag, default).
"""

    def __init__(self, params: dict[str, Any], conn: Any, has_oracle_maintained: bool) -> None:
        super().__init__(params)
        self._conn = conn
        self._has_oracle_maintained = has_oracle_maintained

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "OracleDriver":
        try:
            import oracledb  # noqa: F401
        except ImportError:
            raise RuntimeError("oracledb not installed — run: pip install oracledb")
        conn, has_oracle_maintained = await cls._open(params)
        return cls(params, conn, has_oracle_maintained)

    @staticmethod
    async def _open(params: dict[str, Any]) -> tuple[Any, bool]:
        import oracledb
        loop = asyncio.get_running_loop()

        def connect() -> tuple[Any, bool]:
            conn = oracledb.connect(
                user=params.get("user", ""),
                password=params.get("password", ""),
                dsn=(
                    f"{params.get('host', 'localhost')}"
                    f":{params.get('port', 1521)}"
                    f"/{params.get('service_name', 'FREEPDB1')}"
                ),
            )
            conn.autocommit = True
            major_version = int(conn.version.split(".")[0])
            return conn, major_version >= 12

        return await loop.run_in_executor(None, connect)

    async def reconnect(self) -> None:
        await self._run(self._conn.close)
        self._conn, self._has_oracle_maintained = await self._open(self.params)

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    async def execute(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:
        """Run a SQL statement. Positional bind values map to ``:1``, ``:2``, … in the query.

        Args:
            sql: SQL statement to execute.
            binds: Positional bind parameters (referenced as ``:1``, ``:2``, … in the query).

        Returns:
            SelectResult for queries that return rows, DMLResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        try:
            return await self._run(self._execute_sync, sql, binds)
        except Exception as exc:
            try:
                import oracledb
                if isinstance(exc, (oracledb.OperationalError, oracledb.InterfaceError)):
                    raise ConnectionLostError(str(exc)) from exc
            except ImportError:
                pass
            raise

    def _execute_sync(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:
        cur = self._conn.cursor()
        cur.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
            return SelectResult(columns=columns, rows=rows)
        return DMLResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        cur = self._conn.cursor()
        match path:
            case []:
                if self._has_oracle_maintained:
                    cur.execute(
                        "SELECT USERNAME FROM ALL_USERS"
                        " WHERE ORACLE_MAINTAINED = 'N' ORDER BY USERNAME"
                    )
                else:
                    cur.execute(
                        "SELECT USERNAME FROM ALL_USERS"
                        f" WHERE USERNAME NOT IN ({_PRE12_SYSTEM_SCHEMAS_SQL})"
                        " ORDER BY USERNAME"
                    )
                return [
                    ExploreItem(name=r[0], type="schema", expandable=True)
                    for r in cur.fetchall()
                ]

            case [schema]:
                cur.execute(
                    "SELECT TABLE_NAME AS N, 'table' AS T FROM ALL_TABLES WHERE OWNER = :1"
                    " UNION ALL"
                    " SELECT VIEW_NAME, 'view' FROM ALL_VIEWS WHERE OWNER = :2"
                    " ORDER BY 1",
                    [schema.upper(), schema.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=True)
                    for r in cur.fetchall()
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS"
                    " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
                    [schema.upper(), table.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in cur.fetchall()
                ]

            case [schema, table, "indexes"]:
                cur.execute(
                    "SELECT INDEX_NAME, LOWER(INDEX_TYPE) FROM ALL_INDEXES"
                    " WHERE TABLE_OWNER = :1 AND TABLE_NAME = :2 ORDER BY INDEX_NAME",
                    [schema.upper(), table.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in cur.fetchall()
                ]

            case [schema, table, "constraints"]:
                cur.execute(
                    "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE FROM ALL_CONSTRAINTS"
                    " WHERE OWNER = :1 AND TABLE_NAME = :2"
                    " AND CONSTRAINT_TYPE IN ('P', 'U', 'C', 'R')"
                    " AND STATUS = 'ENABLED' AND GENERATED = 'USER NAME'"
                    " ORDER BY CONSTRAINT_NAME",
                    [schema.upper(), table.upper()],
                )
                return [
                    ExploreItem(
                        name=r[0],
                        type=_CONSTRAINT_TYPE.get(r[1], r[1].lower()),
                        expandable=False,
                    )
                    for r in cur.fetchall()
                ]

            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> TableDescription | None:
        match path:
            case [schema, table]:
                schema_up = schema.upper()
                table_up = table.upper()
                cur = self._conn.cursor()

                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT"
                    " FROM ALL_TAB_COLUMNS"
                    " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
                    [schema_up, table_up],
                )
                col_rows = cur.fetchall()

                cur.execute(
                    "SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS con"
                    " JOIN ALL_CONS_COLUMNS cc"
                    "  ON con.OWNER = cc.OWNER"
                    "  AND con.CONSTRAINT_NAME = cc.CONSTRAINT_NAME"
                    "  AND con.TABLE_NAME = cc.TABLE_NAME"
                    " WHERE con.OWNER = :1 AND con.TABLE_NAME = :2"
                    "  AND con.CONSTRAINT_TYPE = 'P'",
                    [schema_up, table_up],
                )
                pk_cols = {r[0] for r in cur.fetchall()}

                return TableDescription(
                    table=table_up,
                    schema=schema_up,
                    columns=[
                        ColumnInfo(
                            name=r[0],
                            type=r[1],
                            nullable=r[2] == "Y",
                            pk=r[0] in pk_cols,
                            default=r[3].strip() if r[3] is not None else None,
                        )
                        for r in col_rows
                    ],
                )
            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs)
        )
