"""Renders a table and all tables connected to it (recursively via foreign keys)
as an ASCII box-and-tree diagram.

Layout is a vertical tree: the source table's box is printed first, then each
related table is nested underneath, indented like the ``tree`` command, and
connected to its parent with a branch line naming it. Tables already
rendered elsewhere in the tree (cycles, diamond references) are not
re-rendered — they appear as a plain reference line pointing back to them.

Every table and column name drawn in the diagram is also tracked as a
:class:`~belvedere.protocol.DiagramRegion`, so a client can map a cursor
position back to an ``explore.describe`` path.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .protocol import (
    ColumnInfo,
    DescribeResult,
    DiagramRegion,
    TableDescription,
    TableReference,
)

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
class DiagramResult:
    diagram: str
    """The rendered diagram, as a multi-line string."""
    regions: list[DiagramRegion]
    """Byte-offset spans naming a table or column at each point in ``diagram``."""


@dataclass
class _Segment:
    text: str
    path: list[str] | None = None
    """Path this span resolves to via explore.describe; None for unlabeled text."""


_Line = list[_Segment]


@dataclass
class _Edge:
    node: "_Node | None"
    """Child node, or None if the target was already rendered elsewhere (or unavailable)."""
    ref_name: str
    """Display name of the target table; used when node is None."""
    ref_path: list[str]
    """Path identifying the target table; used when node is None."""


@dataclass
class _Node:
    name: str
    """Display name, e.g. ``dbo.orders`` or ``orders``."""
    path: list[str]
    """Path identifying this table."""
    columns: list[ColumnInfo]
    fk_columns: set[str]
    """Names of columns covered by an outgoing foreign key."""
    ref_columns: set[str]
    """Names of columns covered by an incoming foreign key (referenced by another table)."""
    children: list[_Edge] = field(default_factory=list)


async def build_diagram(path: list[str], describe: Describe) -> DiagramResult:
    """Fetch the table at ``path`` and all connected tables, and render them as ASCII.

    Args:
        path: Path segments identifying a table (e.g. ``["dbo", "orders"]``).
        describe: Async callback resolving a path to its describe result.

    Returns:
        The rendered diagram text and the regions naming each table/column
        drawn within it. No max width/height is applied to the diagram text —
        the caller should render it without line-wrapping.

    Raises:
        DiagramError: If path does not resolve to a table.
    """
    desc = await describe(path)
    if not isinstance(desc, TableDescription):
        raise DiagramError(f"Path {path!r} does not resolve to a table")
    visited = {tuple(path)}
    source = await _visit(path, desc, describe, visited, depth=0)
    return _finalize(_render(source))


async def _visit(
    path: list[str],
    desc: TableDescription,
    describe: Describe,
    visited: set[tuple[str, ...]],
    depth: int,
) -> _Node:
    node = _Node(
        name=_display_name(desc),
        path=path,
        columns=desc.columns,
        fk_columns={r.column for r in desc.outgoing_references},
        ref_columns={r.column for r in desc.incoming_references},
    )
    for ref in _iter_refs(desc):
        ref_path = _ref_path(desc, ref)
        ref_name = f"{ref.schema}.{ref.table}" if ref.schema else ref.table

        if tuple(ref_path) == tuple(path):
            continue  # self-reference — already fully described by this box

        if tuple(ref_path) in visited or depth >= _MAX_DEPTH:
            node.children.append(_Edge(node=None, ref_name=ref_name, ref_path=ref_path))
            continue

        visited.add(tuple(ref_path))
        child_desc = await describe(ref_path)
        if not isinstance(child_desc, TableDescription):
            node.children.append(_Edge(node=None, ref_name=ref_name, ref_path=ref_path))
            continue

        child = await _visit(ref_path, child_desc, describe, visited, depth + 1)
        node.children.append(_Edge(node=child, ref_name=ref_name, ref_path=ref_path))

    return node


def _iter_refs(desc: TableDescription):
    yield from desc.outgoing_references
    yield from desc.incoming_references


def _ref_path(desc: TableDescription, ref: TableReference) -> list[str]:
    if desc.schema is None:
        return [ref.table]
    return [ref.schema or desc.schema, ref.table]


def _display_name(desc: TableDescription) -> str:
    return f"{desc.schema}.{desc.table}" if desc.schema else desc.table


def _render(node: _Node) -> list[_Line]:
    return _render_box(node) + _render_children(node.children, prefix="")


def _render_children(edges: list[_Edge], prefix: str) -> list[_Line]:
    lines: list[_Line] = []
    for i, edge in enumerate(edges):
        is_last = i == len(edges) - 1
        branch = _LAST_BRANCH if is_last else _BRANCH
        cont = _BLANK if is_last else _PIPE

        if edge.node is None:
            lines.append(
                [_Segment(f"{prefix}{branch}"), _Segment(edge.ref_name, edge.ref_path)]
            )
            continue

        box_lines = _render_box(edge.node)
        lines.append([_Segment(f"{prefix}{branch}"), *box_lines[0]])
        lines += [[_Segment(f"{prefix}{cont}"), *bl] for bl in box_lines[1:]]
        lines += _render_children(edge.node.children, prefix + cont)
    return lines


def _render_box(node: _Node) -> list[_Line]:
    display_cols = [
        col
        for col in node.columns
        if col.pk or col.name in node.fk_columns or col.name in node.ref_columns
    ]
    hidden = len(display_cols) < len(node.columns)

    if not node.columns:
        content_lines = ["(no columns)"]
    else:
        rows = []
        for col in display_cols:
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
        ]
        if hidden:
            content_lines.append("...")

    inner_w = max(len(node.name) + 2, max(len(line) for line in content_lines))
    top: _Line = [
        _Segment("┌─ "),
        _Segment(node.name, node.path),
        _Segment(" " + "─" * max(0, inner_w - len(node.name) - 1) + "┐"),
    ]
    bottom: _Line = [_Segment("└" + "─" * (inner_w + 2) + "┘")]

    body: list[_Line] = []
    if not node.columns:
        body.append([_Segment(f"│ {content_lines[0]:<{inner_w}} │")])
    else:
        for col, content in zip(display_cols, content_lines):
            padded = f"{content:<{inner_w}}"
            rest = padded[len(col.name) :]
            col_path = [*node.path, "columns", col.name]
            body.append(
                [_Segment("│ "), _Segment(col.name, col_path), _Segment(rest + " │")]
            )
        if hidden:
            ellipsis = content_lines[-1]
            padded = f"{ellipsis:<{inner_w}}"
            cols_path = [*node.path, "columns"]
            body.append(
                [
                    _Segment("│ "),
                    _Segment(ellipsis, cols_path),
                    _Segment(padded[len(ellipsis) :] + " │"),
                ]
            )

    return [top, *body, bottom]


def _finalize(lines: list[_Line]) -> DiagramResult:
    text_lines: list[str] = []
    regions: list[DiagramRegion] = []
    for row, segments in enumerate(lines):
        parts: list[str] = []
        col = 0
        for seg in segments:
            parts.append(seg.text)
            byte_len = len(seg.text.encode())
            if seg.path is not None:
                regions.append(
                    DiagramRegion(
                        row=row, col_start=col, col_end=col + byte_len, path=seg.path
                    )
                )
            col += byte_len
        text_lines.append("".join(parts))
    return DiagramResult(diagram="\n".join(text_lines), regions=regions)
