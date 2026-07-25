"""Oracle driver — requires: pip install oracledb"""

import asyncio
import logging
import re
from typing import Any

import oracledb
from oracledb import AsyncConnection

from ...protocol import (
    DescribeResult,
    DriverParam,
    EntityDescription,
    ExploreItem,
    FieldDescription,
    IndexDescription,
    Language,
    ParamType,
    ReadResult,
    TableReference,
    WriteResult,
)
from ..base import (
    BaseDriver,
    ConnectionLostError,
    DriverError,
    DriverSettings,
    build_column_samples,
    find_reference,
    group_references_by_column,
    group_references_by_ref_column,
)
from .queries import (
    apply_metadata_transform,
    build_column_index_lists,
    build_preview_query,
    fetch_all_column_comments,
    fetch_column_details,
    fetch_column_names_and_types,
    fetch_explain_plan,
    fetch_index_ddl,
    fetch_index_fields_for_index,
    invalidate_cache,
    fetch_index_fields_for_table,
    fetch_index_meta,
    fetch_index_metas_for_table,
    fetch_incoming_references,
    fetch_index_names_and_types,
    fetch_join_tables_for_index,
    fetch_join_tables_for_table,
    fetch_column_sample,
    fetch_outgoing_references,
    fetch_pk_columns,
    fetch_schemas,
    fetch_table_comment,
    fetch_table_sample_rows,
    fetch_tables_and_views,
    render_lob,
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

Uses the `oracledb` driver in thin mode — no Oracle Instant Client required.

**Queries:** Standard SQL.

```sql
SELECT * FROM employees WHERE department_id = 10 AND hire_date > DATE '2024-01-01'
SELECT e.id, d.name FROM employees e JOIN departments d ON d.id = e.department_id
INSERT INTO employees (department_id, name) VALUES (10, 'Alice')
ALTER SESSION SET NLS_LENGTH_SEMANTICS = CHAR
```

**Resources:**

```
(root)  ← non-system schemas (ALL_USERS where ORACLE_MAINTAINED = 'N')
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        └── indexes      → name, index type
```

Describing a table or view returns column metadata (name, type, nullability,
primary key flag, default). Describing an index returns its key fields,
direction, and uniqueness.

**Session properties:** run `ALTER SESSION SET <property> = <value>` like any
other statement. It's remembered per-connection and automatically re-applied
if the connection is silently reconnected (e.g. after an idle timeout).
"""

    def __init__(
        self,
        params: dict[str, Any],
        conn: AsyncConnection,
        has_oracle_maintained: bool,
        settings: DriverSettings,
    ) -> None:
        super().__init__(params, settings)
        self._conn = conn
        self._has_oracle_maintained = has_oracle_maintained
        """True when connected to Oracle 12c+; enables ORACLE_MAINTAINED column filter."""
        self._metadata_transform_set = False
        """Set to True after DBMS_METADATA session transform params are applied.
        Cleared on reconnect so they are re-applied to the new session."""
        self._session_statements: dict[str, str] = {}
        """ALTER SESSION SET statements successfully run on this connection, keyed
        by upper-cased property name (a later SET for the same property replaces
        the earlier entry). Replayed in reconnect() since a fresh Oracle session
        starts without them."""

    @classmethod
    async def create(
        cls, params: dict[str, Any], settings: DriverSettings
    ) -> "OracleDriver":
        conn, has_oracle_maintained = await cls._open(params)
        return cls(params, conn, has_oracle_maintained, settings)

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
        await self._replay_session_statements()

    async def _replay_session_statements(self) -> None:
        for stmt in self._session_statements.values():
            try:
                cur = self._conn.cursor()
                await cur.execute(stmt)
            except Exception as exc:
                _maybe_raise_connection_lost(exc)
                if isinstance(exc, oracledb.DatabaseError):
                    raise DriverError(_format_db_error(exc, stmt)) from exc
                raise

    async def disconnect(self) -> None:
        try:
            await self._conn.close()
        except oracledb.InterfaceError:
            pass

    async def execute(
        self, query: str, binds: list[Any] | None = None
    ) -> ReadResult | WriteResult:
        """Run a SQL statement. Positional bind values map to ``:1``, ``:2``, … in the query.

        Args:
            query: SQL statement to execute.
            binds: Optional positional bind parameters (referenced as ``:1``, ``:2``, … in the query).

        Returns:
            ReadResult for queries that return rows, WriteResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        binds = binds or []

        try:
            cur = self._conn.cursor()
            await cur.execute(query, binds)
            prop = _alter_session_property(query)
            if prop is not None:
                self._session_statements[prop] = query
            if _is_explain_plan(query):
                lines = await fetch_explain_plan(cur)
                rows = [[row] for row in lines]
                return ReadResult(
                    columns=["PLAN_TABLE_OUTPUT"], rows=rows, rows_total=len(rows)
                )
            if cur.description is not None:
                columns = [d[0] for d in cur.description]
                rows = [
                    [await render_lob(v) for v in row] for row in await cur.fetchall()
                ]
                return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
            invalidate_cache(self._conn)
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

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(build_preview_query(schema, table))
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
                return await self._describe_entity(schema.upper(), table.upper())

            case [schema, table, "indexes"]:
                return await self._describe_indices(schema.upper(), table.upper())

            case [schema, table, "indexes", index_name]:
                return await self._describe_index(
                    schema.upper(), table.upper(), index_name.upper()
                )

            case [schema, table, "columns", col_name]:
                return await self._describe_field(
                    schema.upper(), table.upper(), col_name.upper()
                )

            case [schema, table, "relationships", column]:
                return await self._describe_relationship(
                    schema.upper(), table.upper(), column.upper()
                )

            case _:
                return None

    async def _describe_entity(self, schema: str, table: str) -> EntityDescription:
        col_details = await fetch_column_details(self._conn, schema, table)
        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        indices = await self._describe_indices(schema, table, fetch_ddl=False)
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        excl, comp = build_column_index_lists(fields_by_index, indices)
        comments = await fetch_all_column_comments(self._conn, schema, table)
        samples = await self._fetch_samples(schema, table)
        comment = await fetch_table_comment(self._conn, schema, table)
        outgoing_by_col = group_references_by_column(
            await fetch_outgoing_references(self._conn, schema, table)
        )
        incoming_by_col = group_references_by_ref_column(
            await fetch_incoming_references(self._conn, schema, table)
        )

        return EntityDescription(
            name=table,
            kind="table",
            schema=schema,
            comment=comment,
            properties=[
                FieldDescription(
                    name=col.name,
                    types=[col.type],
                    nullable=col.nullable,
                    pk=col.name in pk_cols,
                    default=col.default,
                    exclusive_indices=excl.get(col.name, []),
                    composite_indices=comp.get(col.name, []),
                    comment=comments.get(col.name),
                    sample=samples.get(col.name, []),
                    outgoing_references=outgoing_by_col.get(col.name, []),
                    incoming_references=incoming_by_col.get(col.name, []),
                )
                for col in col_details
            ],
        )

    async def _describe_indices(
        self, schema: str, table: str, *, fetch_ddl: bool = True
    ) -> list[IndexDescription]:
        metas = await fetch_index_metas_for_table(self._conn, schema, table)
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        join_tables = await fetch_join_tables_for_table(self._conn, schema, table)

        indices = []
        for meta in metas:
            ddl = (
                await self._get_index_ddl(meta.owner, meta.name, meta.index_type, table)
                if fetch_ddl and not meta.generated
                else None
            )
            indices.append(
                IndexDescription(
                    name=meta.name,
                    fields=fields_by_index.get(meta.name, []),
                    unique=meta.unique,
                    tables=join_tables.get(meta.name, [table]),
                    index_type=meta.index_type,
                    visible=meta.visible,
                    ddl=ddl,
                )
            )

        return indices

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
        ddl = (
            None
            if meta.generated
            else await self._get_index_ddl(schema, index_name, meta.index_type, table)
        )

        return IndexDescription(
            name=index_name,
            fields=fields,
            unique=meta.unique,
            tables=tables,
            index_type=meta.index_type,
            visible=meta.visible,
            ddl=ddl,
        )

    async def _describe_field(
        self, schema: str, table: str, col_name: str
    ) -> FieldDescription | None:
        col_details = await fetch_column_details(self._conn, schema, table)
        col = next((c for c in col_details if c.name == col_name), None)
        if col is None:
            return None

        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        indices = await self._describe_indices(schema, table, fetch_ddl=False)
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        excl, comp = build_column_index_lists(fields_by_index, indices)
        comments = await fetch_all_column_comments(self._conn, schema, table)
        sample = await self._fetch_sample(schema, table, col_name)
        outgoing_by_col = group_references_by_column(
            await fetch_outgoing_references(self._conn, schema, table)
        )
        incoming_by_col = group_references_by_ref_column(
            await fetch_incoming_references(self._conn, schema, table)
        )

        return FieldDescription(
            name=col.name,
            types=[col.type],
            nullable=col.nullable,
            pk=col.name in pk_cols,
            default=col.default,
            exclusive_indices=excl.get(col_name, []),
            composite_indices=comp.get(col_name, []),
            comment=comments.get(col_name),
            sample=sample,
            outgoing_references=outgoing_by_col.get(col_name, []),
            incoming_references=incoming_by_col.get(col_name, []),
        )

    async def _describe_relationship(
        self, schema: str, table: str, column: str
    ) -> TableReference | None:
        refs = await fetch_outgoing_references(self._conn, schema, table)
        return find_reference(refs, column)

    async def _fetch_sample(self, schema: str, table: str, col_name: str) -> list[Any]:
        try:
            return await asyncio.wait_for(
                fetch_column_sample(
                    self._conn,
                    schema,
                    table,
                    col_name,
                    self._settings.column_sample_size,
                ),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return []

    async def _fetch_samples(self, schema: str, table: str) -> dict[str, list[Any]]:
        try:
            columns, rows = await asyncio.wait_for(
                fetch_table_sample_rows(self._conn, schema, table),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return {}
        return build_column_samples(columns, rows, self._settings.column_sample_size)

    async def _ensure_metadata_transform(self) -> None:
        if self._metadata_transform_set:
            return
        await apply_metadata_transform(self._conn)
        self._metadata_transform_set = True

    async def _get_index_ddl(
        self, schema: str, index_name: str, index_type: str | None, table: str
    ) -> str:
        try:
            await self._ensure_metadata_transform()
            return await fetch_index_ddl(self._conn, schema, index_name)
        except Exception as exc:
            logger.debug(
                "DBMS_METADATA.GET_DDL failed for index %s.%s on table %s.%s (index_type=%r): %s",
                schema,
                index_name,
                schema,
                table,
                index_type,
                exc,
            )
            return "-- DDL unavailable"


def _is_explain_plan(query: str) -> bool:
    for line in query.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.upper().startswith("EXPLAIN PLAN")
    return False


_ALTER_SESSION_RE = re.compile(
    r"(?is)^\s*ALTER\s+SESSION\s+SET\s+([A-Za-z_][A-Za-z0-9_]*)\s*="
)


def _alter_session_property(query: str) -> str | None:
    """Return the upper-cased property name if *query* is an ``ALTER SESSION
    SET <property> = ...`` statement, else None."""
    match = _ALTER_SESSION_RE.match(query)
    return match.group(1).upper() if match else None


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
