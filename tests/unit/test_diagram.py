from collections.abc import Awaitable, Callable

import pytest

from belvedere.diagram import DiagramError, build_diagram
from belvedere.protocol import (
    ColumnInfo,
    DescribeResult,
    IndexDescription,
    TableDescription,
    TableReference,
)

Describe = Callable[[list[str]], Awaitable[DescribeResult]]


def _describe_from(table_by_path: dict[tuple[str, ...], DescribeResult]) -> Describe:
    async def describe(path: list[str]) -> DescribeResult:
        return table_by_path.get(tuple(path))

    return describe


class TestBuildDiagram:
    async def test_raises_when_path_does_not_resolve_to_a_table(self) -> None:
        describe = _describe_from({})
        with pytest.raises(DiagramError):
            await build_diagram(["users"], describe)

    async def test_single_table_renders_a_box_with_its_name(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "users" in result.diagram
        assert "id" in result.diagram

    async def test_schema_qualified_table_shows_schema_dot_table(self) -> None:
        desc = TableDescription(schema="dbo", table="users", columns=[])
        describe = _describe_from({("dbo", "users"): desc})
        result = await build_diagram(["dbo", "users"], describe)
        assert "dbo.users" in result.diagram

    async def test_pk_column_is_marked(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "PK" in result.diagram

    async def test_fk_column_is_marked(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): desc, ("users",): users})
        result = await build_diagram(["orders"], describe)
        assert "FK" in result.diagram

    async def test_outgoing_reference_renders_connected_table(self) -> None:
        orders = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): orders, ("users",): users})
        result = await build_diagram(["orders"], describe)
        assert "users" in result.diagram
        assert "user_id → users.id" in result.diagram

    async def test_incoming_reference_renders_connected_table(self) -> None:
        users = TableDescription(
            table="users",
            columns=[ColumnInfo(name="id", type="INTEGER", pk=True)],
            incoming_references=[
                TableReference(column="id", table="orders", ref_column="user_id")
            ],
        )
        orders = TableDescription(
            table="orders", columns=[ColumnInfo(name="user_id", type="INTEGER")]
        )
        describe = _describe_from({("users",): users, ("orders",): orders})
        result = await build_diagram(["users"], describe)
        assert "orders" in result.diagram
        assert "orders.user_id → id" in result.diagram

    async def test_self_reference_is_not_duplicated(self) -> None:
        desc = TableDescription(
            table="employees",
            columns=[ColumnInfo(name="manager_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="manager_id", table="employees", ref_column="id")
            ],
        )
        describe = _describe_from({("employees",): desc})
        result = await build_diagram(["employees"], describe)
        assert result.diagram.count("┌─ employees") == 1

    async def test_cycle_does_not_recurse_infinitely(self) -> None:
        a = TableDescription(
            table="a",
            columns=[ColumnInfo(name="b_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="b_id", table="b", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[ColumnInfo(name="a_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id")
            ],
        )
        describe = _describe_from({("a",): a, ("b",): b})
        result = await build_diagram(["a"], describe)
        assert result.diagram.count("┌─ a") == 1
        assert result.diagram.count("┌─ b") == 1

    async def test_unresolvable_reference_is_shown_without_a_box(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "users" in result.diagram
        assert "┌─ users" not in result.diagram

    async def test_non_table_describe_result_raises(self) -> None:
        describe = _describe_from({("i",): IndexDescription(index="i", fields=[])})
        with pytest.raises(DiagramError):
            await build_diagram(["i"], describe)

    async def test_non_key_columns_are_collapsed_to_an_ellipsis(self) -> None:
        desc = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="name", type="TEXT"),
                ColumnInfo(name="email", type="TEXT"),
            ],
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "..." in result.diagram
        assert "name" not in result.diagram
        assert "email" not in result.diagram

    async def test_incoming_fk_column_is_kept(self) -> None:
        users = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="username", type="TEXT"),
                ColumnInfo(name="bio", type="TEXT"),
            ],
            incoming_references=[
                TableReference(column="username", table="posts", ref_column="author")
            ],
        )
        posts = TableDescription(
            table="posts", columns=[ColumnInfo(name="author", type="TEXT")]
        )
        describe = _describe_from({("users",): users, ("posts",): posts})
        result = await build_diagram(["users"], describe)
        assert "username" in result.diagram
        assert "bio" not in result.diagram
        assert "..." in result.diagram

    async def test_no_ellipsis_when_all_columns_are_keys(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[
                ColumnInfo(name="user_id", type="INTEGER"),
                ColumnInfo(name="product_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id"),
                TableReference(column="product_id", table="products", ref_column="id"),
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "..." not in result.diagram


class TestBuildDiagramRegions:
    async def test_table_header_region_resolves_to_table_path(self) -> None:
        desc = TableDescription(schema="dbo", table="users", columns=[])
        describe = _describe_from({("dbo", "users"): desc})
        result = await build_diagram(["dbo", "users"], describe)

        region = next(r for r in result.regions if r.path == ["dbo", "users"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "dbo.users"

    async def test_column_region_resolves_to_column_path(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns", "id"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "id"

    async def test_column_region_offset_accounts_for_multibyte_box_chars(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns", "id"])
        line = result.diagram.splitlines()[region.row]
        assert line.startswith("│ id")
        # "│ " is 4 bytes (│ is 3-byte UTF-8) but only 2 characters — the
        # byte offset must reflect that, not the character index.
        assert region.col_start == 4

    async def test_edge_label_region_names_the_referenced_table(self) -> None:
        orders = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): orders, ("users",): users})
        result = await build_diagram(["orders"], describe)

        matches = [r for r in result.regions if r.path == ["users"]]
        assert len(matches) == 2  # once in the users box header, once in the edge label
        for region in matches:
            line = result.diagram.splitlines()[region.row]
            span = line.encode()[region.col_start : region.col_end].decode()
            assert span == "users"

    async def test_ellipsis_region_resolves_to_the_table_column_list(self) -> None:
        desc = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="name", type="TEXT"),
            ],
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "..."

    async def test_unresolved_reference_still_gets_a_region(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)

        region = next(r for r in result.regions if r.path == ["users"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "users"
