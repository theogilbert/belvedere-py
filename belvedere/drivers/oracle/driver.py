"""Oracle driver — requires: pip install oracledb"""

import logging
from typing import Any

import oracledb

from ...protocol import (
    ColumnDescription,
    ColumnInfo,
    ColumnsDescription,
    DescribeResult,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndicesDescription,
    Language,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from ..base import BaseDriver, ConnectionLostError, DriverError
from .queries import (
    apply_metadata_transform,
    build_column_index_lists,
    fetch_all_column_comments,
    fetch_column_details,
    fetch_column_index_mapping,
    fetch_column_names_and_types,
    fetch_column_sample,
    fetch_constraint_names_and_types,
    fetch_index_ddl,
    fetch_index_fields_for_index,
    fetch_index_fields_for_table,
    fetch_index_meta,
    fetch_index_metas_for_table,
    fetch_index_names_and_types,
    fetch_join_tables_for_index,
    fetch_join_tables_for_table,
    fetch_pk_columns,
    fetch_schemas,
    fetch_tables_and_views,
)

logger = logging.getLogger(__name__)


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
        self._metadata_transform_set = False
        """Set to True after DBMS_METADATA session transform params are applied.
        Cleared on reconnect so they are re-applied to the new session."""

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
            raise DriverError(_exc_message(exc)) from exc

    async def reconnect(self) -> None:
        try:
            await self._conn.close()
        except oracledb.InterfaceError:
            pass
        self._conn, self._has_oracle_maintained = await self._open(self.params)
        self._metadata_transform_set = False

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
        match path:
            case []:
                schemas = await fetch_schemas(self._conn, self._has_oracle_maintained)
                return [
                    ExploreItem(name=s, type="schema", expandable=True) for s in schemas
                ]

            case [schema]:
                pairs = await fetch_tables_and_views(self._conn, schema.upper())
                return [
                    ExploreItem(name=name, type=kind, expandable=True)
                    for name, kind in pairs
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                pairs = await fetch_column_names_and_types(
                    self._conn, schema.upper(), table.upper()
                )
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
                ]

            case [schema, table, "indexes"]:
                pairs = await fetch_index_names_and_types(
                    self._conn, schema.upper(), table.upper()
                )
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
                ]

            case [schema, table, "constraints"]:
                pairs = await fetch_constraint_names_and_types(
                    self._conn, schema.upper(), table.upper()
                )
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
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

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        try:
            return await self._explore_describe(path)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    async def _explore_describe(self, path: list[str]) -> DescribeResult:
        match path:
            case [schema, table]:
                return await self._describe_table(schema.upper(), table.upper())

            case [schema, table, "indexes"]:
                return await self._describe_indices(schema.upper(), table.upper())

            case [schema, table, "indexes", index_name]:
                return await self._describe_index(
                    schema.upper(), table.upper(), index_name.upper()
                )

            case [schema, table, "columns"]:
                return await self._describe_columns(schema.upper(), table.upper())

            case [schema, table, "columns", col_name]:
                return await self._describe_column(
                    schema.upper(), table.upper(), col_name.upper()
                )

            case _:
                return None

    async def _describe_table(self, schema: str, table: str) -> TableDescription:
        col_details = await fetch_column_details(self._conn, schema, table)
        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        col_index_map = await fetch_column_index_mapping(self._conn, schema, table)

        index_cols: dict[str, set[str]] = {}
        for col_name, idx_names in col_index_map.items():
            for idx_name in idx_names:
                index_cols.setdefault(idx_name, set()).add(col_name)
        index_col_count = {k: len(v) for k, v in index_cols.items()}

        return TableDescription(
            table=table,
            schema=schema,
            columns=[
                ColumnInfo(
                    name=col.name,
                    type=col.type,
                    nullable=col.nullable,
                    pk=col.name in pk_cols,
                    default=col.default,
                    exclusive_index=any(
                        index_col_count[i] == 1 for i in col_index_map.get(col.name, [])
                    ),
                    composite_index=any(
                        index_col_count[i] > 1 for i in col_index_map.get(col.name, [])
                    ),
                )
                for col in col_details
            ],
        )

    async def _describe_indices(self, schema: str, table: str) -> IndicesDescription:
        metas = await fetch_index_metas_for_table(self._conn, schema, table)
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        join_tables = await fetch_join_tables_for_table(self._conn, schema, table)

        indices = []
        for meta in metas:
            ddl = (
                None if meta.generated else await self._get_index_ddl(schema, meta.name)
            )
            indices.append(
                IndexDescription(
                    index=meta.name,
                    fields=fields_by_index.get(meta.name, []),
                    unique=meta.unique,
                    tables=join_tables.get(meta.name, [table]),
                    index_type=meta.index_type,
                    visible=meta.visible,
                    ddl=ddl,
                )
            )

        return IndicesDescription(indices=indices)

    async def _describe_index(
        self, schema: str, table: str, index_name: str
    ) -> IndexDescription | None:
        meta = await fetch_index_meta(self._conn, schema, index_name)
        if meta is None:
            return None

        fields = await fetch_index_fields_for_index(self._conn, schema, index_name)
        tables = await fetch_join_tables_for_index(
            self._conn, schema, index_name, table
        )
        ddl = None if meta.generated else await self._get_index_ddl(schema, index_name)

        return IndexDescription(
            index=index_name,
            fields=fields,
            unique=meta.unique,
            tables=tables,
            index_type=meta.index_type,
            visible=meta.visible,
            ddl=ddl,
        )

    async def _describe_columns(self, schema: str, table: str) -> ColumnsDescription:
        col_details = await fetch_column_details(self._conn, schema, table)
        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        all_indices = (await self._describe_indices(schema, table)).indices
        excl, comp = await build_column_index_lists(
            self._conn, schema, table, all_indices
        )
        comments = await fetch_all_column_comments(self._conn, schema, table)

        columns = []
        for col in col_details:
            sample = await fetch_column_sample(self._conn, schema, table, col.name)
            columns.append(
                ColumnDescription(
                    name=col.name,
                    data_type=col.type,
                    nullable=col.nullable,
                    pk=col.name in pk_cols,
                    default=col.default,
                    exclusive_indices=excl.get(col.name, []),
                    composite_indices=comp.get(col.name, []),
                    comment=comments.get(col.name),
                    sample=sample,
                )
            )
        return ColumnsDescription(columns=columns)

    async def _describe_column(
        self, schema: str, table: str, col_name: str
    ) -> ColumnDescription | None:
        col_details = await fetch_column_details(self._conn, schema, table)
        col = next((c for c in col_details if c.name == col_name), None)
        if col is None:
            return None

        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        all_indices = (await self._describe_indices(schema, table)).indices
        excl, comp = await build_column_index_lists(
            self._conn, schema, table, all_indices
        )
        comments = await fetch_all_column_comments(self._conn, schema, table)
        sample = await fetch_column_sample(self._conn, schema, table, col_name)

        return ColumnDescription(
            name=col.name,
            data_type=col.type,
            nullable=col.nullable,
            pk=col.name in pk_cols,
            default=col.default,
            exclusive_indices=excl.get(col_name, []),
            composite_indices=comp.get(col_name, []),
            comment=comments.get(col_name),
            sample=sample,
        )

    async def _ensure_metadata_transform(self) -> None:
        if self._metadata_transform_set:
            return
        await apply_metadata_transform(self._conn)
        self._metadata_transform_set = True

    async def _get_index_ddl(self, schema: str, index_name: str) -> str | None:
        try:
            await self._ensure_metadata_transform()
            return await fetch_index_ddl(self._conn, schema, index_name)
        except Exception as exc:
            logger.debug(
                "DBMS_METADATA.GET_DDL failed for index %s.%s: %s",
                schema,
                index_name,
                exc,
            )
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
        raise ConnectionLostError(_exc_message(exc)) from exc
    if isinstance(exc, oracledb.DatabaseError):
        error = exc.args[0] if exc.args else None
        if getattr(error, "is_session_dead", False):
            raise ConnectionLostError(_exc_message(exc)) from exc


def _format_db_error(exc: oracledb.DatabaseError, query: str) -> str:
    msg = _exc_message(exc)
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


def _exc_message(exc: BaseException) -> str:
    """Build an error message that includes the full exception cause chain."""
    msg = str(exc).rstrip()
    cause: BaseException | None = exc.__cause__ or exc.__context__
    seen: set[int] = {id(exc)}
    while cause is not None and id(cause) not in seen:
        detail = str(cause).strip()
        if detail and detail not in msg:
            msg = f"{msg} — {detail}"
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return msg
