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

import dataclasses
import os
import uuid
from collections.abc import AsyncGenerator

import pytest

from grannos.drivers.base import DriverSettings
from grannos.drivers.oracle import OracleDriver
from grannos.protocol import (
    EntityDescription,
    FieldDescription,
    IndexDescription,
    ReadResult,
    TableReference,
    WriteResult,
)

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
        d = await OracleDriver.create(_params(), DriverSettings())
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
async def driver2() -> AsyncGenerator[OracleDriver, None]:
    pytest.importorskip("oracledb")
    password2 = os.environ.get("ORACLE_PASSWORD2", "")
    if not password2:
        pytest.skip("ORACLE_PASSWORD2 not set; skipping cross-schema index tests")
    params = {
        **_params(),
        "user": os.environ.get("ORACLE_USER2", "testuser2"),
        "password": password2,
    }
    try:
        d = await OracleDriver.create(params, DriverSettings())
    except Exception as exc:
        pytest.skip(f"Second Oracle user not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture
async def schema2(driver2: OracleDriver) -> str:
    result = await driver2.execute("SELECT USER FROM DUAL", [])
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


class TestReconnect:
    async def test_reconnect_succeeds_when_connection_is_dead(
        self, driver: OracleDriver
    ) -> None:
        await driver._conn.close()
        await driver.reconnect()
        result = await driver.execute("SELECT 1 FROM DUAL", [])
        assert isinstance(result, ReadResult)


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
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_should_return_dml_result_for_delete(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        await driver.execute(f"INSERT INTO {schema}.{table} VALUES (:1)", [1])
        await driver.execute(f"INSERT INTO {schema}.{table} VALUES (:1)", [2])
        result = await driver.execute(f"DELETE FROM {schema}.{table}", [])
        assert isinstance(result, WriteResult)
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
        assert {i.name for i in items} == {"columns", "indexes"}
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

    async def test_unknown_path_returns_empty(self, driver: OracleDriver) -> None:
        assert (
            await driver.explore_list(["NO_SUCH_SCHEMA", "NO_SUCH_TABLE", "extra"])
            == []
        )


class TestExploreDescribe:
    async def test_returns_field_metadata(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER NOT NULL, val VARCHAR2(50))", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        assert desc.schema == schema
        assert desc.name == table
        assert desc.kind == "table"
        by_name = {f.name: f for f in desc.properties}
        assert list(by_name) == ["ID", "VAL"]
        assert by_name["ID"].types == ["NUMBER"]
        assert by_name["ID"].nullable is False
        assert by_name["VAL"].types == ["VARCHAR2"]
        assert by_name["VAL"].nullable is True

    async def test_returns_pk_flag(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER PRIMARY KEY, val VARCHAR2(50))",
            [],
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].pk is True
        assert by_name["VAL"].pk is False

    async def test_returns_default_value(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER DEFAULT 0, val VARCHAR2(50))", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].default == "0"
        assert by_name["VAL"].default is None

    async def test_returns_none_for_unknown_path(self, driver: OracleDriver) -> None:
        assert await driver.explore_describe([]) is None

    async def test_columns_group_path_no_longer_resolves(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        assert await driver.explore_describe([schema, table, "columns"]) is None

    async def test_comment(self, driver: OracleDriver, schema: str, table: str) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        await driver.execute(
            f"COMMENT ON TABLE {schema}.{table} IS 'A test table comment'", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        assert desc.comment == "A test table comment"

    async def test_comment_defaults_to_none(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        assert desc.comment is None
        assert await driver.explore_describe(["SYS"]) is None

    async def test_data_type(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].types == ["NUMBER"]
        assert by_name["VAL"].types == ["VARCHAR2"]

    async def test_exclusive_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val)", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert len(by_name["VAL"].exclusive_indices) == 1
        assert by_name["VAL"].exclusive_indices[0].name == idx
        assert by_name["ID"].exclusive_indices == []
        assert by_name["VAL"].composite_indices == []

    async def test_composite_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table}"
            f" (id NUMBER, val VARCHAR2(50), other VARCHAR2(50))",
            [],
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val, other)", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert len(by_name["VAL"].composite_indices) == 1
        assert by_name["VAL"].exclusive_indices == []

    async def test_field_comment(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        await driver.execute(
            f"COMMENT ON COLUMN {schema}.{table}.val IS 'A test comment'", []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["VAL"].comment == "A test comment"
        assert by_name["ID"].comment is None

    async def test_sample_values(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(10))", []
        )
        for i, v in enumerate(["x", "y", "z", "x"]):
            await driver.execute(
                f"INSERT INTO {schema}.{table} VALUES (:1, :2)", [i, v]
            )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        sample = by_name["VAL"].sample
        assert len(sample) <= 3
        assert set(sample).issubset({"x", "y", "z"})

    async def test_returns_outgoing_references(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, child])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        ref = by_name["PARENT_ID"].outgoing_references[0]
        # The constraint is unnamed, so Oracle auto-generates an unpredictable
        # SYS_C* name — assert it's present without pinning its exact value.
        assert ref.constraint_name is not None
        assert dataclasses.replace(ref, constraint_name=None) == TableReference(
            table=child,
            schema=schema,
            column="PARENT_ID",
            ref_table=parent,
            ref_schema=schema,
            ref_column="ID",
        )
        assert by_name["ID"].outgoing_references == []

    async def test_returns_incoming_references(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, parent])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        ref = by_name["ID"].incoming_references[0]
        assert ref.constraint_name is not None
        assert dataclasses.replace(ref, constraint_name=None) == TableReference(
            table=child,
            schema=schema,
            column="PARENT_ID",
            ref_table=parent,
            ref_schema=schema,
            ref_column="ID",
        )

    async def test_references_default_to_empty(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].outgoing_references == []
        assert by_name["ID"].incoming_references == []

    async def test_outgoing_reference_is_unique_when_fk_column_has_unique_constraint(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  parent_id NUMBER UNIQUE,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, child])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["PARENT_ID"].outgoing_references[0].unique is True

    async def test_outgoing_reference_is_not_unique_by_default(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, child])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["PARENT_ID"].outgoing_references[0].unique is False

    async def test_incoming_reference_is_unique_when_fk_column_has_unique_constraint(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  parent_id NUMBER UNIQUE,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, parent])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].incoming_references[0].unique is True

    async def test_incoming_reference_is_not_unique_by_default(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, parent])
        assert isinstance(desc, EntityDescription)
        by_name = {f.name: f for f in desc.properties}
        assert by_name["ID"].incoming_references[0].unique is False

    async def test_should_describe_a_relationship(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe(
            [schema, child, "relationships", "parent_id"]
        )
        assert isinstance(desc, TableReference)
        assert desc.table == child
        assert desc.schema == schema
        assert desc.column == "PARENT_ID"
        assert desc.ref_table == parent
        assert desc.ref_schema == schema
        assert desc.ref_column == "ID"
        assert desc.constraint_name is not None

    async def test_should_return_none_for_unknown_relationship_column(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        result = await driver.explore_describe([schema, table, "relationships", "id"])
        assert result is None


class TestExploreDescribeIndex:
    async def test_basic_fields_and_type(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.name == idx
        assert len(desc.fields) == 1
        assert desc.fields[0].name == "VAL"
        assert desc.fields[0].direction == "asc"

    async def test_descending_direction(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val NUMBER)", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val DESC)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.fields[0].direction == "desc"

    async def test_unique_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, email VARCHAR2(100))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE UNIQUE INDEX {schema}.{idx} ON {schema}.{table}(email)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.unique is True

    async def test_multi_column_index_field_order(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table}"
            f" (id NUMBER, last VARCHAR2(50), first VARCHAR2(50))",
            [],
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(last, first)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert [f.name for f in desc.fields] == ["LAST", "FIRST"]

    async def test_ddl_populated_for_user_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.ddl is not None
        assert idx in desc.ddl

    async def test_unknown_index_returns_none(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        assert (
            await driver.explore_describe([schema, table, "indexes", "NO_SUCH_IDX"])
            is None
        )


class TestExploreDescribeField:
    async def test_basic_fields(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50) NOT NULL)", []
        )
        desc = await driver.explore_describe([schema, table, "columns", "VAL"])
        assert isinstance(desc, FieldDescription)
        assert desc.name == "VAL"
        assert desc.types == ["VARCHAR2"]
        assert desc.nullable is False
        assert desc.pk is False

    async def test_pk_column(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER PRIMARY KEY, val VARCHAR2(50))",
            [],
        )
        desc = await driver.explore_describe([schema, table, "columns", "ID"])
        assert isinstance(desc, FieldDescription)
        assert desc.pk is True

    async def test_exclusive_index(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver.execute(
            f"CREATE INDEX {schema}.{idx} ON {schema}.{table}(val)", []
        )
        desc = await driver.explore_describe([schema, table, "columns", "VAL"])
        assert isinstance(desc, FieldDescription)
        assert len(desc.exclusive_indices) == 1
        assert desc.exclusive_indices[0].name == idx
        assert desc.composite_indices == []

    async def test_comment(self, driver: OracleDriver, schema: str, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        await driver.execute(
            f"COMMENT ON COLUMN {schema}.{table}.val IS 'Column comment'", []
        )
        desc = await driver.explore_describe([schema, table, "columns", "VAL"])
        assert isinstance(desc, FieldDescription)
        assert desc.comment == "Column comment"

    async def test_sample_values(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(10))", []
        )
        for i, v in enumerate(["a", "b", "c", "a"]):
            await driver.execute(
                f"INSERT INTO {schema}.{table} VALUES (:1, :2)", [i, v]
            )
        desc = await driver.explore_describe([schema, table, "columns", "VAL"])
        assert isinstance(desc, FieldDescription)
        assert len(desc.sample) <= 3
        assert set(desc.sample).issubset({"a", "b", "c"})

    async def test_outgoing_references(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, child, "columns", "PARENT_ID"])
        assert isinstance(desc, FieldDescription)
        assert len(desc.outgoing_references) == 1
        ref = desc.outgoing_references[0]
        assert dataclasses.replace(ref, constraint_name=None) == TableReference(
            table=child,
            schema=schema,
            column="PARENT_ID",
            ref_table=parent,
            ref_schema=schema,
            ref_column="ID",
        )

    async def test_incoming_references(
        self, driver: OracleDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE {schema}.{parent} (id NUMBER PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE {schema}.{child} ("
            f"  id NUMBER, parent_id NUMBER,"
            f"  FOREIGN KEY (parent_id) REFERENCES {schema}.{parent}(id)"
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, parent, "columns", "ID"])
        assert isinstance(desc, FieldDescription)
        assert len(desc.incoming_references) == 1
        ref = desc.incoming_references[0]
        assert dataclasses.replace(ref, constraint_name=None) == TableReference(
            table=child,
            schema=schema,
            column="PARENT_ID",
            ref_table=parent,
            ref_schema=schema,
            ref_column="ID",
        )

    async def test_empty_outgoing_references_when_not_fk(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        desc = await driver.explore_describe([schema, table, "columns", "ID"])
        assert isinstance(desc, FieldDescription)
        assert desc.outgoing_references == []

    async def test_unknown_column_returns_none(
        self, driver: OracleDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE {schema}.{table} (id NUMBER)", [])
        assert (
            await driver.explore_describe([schema, table, "columns", "NO_SUCH_COL"])
            is None
        )


class TestCrossSchemaIndex:
    async def test_indices_ddl_for_index_owned_by_different_schema(
        self,
        driver: OracleDriver,
        driver2: OracleDriver,
        schema: str,
        schema2: str,
        table: str,
    ) -> None:
        await driver.execute(
            f"CREATE TABLE {schema}.{table} (id NUMBER, val VARCHAR2(50))", []
        )
        idx = "IDX_" + table
        await driver2.execute(
            f"CREATE INDEX {schema2}.{idx} ON {schema}.{table}(val)", []
        )
        desc = await driver.explore_describe([schema, table, "indexes"])
        assert isinstance(desc, list)
        indices = [i for i in desc if isinstance(i, IndexDescription)]
        by_name = {i.name: i for i in indices}
        assert idx in by_name
        ddl = by_name[idx].ddl
        assert ddl is not None
        assert idx in ddl
