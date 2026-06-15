"""
Integration tests for the Oracle driver.

Requires a running Oracle instance. Configure via environment variables:
  ORACLE_HOST      (default: localhost)
  ORACLE_PORT      (default: 1521)
  ORACLE_USER      (default: testuser)
  ORACLE_PASSWORD  (required — no default)
  ORACLE_SERVICE   (default: FREEPDB1)

Tests are skipped automatically when oracledb is not installed or the
server is unreachable.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.oracle import OracleDriver
from belvedere.protocol import DMLResult, ReadResult, TableDescription

pytestmark = pytest.mark.external


def _params() -> dict:
    return {
        "host": os.environ.get("ORACLE_HOST", "localhost"),
        "port": int(os.environ.get("ORACLE_PORT", "1521")),
        "user": os.environ.get("ORACLE_USER", "testuser"),
        "password": os.environ.get("ORACLE_PASSWORD", ""),
        "service_name": os.environ.get("ORACLE_SERVICE", "FREEPDB1"),
    }


@pytest.fixture
async def driver() -> AsyncGenerator[OracleDriver, None]:
    pytest.importorskip("oracledb")
    try:
        d = await OracleDriver.create(_params())
    except Exception as exc:
        pytest.skip(f"Oracle not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture
async def schema(driver: OracleDriver) -> str:
    result = await driver.execute("SELECT USER FROM DUAL", [])
    assert isinstance(result, ReadResult)
    return result.rows[0][0]


@pytest.fixture
async def table(driver: OracleDriver, schema: str) -> AsyncGenerator[str, None]:
    name = "T_" + uuid.uuid4().hex[:12].upper()
    yield name
    try:
        await driver.execute(
            f"DROP TABLE {schema}.{name} CASCADE CONSTRAINTS PURGE", []
        )
    except Exception:
        pass


@pytest.fixture
async def tables(
    driver: OracleDriver, schema: str
) -> AsyncGenerator[tuple[str, str], None]:
    parent = "T_" + uuid.uuid4().hex[:12].upper()
    child = "T_" + uuid.uuid4().hex[:12].upper()
    yield parent, child
    for name in (child, parent):
        try:
            await driver.execute(
                f"DROP TABLE {schema}.{name} CASCADE CONSTRAINTS PURGE", []
            )
        except Exception:
            pass


class TestExecute:
    async def test_should_return_columns_and_rows(self, driver: OracleDriver) -> None:
        result = await driver.execute("SELECT 1 AS n, 'hello' AS s FROM DUAL", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["N", "S"]
        assert result.rows == [[1, "hello"]]

    async def test_should_support_positional_params(self, driver: OracleDriver) -> None:
        result = await driver.execute("SELECT :1 AS val FROM DUAL", [42])
        assert isinstance(result, ReadResult)
        assert result.rows == [[42]]

    async def test_should_return_dml_result_for_insert(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        result = await driver.execute(
            f"INSERT INTO {schema}.{table} VALUES (:1, :2)", [1, "hello"]
        )
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 1

    async def test_should_return_dml_result_for_delete(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        await driver.execute(f"INSERT INTO {schema}.{table} VALUES (:1)", [1])
        await driver.execute(f"INSERT INTO {schema}.{table} VALUES (:1)", [2])
        result = await driver.execute(f"DELETE FROM {schema}.{table}", [])
        assert isinstance(result, DMLResult)
        assert result.rows_affected == 2

    async def test_should_persist_inserts_within_connection(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        await driver.execute(
            f"INSERT INTO {schema}.{table} VALUES (:1, :2)", [1, "hello"]
        )
        result = await driver.execute(f"SELECT id, val FROM {schema}.{table}", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["ID", "VAL"]
        assert result.rows == [[1, "hello"]]


class TestExploreList:
    async def test_root_lists_non_system_schemas(
        self, driver: OracleDriver, schema: str
    ) -> None:
        items = await driver.explore_list([])
        names = {i.name for i in items}
        assert schema in names
        assert "SYS" not in names
        assert "SYSTEM" not in names
        assert all(i.type == "schema" for i in items)
        assert all(i.expandable for i in items)

    async def test_schema_lists_created_table(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        items = await driver.explore_list([schema])
        assert any(i.name == table and i.type == "table" for i in items)

    async def test_schema_lists_view(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        view = table + "_V"
        try:
            await driver.execute(
                f"CREATE VIEW {schema}.{view} AS SELECT * FROM {schema}.{table}", []
            )
            items = await driver.explore_list([schema])
            assert any(i.name == view and i.type == "view" for i in items)
        finally:
            try:
                await driver.execute(f"DROP VIEW {schema}.{view}", [])
            except Exception:
                pass

    async def test_table_returns_groups(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        items = await driver.explore_list([schema, table])
        assert {i.name for i in items} == {"columns", "indexes", "constraints"}
        assert all(i.type == "group" and i.expandable for i in items)

    async def test_columns_lists_in_ordinal_order(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50), active NUMBER(1))",
            [],
        )
        items = await driver.explore_list([schema, table, "columns"])
        assert [i.name for i in items] == ["ID", "VAL", "ACTIVE"]
        assert all(not i.expandable for i in items)

    async def test_columns_reflect_data_type(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, name VARCHAR2(100))", []
        )
        items = await driver.explore_list([schema, table, "columns"])
        by_name = {i.name: i for i in items}
        assert by_name["ID"].type == "NUMBER"
        assert by_name["NAME"].type == "VARCHAR2"

    async def test_indexes_lists_created_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val)", []
        )
        items = await driver.explore_list([schema, table, "indexes"])
        assert any(i.name == idx for i in items)
        assert all(not i.expandable for i in items)

    async def test_indexes_empty_when_none_exist(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        items = await driver.explore_list([schema, table, "indexes"])
        assert items == []

    async def test_constraints_lists_primary_key(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        pk = "PK_" + table
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, CONSTRAINT {pk} PRIMARY KEY (id))",
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert pk in by_name
        assert by_name[pk].type == "primary_key"
        assert not by_name[pk].expandable

    async def test_constraints_lists_unique(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        uq = "UQ_" + table
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50),"
            f" CONSTRAINT {uq} UNIQUE (val))",
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert uq in by_name
        assert by_name[uq].type == "unique"

    async def test_constraints_lists_check(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        ck = "CHK_" + table
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER,"
            f" CONSTRAINT {ck} CHECK (id > 0))",
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert ck in by_name
        assert by_name[ck].type == "check"

    async def test_constraints_maps_foreign_key_type(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        pk = "PK_" + parent
        fk = "FK_" + child
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER, CONSTRAINT {pk} PRIMARY KEY (id))",
            [],
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  CONSTRAINT {fk} FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        items = await driver.explore_list([schema, child, "constraints"])
        by_name = {i.name: i for i in items}
        assert fk in by_name
        assert by_name[fk].type == "foreign_key"

    async def test_unknown_path_returns_empty(self, driver: OracleDriver) -> None:
        assert (
            await driver.explore_list(["NO_SUCH_SCHEMA", "NO_SUCH_TABLE", "extra"])
            == []
        )


class TestExploreDescribe:
    async def test_returns_column_metadata(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER NOT NULL, val VARCHAR2(50))", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        assert desc.schema == schema
        assert desc.table == table
        by_name = {c.name: c for c in desc.columns}
        assert list(by_name) == ["ID", "VAL"]
        assert by_name["ID"].type == "NUMBER"
        assert by_name["ID"].nullable is False
        assert by_name["VAL"].type == "VARCHAR2"
        assert by_name["VAL"].nullable is True

    async def test_returns_pk_flag(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER PRIMARY KEY, val VARCHAR2(50))",
            [],
        )
        desc = await driver.explore_describe([schema, table])
        assert desc is not None
        by_name = {c.name: c for c in desc.columns}
        assert by_name["ID"].pk is True
        assert by_name["VAL"].pk is False

    async def test_returns_default_value(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER DEFAULT 0, val VARCHAR2(50))", []
        )
        desc = await driver.explore_describe([schema, table])
        assert desc is not None
        by_name = {c.name: c for c in desc.columns}
        assert by_name["ID"].default == "0"
        assert by_name["VAL"].default is None

    async def test_returns_none_for_unknown_path(self, driver: OracleDriver) -> None:
        assert await driver.explore_describe([]) is None
        assert await driver.explore_describe(["SYS"]) is None
