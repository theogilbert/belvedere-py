import re
from collections.abc import Awaitable, Callable, Mapping

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


def _describe_from(table_by_path: Mapping[tuple[str, ...], DescribeResult]) -> Describe:
    async def describe(path: list[str]) -> DescribeResult:
        return table_by_path.get(tuple(path))

    return describe


def _chain_tables(n: int) -> dict[tuple[str, ...], TableDescription]:
    """Tables t0..t{n-1} where each t{i} has a foreign key to t{i-1}."""
    tables: dict[tuple[str, ...], TableDescription] = {}
    for i in range(n):
        columns = [ColumnInfo(name="id", type="INTEGER", pk=True)]
        outgoing = []
        if i > 0:
            columns.append(ColumnInfo(name="prev_id", type="INTEGER"))
            outgoing.append(
                TableReference(column="prev_id", table=f"t{i - 1}", ref_column="id")
            )
        tables[(f"t{i}",)] = TableDescription(
            table=f"t{i}", columns=columns, outgoing_references=outgoing
        )
    for path, desc in tables.items():
        for ref in desc.outgoing_references:
            tables[(ref.table,)].incoming_references.append(
                TableReference(
                    column=ref.ref_column, table=path[0], ref_column=ref.column
                )
            )
    return tables


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

    async def test_many_to_one_edge_shows_star_and_one_markers(self) -> None:
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
        # "*" (many) at the FK side, "1" at the referenced side.
        assert re.search(r"│\*─*1│", result.diagram)

    async def test_one_to_one_edge_shows_one_markers_on_both_ends(self) -> None:
        orders = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(
                    column="user_id", table="users", ref_column="id", unique=True
                )
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): orders, ("users",): users})
        result = await build_diagram(["orders"], describe)
        # FK column is itself unique, so both ends show "1" instead of "*".
        assert re.search(r"│1─*1│", result.diagram)

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

    async def test_unresolvable_reference_gets_a_placeholder_box(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "┌─ users" in result.diagram
        assert "(unavailable)" in result.diagram

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

    async def test_diamond_reference_draws_exactly_one_box(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        shared = TableDescription(
            table="shared", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from(
            {("root",): root, ("a",): a, ("b",): b, ("shared",): shared}
        )
        result = await build_diagram(["root"], describe)
        assert result.diagram.count("┌─ shared") == 1

    async def test_edge_between_boxes_of_different_heights_is_straight(self) -> None:
        tall = TableDescription(
            table="tall",
            columns=[
                ColumnInfo(name=f"k{i}", type="INTEGER", pk=True) for i in range(4)
            ]
            + [ColumnInfo(name="small_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="small_id", table="small", ref_column="id")
            ],
        )
        small = TableDescription(
            table="small", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("tall",): tall, ("small",): small})
        result = await build_diagram(["tall"], describe)
        # small is nudged down so both anchors share a row — one straight
        # border-to-border line, no jog in the channel. Endpoints carry
        # cardinality markers: "*" (many) at the FK side, "1" at the
        # referenced side.
        assert re.search(r"│\*─*1│", result.diagram)

    async def test_skip_edge_intermediate_box_is_nudged_off_the_corridor(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="mid_id", type="INTEGER"),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="mid_id", table="mid", ref_column="id"),
                TableReference(column="leaf_id", table="leaf", ref_column="id"),
            ],
        )
        mid = TableDescription(
            table="mid",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="leaf_id", table="leaf", ref_column="id")
            ],
        )
        leaf = TableDescription(
            table="leaf", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("root",): root, ("mid",): mid, ("leaf",): leaf})
        result = await build_diagram(["root"], describe)
        # The root→leaf edge crosses mid's rank; mid must move up out of the
        # corridor instead of the edge detouring around it, which puts mid's
        # header above root's.
        lines = result.diagram.splitlines()
        row_of = {
            t: next(r for r, s in enumerate(lines) if f"┌─ {t} " in s)
            for t in ("root", "mid")
        }
        assert row_of["mid"] < row_of["root"]

    async def test_long_chain_wraps_into_a_new_band(self) -> None:
        describe = _describe_from(_chain_tables(8))
        result = await build_diagram(["t0"], describe)

        lines = result.diagram.splitlines()
        col_of = {}
        for i in range(8):
            marker = f"┌─ t{i} "
            row = next(r for r, line in enumerate(lines) if marker in line)
            col_of[i] = lines[row].index(marker)

        # t0..t4 form the first band (5 columns), each strictly to the right
        # of the previous; t5 wraps into a snaking second band, landing
        # directly below t4 instead of jumping back to the far left.
        assert col_of[4] > col_of[0]
        assert col_of[5] == col_of[4]

    async def test_adjacent_band_edge_connects_via_the_wrap_lane(self) -> None:
        tables = _chain_tables(6)
        tables[("t5",)].outgoing_references.append(
            TableReference(column="t3_id", table="t3", ref_column="id")
        )
        tables[("t3",)].incoming_references.append(
            TableReference(column="id", table="t5", ref_column="t3_id")
        )
        describe = _describe_from(tables)
        result = await build_diagram(["t0"], describe)

        # t5 wraps into the band below t3's; their edge leaves t3 through its
        # bottom border instead of crowding the channel on t3's right.
        lines = result.diagram.splitlines()
        top = next(r for r, s in enumerate(lines) if "┌─ t3 " in s)
        left = lines[top].index("┌─ t3 ")
        right = lines[top].index("┐", left)
        bottom = next(r for r in range(top + 1, len(lines)) if lines[r][left] == "└")
        center = left + (right - left + 1) // 2
        # t3 is the referenced ("1") side of the relationship, so the wrap
        # connector's first cell below t3's border is the "1" marker rather
        # than a plain vertical line.
        assert lines[bottom + 1].ljust(center + 1)[center] == "1"

    async def test_wrap_connected_box_sinks_below_its_rank_siblings(self) -> None:
        tables = _chain_tables(6)
        tables[("t5",)].outgoing_references.append(
            TableReference(column="t3_id", table="t3", ref_column="id")
        )
        tables[("t3",)].incoming_references.append(
            TableReference(column="id", table="t5", ref_column="t3_id")
        )
        tables[("u3",)] = TableDescription(
            table="u3",
            columns=[ColumnInfo(name="prev_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="prev_id", table="t2", ref_column="id")
            ],
        )
        tables[("t2",)].incoming_references.append(
            TableReference(column="id", table="u3", ref_column="prev_id")
        )
        describe = _describe_from(tables)
        result = await build_diagram(["t0"], describe)

        # t3's edge to t5 (band below) drops from t3's bottom border, so its
        # rank-mate u3 must not sit between t3 and the band's lower edge.
        lines = result.diagram.splitlines()
        row_of = {
            t: next(r for r, s in enumerate(lines) if f"┌─ {t} " in s)
            for t in ("t3", "u3")
        }
        assert row_of["t3"] > row_of["u3"]


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

    async def test_table_referenced_twice_still_gets_exactly_one_region(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        shared = TableDescription(
            table="shared", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from(
            {("root",): root, ("a",): a, ("b",): b, ("shared",): shared}
        )
        result = await build_diagram(["root"], describe)

        matches = [r for r in result.regions if r.path == ["shared"]]
        assert len(matches) == 1

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
