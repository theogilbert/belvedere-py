"""Turns abstract layout positions into concrete box rectangles. Sizes every
table's box from its content, positions columns left to right and rows top to
bottom (nudged so directly-connected neighbors share a row, costing the
router zero bends), and overprovisions the gaps between columns and around
the whole diagram's perimeter so ``route.py`` always has room to route
through without ever needing to touch a box. ``route.py`` compacts the
overprovisioned space back out once every path is known.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from ..protocol import FieldDescription
from .canvas import _Line, _Segment
from .graph import GraphEdge, GraphNode
from .layout import Layout

_BOX_GAP = 1
"""Blank rows between boxes stacked within the same column."""
_CHANNEL_PADDING = 1
"""Blank columns on each side of an inter-column channel, outside the lanes
an edge might actually run through."""
_ALIGN_SWEEPS = 2
"""Rounds of vertical alignment; each round pulls every column toward its
left neighbors, then every column toward its right neighbors."""

STUB_LEN = 1
"""Mandatory straight unit-steps out of an anchor before ``route.py`` allows
a bend. Defined here (not in ``route.py``, which imports it) because
``place.py`` must reserve at least this much clearance on every channel and
perimeter margin for the stub to fit — importing the other way would cycle."""


@dataclass
class Rect:
    top: int
    left: int
    height: int
    width: int


@dataclass
class PlaceResult:
    rects: dict[int, Rect]
    """Every node's box, by node id, in overprovisioned canvas coordinates —
    ``route.py`` compacts these down once every edge is routed."""
    box_lines: dict[int, list[_Line]]
    """Every node's pre-rendered box content, by node id, ready for
    ``Canvas.blit_box`` at its ``rects`` position."""
    bounds: tuple[int, int]
    """``(n_rows, n_cols)`` of the full canvas, including the perimeter
    margin ring reserved for edges to detour around the diagram's content."""


def place(
    nodes: list[GraphNode], edges: list[GraphEdge], layout: Layout
) -> PlaceResult:
    box_lines = {n.id: _box_lines(n) for n in nodes}
    box_size = {nid: _box_size(lines) for nid, lines in box_lines.items()}
    height = {nid: h for nid, (h, _) in box_size.items()}

    col_x = _column_x(layout, box_size, edges)
    y_within = _initial_rows(layout, height)
    _align_rows(layout, edges, y_within, height)
    shift = -min(y_within.values(), default=0)
    if shift > 0:
        y_within = {nid: y + shift for nid, y in y_within.items()}

    margin = max(len(edges), STUB_LEN + 1)
    rects = {
        nid: Rect(
            top=y_within[nid] + margin,
            left=col_x[layout.column[nid]] + margin,
            height=h,
            width=w,
        )
        for nid, (h, w) in box_size.items()
    }

    max_row = max((r.top + r.height for r in rects.values()), default=0)
    max_col = max((r.left + r.width for r in rects.values()), default=0)
    bounds = (max_row + margin, max_col + margin)

    return PlaceResult(rects=rects, box_lines=box_lines, bounds=bounds)


_TYPE_MODIFIER_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _base_type(type_: str) -> str:
    """Drop a type's parenthesised modifier — ``VARCHAR2(50 CHAR, 200 BYTE)``
    becomes ``VARCHAR2``.

    The diagram is a structural overview: a length or precision says nothing
    about the relationships it exists to show, and widens every box that
    carries one. ``explore.describe`` still reports the full type.
    """
    return _TYPE_MODIFIER_RE.sub("", type_)


def _box_lines(node: GraphNode) -> list[_Line]:
    if node.unavailable:
        content_lines = ["(unavailable)"]
        display_cols: list[FieldDescription] = []
        hidden = False
    else:
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
                types = "|".join(_base_type(t) for t in col.types)
                rows.append((col.name, types, ",".join(markers)))
            name_w = max((len(r[0]) for r in rows), default=0)
            type_w = max((len(r[1]) for r in rows), default=0)
            content_lines = [
                f"{n:<{name_w}}  {t:<{type_w}}  {m}".rstrip() for n, t, m in rows
            ]
            if hidden:
                content_lines.append("...")

    inner_w = max(len(node.name) + 2, max(len(line) for line in content_lines))
    top_text = (
        "┌─ " + node.name + " " + "─" * max(0, inner_w - len(node.name) - 1) + "┐"
    )
    top: _Line = [_Segment(top_text, node.path, kind="table")]
    bottom: _Line = [_Segment("└" + "─" * (inner_w + 2) + "┘", node.path, kind="table")]

    left_border = _Segment("│", node.path, kind="table")
    right_border = _Segment("│", node.path, kind="table")

    body: list[_Line] = []
    if node.unavailable or not node.columns:
        body.append(
            [
                left_border,
                _Segment(f" {content_lines[0]:<{inner_w}} "),
                right_border,
            ]
        )
    else:
        for col, content in zip(display_cols, content_lines):
            padded = f"{content:<{inner_w}}"
            rest = padded[len(col.name) :]
            col_path = [*node.path, "columns", col.name]
            body.append(
                [
                    left_border,
                    _Segment(" "),
                    _Segment(col.name, col_path, kind="column"),
                    _Segment(rest + " "),
                    right_border,
                ]
            )
        if hidden:
            ellipsis = content_lines[-1]
            padded = f"{ellipsis:<{inner_w}}"
            cols_path = [*node.path, "columns"]
            body.append(
                [
                    left_border,
                    _Segment(" "),
                    _Segment(ellipsis, cols_path, kind="column"),
                    _Segment(padded[len(ellipsis) :] + " "),
                    right_border,
                ]
            )

    return [top, *body, bottom]


def _box_size(lines: list[_Line]) -> tuple[int, int]:
    width = max(sum(len(seg.text) for seg in line) for line in lines)
    return len(lines), width


def _column_x(
    layout: Layout, box_size: dict[int, tuple[int, int]], edges: list[GraphEdge]
) -> dict[int, int]:
    col_width: dict[int, int] = defaultdict(lambda: 1)
    for nid, (_, w) in box_size.items():
        c = layout.column[nid]
        col_width[c] = max(col_width[c], w)

    boundary_count: dict[int, int] = defaultdict(int)
    for edge in edges:
        c0, c1 = layout.column[edge.source], layout.column[edge.target]
        lo, hi = min(c0, c1), max(c0, c1)
        for c in range(lo, hi):
            boundary_count[c] += 1

    columns = sorted(layout.row_order)
    col_x: dict[int, int] = {}
    x = 0
    for i, c in enumerate(columns):
        col_x[c] = x
        x += col_width[c]
        if i < len(columns) - 1:
            # Both boxes flanking this channel may need a full mandatory stub
            # reaching into it from their own side, plus room to bend past
            # each other — a channel narrower than that starves route.py of
            # anywhere to put the stub regardless of how few edges cross it.
            gap = max(2 * _CHANNEL_PADDING + boundary_count[c], 2 * STUB_LEN + 1)
            x += gap
    return col_x


def _initial_rows(layout: Layout, height: dict[int, int]) -> dict[int, int]:
    y_within: dict[int, int] = {}
    for ids in layout.row_order.values():
        y = 0
        for nid in ids:
            y_within[nid] = y
            y += height[nid] + _BOX_GAP
    return y_within


def _align_rows(
    layout: Layout,
    edges: list[GraphEdge],
    y_within: dict[int, int],
    height: dict[int, int],
) -> None:
    """Nudges boxes up or down within their column so a direct edge between
    two adjacent columns lands on the same row on both ends, giving it a
    straight line instead of a jog. Only considers adjacent-column edges —
    a skip edge spanning several columns gains little from precise alignment
    and routes around obstacles anyway. Priority: the better-connected box
    wins a contested nudge."""
    left_partners: dict[int, list[int]] = defaultdict(list)
    right_partners: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        u, v = edge.source, edge.target
        cu, cv = layout.column[u], layout.column[v]
        if abs(cu - cv) != 1:
            continue
        lo, hi = (u, v) if cu < cv else (v, u)
        right_partners[lo].append(hi)
        left_partners[hi].append(lo)

    priority = {
        nid: len(left_partners[nid]) + len(right_partners[nid]) for nid in y_within
    }

    def anchor(nid: int) -> int:
        return y_within[nid] + height[nid] // 2

    def pull(ids: list[int], partners: dict[int, list[int]]) -> None:
        for nid in sorted(ids, key=lambda n: -priority[n]):
            refs = [anchor(p) for p in partners[nid]]
            if not refs:
                continue
            desired = round(sum(refs) / len(refs)) - height[nid] // 2
            _nudge(ids, ids.index(nid), desired, y_within, height, priority)

    columns = sorted(layout.row_order)
    for _ in range(_ALIGN_SWEEPS):
        for c in columns:
            pull(layout.row_order[c], left_partners)
        for c in reversed(columns):
            pull(layout.row_order[c], right_partners)


def _nudge(
    ids: list[int],
    i: int,
    desired: int,
    y: dict[int, int],
    height: dict[int, int],
    priority: dict[int, int],
) -> None:
    """Moves ``ids[i]`` as close to ``desired`` as its column-mates allow:
    strictly lower-priority nodes in the way get pushed along, while an
    equal-or-higher priority node is a hard barrier."""
    nid = ids[i]
    if desired > y[nid]:
        limit = desired
        needed = height[nid] + _BOX_GAP
        for k in range(i + 1, len(ids)):
            if priority[ids[k]] >= priority[nid]:
                limit = min(limit, y[ids[k]] - needed)
                break
            needed += height[ids[k]] + _BOX_GAP
        if limit <= y[nid]:
            return
        y[nid] = limit
        for k in range(i + 1, len(ids)):
            floor = y[ids[k - 1]] + height[ids[k - 1]] + _BOX_GAP
            if y[ids[k]] >= floor:
                break
            y[ids[k]] = floor
    elif desired < y[nid]:
        limit = desired
        needed = 0
        for k in range(i - 1, -1, -1):
            if priority[ids[k]] >= priority[nid]:
                limit = max(limit, y[ids[k]] + height[ids[k]] + _BOX_GAP + needed)
                break
            needed += height[ids[k]] + _BOX_GAP
        if limit >= y[nid]:
            return
        y[nid] = limit
        for k in range(i - 1, -1, -1):
            ceiling = y[ids[k + 1]] - height[ids[k]] - _BOX_GAP
            if y[ids[k]] <= ceiling:
                break
            y[ids[k]] = ceiling
