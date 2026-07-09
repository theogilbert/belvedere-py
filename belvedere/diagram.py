"""Renders a table and all tables connected to it (recursively via foreign keys)
as an ASCII box-and-tree diagram.

Layout is a vertical tree: the root table's box is printed first, then each
related table is nested underneath, indented like the ``tree`` command, and
connected to its parent with an edge label describing the join. Tables already
rendered elsewhere in the tree (cycles, diamond references) are not
re-rendered — they appear as a plain reference line pointing back to them.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .protocol import ColumnInfo, DescribeResult, TableDescription, TableReference

Describe = Callable[[list[str]], Awaitable[DescribeResult]]
"""Fetches the description for a path, as ``Dispatcher._handle_explore_diagram`` does
via ``conn.driver.explore_describe`` (with reconnect-and-retry)."""

_MAX_DEPTH = 20
"""Tree levels rendered as full boxes before falling back to a compact reference line.
Bounds worst-case diagram width, since each level adds 4 columns of indentation."""

_BRANCH = "├── "
_LAST_BRANCH = "└── "
_PIPE = "│   "
_BLANK = "    "


class DiagramError(Exception):
    """Raised when the given path does not resolve to a table."""


@dataclass
class _Edge:
    label: str
    """Join description shown next to the branch, e.g. ``user_id → users.id``."""
    node: "_Node | None"
    """Child node, or None if the target was already rendered elsewhere (or unavailable)."""
    ref_name: str
    """Display name of the target table; used when node is None."""


@dataclass
class _Node:
    name: str
    """Display name, e.g. ``dbo.orders`` or ``orders``."""
    columns: list[ColumnInfo]
    fk_columns: set[str]
    """Names of columns covered by an outgoing foreign key."""
    children: list[_Edge]


async def build_diagram(path: list[str], describe: Describe) -> str:
    """Fetch the table at ``path`` and all connected tables, and render them as ASCII.

    Args:
        path: Path segments identifying a table (e.g. ``["dbo", "orders"]``).
        describe: Async callback resolving a path to its describe result.

    Returns:
        The rendered diagram, with no max width/height applied — the caller
        should render it without line-wrapping.

    Raises:
        DiagramError: If path does not resolve to a table.
    """
    desc = await describe(path)
    if not isinstance(desc, TableDescription):
        raise DiagramError(f"Path {path!r} does not resolve to a table")
    visited = {tuple(path)}
    root = await _visit(path, desc, describe, visited, depth=0)
    return "\n".join(_render(root))


async def _visit(
    path: list[str],
    desc: TableDescription,
    describe: Describe,
    visited: set[tuple[str, ...]],
    depth: int,
) -> _Node:
    node = _Node(
        name=_display_name(desc),
        columns=desc.columns,
        fk_columns={r.column for r in desc.outgoing_references},
        children=[],
    )
    for direction, ref in _iter_refs(desc):
        ref_path = _ref_path(desc, ref)
        label = _edge_label(ref, direction)
        ref_name = f"{ref.schema}.{ref.table}" if ref.schema else ref.table

        if tuple(ref_path) == tuple(path):
            continue  # self-reference — already fully described by this box

        if tuple(ref_path) in visited or depth >= _MAX_DEPTH:
            node.children.append(_Edge(label=label, node=None, ref_name=ref_name))
            continue

        visited.add(tuple(ref_path))
        child_desc = await describe(ref_path)
        if not isinstance(child_desc, TableDescription):
            node.children.append(_Edge(label=label, node=None, ref_name=ref_name))
            continue

        child = await _visit(ref_path, child_desc, describe, visited, depth + 1)
        node.children.append(_Edge(label=label, node=child, ref_name=ref_name))

    return node


def _iter_refs(desc: TableDescription):
    for ref in desc.outgoing_references:
        yield "out", ref
    for ref in desc.incoming_references:
        yield "in", ref


def _edge_label(ref: TableReference, direction: str) -> str:
    target = f"{ref.schema}.{ref.table}" if ref.schema else ref.table
    if direction == "out":
        return f"{ref.column} → {target}.{ref.ref_column}"
    return f"{target}.{ref.ref_column} → {ref.column}"


def _ref_path(desc: TableDescription, ref: TableReference) -> list[str]:
    if desc.schema is None:
        return [ref.table]
    return [ref.schema or desc.schema, ref.table]


def _display_name(desc: TableDescription) -> str:
    return f"{desc.schema}.{desc.table}" if desc.schema else desc.table


def _render(node: _Node) -> list[str]:
    return _render_box(node) + _render_children(node.children, prefix="")


def _render_children(edges: list[_Edge], prefix: str) -> list[str]:
    lines: list[str] = []
    for i, edge in enumerate(edges):
        is_last = i == len(edges) - 1
        branch = _LAST_BRANCH if is_last else _BRANCH
        cont = _BLANK if is_last else _PIPE

        if edge.node is None:
            lines.append(f"{prefix}{branch}{edge.ref_name}  ({edge.label})")
            continue

        lines.append(f"{prefix}{branch}{edge.label}")
        box_lines = _render_box(edge.node)
        lines += [f"{prefix}{cont}{bl}" for bl in box_lines]
        lines += _render_children(edge.node.children, prefix + cont)
    return lines


def _render_box(node: _Node) -> list[str]:
    rows = []
    for col in node.columns:
        markers = []
        if col.pk:
            markers.append("PK")
        if col.name in node.fk_columns:
            markers.append("FK")
        rows.append((col.name, col.type, ",".join(markers)))

    name_w = max((len(r[0]) for r in rows), default=0)
    type_w = max((len(r[1]) for r in rows), default=0)
    content_lines = [
        f"{n:<{name_w}}  {t:<{type_w}}  {m}".rstrip() for n, t, m in rows
    ] or ["(no columns)"]

    inner_w = max(len(node.name) + 2, max(len(line) for line in content_lines))
    top = "┌─ " + node.name + " " + "─" * max(0, inner_w - len(node.name) - 1) + "┐"
    bottom = "└" + "─" * (inner_w + 2) + "┘"
    body = [f"│ {line:<{inner_w}} │" for line in content_lines]
    return [top, *body, bottom]
