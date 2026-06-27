"""Oracle driver — requires: pip install oracledb"""

from typing import Any

import oracledb

from ..protocol import (
    ColumnInfo,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    Language,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from .base import BaseDriver, ConnectionLostError, DriverError

_USED_IMPORTS = (
    IndexDescription,
    IndexKeyField,
)  # referenced below in _explore_describe

_CONSTRAINT_TYPE = {"P": "primary_key", "U": "unique", "C": "check", "R": "foreign_key"}

# Pre-12c fallback: ORACLE_MAINTAINED column doesn't exist before 12.1, so we
# exclude known system schemas by name instead.
_PRE12_SYSTEM_SCHEMAS_SQL = ", ".join(
    f"'{u}'"
    for u in sorted(
        {
            "ANONYMOUS",
            "APEX_030200",
            "APEX_040000",
            "APPQOSSYS",
            "AUDSYS",
            "CTXSYS",
            "DBSFWUSER",
            "DBSNMP",
            "DIP",
            "DVF",
            "DVSYS",
            "EXFSYS",
            "FLOWS_FILES",
            "GGSYS",
            "GSMADMIN_INTERNAL",
            "LBACSYS",
            "MDDATA",
            "MDSYS",
            "OJVMSYS",
            "OLAPSYS",
            "ORACLE_OCM",
            "ORDDATA",
            "ORDPLUGINS",
            "ORDSYS",
            "OUTLN",
            "SI_INFORMTN_SCHEMA",
            "SYS",
            "SYSBACKUP",
            "SYSDG",
            "SYSKM",
            "SYSRAC",
            "SYSTEM",
            "WMSYS",
            "XDB",
            "XS$NULL",
        }
    )
)


class OracleDriver(BaseDriver):
    """Oracle driver backed by python-oracledb (thin mode, no Instant Client required).

    Args:
        params: Connect request fields (``host``, ``port``, ``service_name``, ``user``, ``password``).
        conn: Open oracledb async connection. Use :meth:`create` instead of constructing directly.
        has_oracle_maintained: True when connected to Oracle 12c+.
    """

    LABEL = "Oracle"
    LANGUAGES = [Language.SQL]

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type=ParamType.STRING, label="Host"),
        DriverParam(key="port", type=ParamType.INTEGER, label="Port", default=1521),
        DriverParam(key="service_name", type=ParamType.STRING, label="Service Name"),
        DriverParam(key="user", type=ParamType.STRING, label="User"),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            secret=True,
            required=False,
        ),
    ]

    HELP: str = """\
## Oracle

**Install:** `pip install oracledb` — thin mode, no Oracle Instant Client required.

| Parameter      | Required | Default     | Description           |
|----------------|----------|-------------|-----------------------|
| `host`         | no       | `localhost` | Server hostname or IP |
| `port`         | no       | `1521`      | Listener port         |
| `service_name` | no       | `FREEPDB1`  | Database service name |
| `user`         | no       | —           | Username              |
| `password`     | no       | —           | Password (masked)     |

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

`explore.describe` is supported on `[schema, table]` paths (column metadata:
name, type, nullability, primary key flag, default) and on
`[schema, table, "indexes", index_name]` paths (key fields, direction, uniqueness).
"""

    def __init__(
        self, params: dict[str, Any], conn: Any, has_oracle_maintained: bool
    ) -> None:
        super().__init__(params)
        self._conn = conn
        self._has_oracle_maintained = has_oracle_maintained
        """True when connected to Oracle 12c+; enables ORACLE_MAINTAINED column filter."""

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "OracleDriver":
        conn, has_oracle_maintained = await cls._open(params)
        return cls(params, conn, has_oracle_maintained)

    @staticmethod
    async def _open(params: dict[str, Any]) -> tuple[Any, bool]:
        try:
            conn = await oracledb.connect_async(
                user=params.get("user", ""),
                password=params.get("password", ""),
                dsn=(
                    f"{params.get('host', 'localhost')}"
                    f":{params.get('port', 1521)}"
                    f"/{params.get('service_name', 'FREEPDB1')}"
                ),
            )
            major_version = int(conn.version.split(".")[0])
            return conn, major_version >= 12
        except oracledb.DatabaseError as exc:
            raise DriverError(str(exc)) from exc

    async def reconnect(self) -> None:
        await self._conn.close()
        self._conn, self._has_oracle_maintained = await self._open(self.params)

    async def disconnect(self) -> None:
        await self._conn.close()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a SQL statement. Positional bind values map to ``:1``, ``:2``, … in the query.

        Args:
            query: SQL statement to execute.
            binds: Positional bind parameters (referenced as ``:1``, ``:2``, … in the query).

        Returns:
            ReadResult for queries that return rows, WriteResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        try:
            cur = self._conn.cursor()
            await cur.execute(query, binds)
            if _is_explain_plan(query):
                await cur.execute(
                    "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY())"
                )
                rows: list[list[Any]] = [[r[0]] for r in await cur.fetchall()]
                return ReadResult(
                    columns=["PLAN_TABLE_OUTPUT"], rows=rows, rows_total=len(rows)
                )
            if cur.description is not None:
                columns = [d[0] for d in cur.description]
                rows = [list(r) for r in await cur.fetchall()]
                return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
            return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            if isinstance(exc, oracledb.DatabaseError):
                raise DriverError(_format_db_error(exc, query)) from exc
            raise

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        try:
            return await self._explore_list(path)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    async def _explore_list(self, path: list[str]) -> list[ExploreItem]:
        cur = self._conn.cursor()
        match path:
            case []:
                if self._has_oracle_maintained:
                    await cur.execute(
                        "SELECT USERNAME FROM ALL_USERS"
                        " WHERE ORACLE_MAINTAINED = 'N' ORDER BY USERNAME"
                    )
                else:
                    await cur.execute(
                        "SELECT USERNAME FROM ALL_USERS"
                        f" WHERE USERNAME NOT IN ({_PRE12_SYSTEM_SCHEMAS_SQL})"
                        " ORDER BY USERNAME"
                    )
                return [
                    ExploreItem(name=r[0], type="schema", expandable=True)
                    for r in await cur.fetchall()
                ]

            case [schema]:
                await cur.execute(
                    "SELECT TABLE_NAME AS N, 'table' AS T FROM ALL_TABLES WHERE OWNER = :1"
                    " UNION ALL"
                    " SELECT VIEW_NAME, 'view' FROM ALL_VIEWS WHERE OWNER = :2"
                    " ORDER BY 1",
                    [schema.upper(), schema.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=True)
                    for r in await cur.fetchall()
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                await cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS"
                    " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
                    [schema.upper(), table.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in await cur.fetchall()
                ]

            case [schema, table, "indexes"]:
                await cur.execute(
                    "SELECT INDEX_NAME, LOWER(INDEX_TYPE) FROM ALL_INDEXES"
                    " WHERE TABLE_OWNER = :1 AND TABLE_NAME = :2 ORDER BY INDEX_NAME",
                    [schema.upper(), table.upper()],
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in await cur.fetchall()
                ]

            case [schema, table, "constraints"]:
                await cur.execute(
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
                    for r in await cur.fetchall()
                ]

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(
                    f'SELECT * FROM "{schema}"."{table}" FETCH FIRST 10 ROWS ONLY', []
                )
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        try:
            return await self._explore_describe(path)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    async def _explore_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        match path:
            case [schema, table]:
                schema_up = schema.upper()
                table_up = table.upper()
                cur = self._conn.cursor()

                await cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT"
                    " FROM ALL_TAB_COLUMNS"
                    " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
                    [schema_up, table_up],
                )
                col_rows = await cur.fetchall()

                await cur.execute(
                    "SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS con"
                    " JOIN ALL_CONS_COLUMNS cc"
                    "  ON con.OWNER = cc.OWNER"
                    "  AND con.CONSTRAINT_NAME = cc.CONSTRAINT_NAME"
                    "  AND con.TABLE_NAME = cc.TABLE_NAME"
                    " WHERE con.OWNER = :1 AND con.TABLE_NAME = :2"
                    "  AND con.CONSTRAINT_TYPE = 'P'",
                    [schema_up, table_up],
                )
                pk_cols = {r[0] for r in await cur.fetchall()}

                await cur.execute(
                    "SELECT aic.COLUMN_NAME, aic.INDEX_NAME"
                    " FROM ALL_IND_COLUMNS aic"
                    " JOIN ALL_INDEXES ai"
                    "  ON aic.INDEX_OWNER = ai.OWNER AND aic.INDEX_NAME = ai.INDEX_NAME"
                    " WHERE ai.TABLE_OWNER = :1 AND ai.TABLE_NAME = :2",
                    [schema_up, table_up],
                )
                col_indexes: dict[str, list[str]] = {}
                index_cols: dict[str, set[str]] = {}
                for col_name, idx_name in await cur.fetchall():
                    col_indexes.setdefault(col_name, []).append(idx_name)
                    index_cols.setdefault(idx_name, set()).add(col_name)
                index_col_count = {k: len(v) for k, v in index_cols.items()}

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
                            exclusive_index=any(
                                index_col_count[i] == 1
                                for i in col_indexes.get(r[0], [])
                            ),
                            composite_index=any(
                                index_col_count[i] > 1
                                for i in col_indexes.get(r[0], [])
                            ),
                        )
                        for r in col_rows
                    ],
                )

            case [schema, table, "indexes", index_name]:
                schema_up = schema.upper()
                index_up = index_name.upper()
                cur = self._conn.cursor()

                await cur.execute(
                    "SELECT UNIQUENESS FROM ALL_INDEXES"
                    " WHERE OWNER = :1 AND INDEX_NAME = :2",
                    [schema_up, index_up],
                )
                row = await cur.fetchone()
                if row is None:
                    return None

                await cur.execute(
                    "SELECT COLUMN_NAME, DESCEND FROM ALL_IND_COLUMNS"
                    " WHERE INDEX_OWNER = :1 AND INDEX_NAME = :2"
                    " ORDER BY COLUMN_POSITION",
                    [schema_up, index_up],
                )
                fields = [
                    IndexKeyField(
                        name=r[0], direction="desc" if r[1] == "DESC" else "asc"
                    )
                    for r in await cur.fetchall()
                ]

                return IndexDescription(
                    index=index_name,
                    fields=fields,
                    unique=row[0] == "UNIQUE",
                    entity=table.upper(),
                )

            case _:
                return None


def _is_explain_plan(query: str) -> bool:
    for line in query.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.upper().startswith("EXPLAIN PLAN")
    return False


def _maybe_raise_connection_lost(exc: Exception) -> None:
    if isinstance(exc, (oracledb.OperationalError, oracledb.InterfaceError)):
        raise ConnectionLostError(str(exc)) from exc
    if isinstance(exc, oracledb.DatabaseError):
        error = exc.args[0] if exc.args else None
        if getattr(error, "is_session_dead", False):
            raise ConnectionLostError(str(exc)) from exc


def _format_db_error(exc: oracledb.DatabaseError, query: str) -> str:
    msg = str(exc)
    error = exc.args[0] if exc.args else None
    offset = getattr(error, "offset", 0)
    if offset > 0:
        line, col = _offset_to_line_col(query, offset)
        msg = f"{msg} (line {line}, col {col})"
    return msg


def _offset_to_line_col(query: str, offset: int) -> tuple[int, int]:
    segment = query[:offset]
    line = segment.count("\n") + 1
    col = offset - segment.rfind("\n")
    return line, col
