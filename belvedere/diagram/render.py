"""Computes concrete canvas geometry from a layout (box placement, channel
sizing, per-edge lane assignment) and routes every edge as an orthogonal path
between box borders. This is the only module that turns abstract rank/row
positions into character coordinates.
"""

from collections import defaultdict
from dataclasses import dataclass

from ..protocol import ColumnInfo, DiagramRegion
from .canvas import Canvas, _Line, _Segment
from .graph import GraphNode
from .layout import Layout, RoutedEdge

_CHANNEL_PADDING = 1
"""Blank columns/rows on each side of a channel, outside the lane columns/rows."""
_BOX_GAP = 1
"""Blank rows between boxes stacked within the same rank."""
_BAND_GAP = 3
"""Blank rows between vertically-stacked bands, hosting wrap-around connectors."""


def render(nodes: list[GraphNode], layout: Layout) -> tuple[str, list[DiagramRegion]]:
    box_lines = {n.id: _box_lines(n) for n in nodes}
    box_size = {nid: _box_size(lines) for nid, lines in box_lines.items()}
    dummy_set = {nid for nid, pos in layout.positions.items() if pos.dummy}
    band_size = layout.band_size

    lane_index = _assign_lanes(layout)
    coords, lane_coord = _place(layout, box_size, band_size, lane_index)

    canvas = Canvas()
    for node_id, lines in box_lines.items():
        top, left = coords[node_id]
        canvas.blit_box(lines, top, left)

    for redge in layout.routed_edges:
        canvas.draw_edge(
            _route(redge, layout, coords, box_size, dummy_set, band_size, lane_coord)
        )

    return canvas.render()


def _box_lines(node: GraphNode) -> list[_Line]:
    if node.unavailable:
        content_lines = ["(unavailable)"]
        display_cols: list[ColumnInfo] = []
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
    if node.unavailable or not node.columns:
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


def _box_size(lines: list[_Line]) -> tuple[int, int]:
    width = max(sum(len(seg.text) for seg in line) for line in lines)
    return len(lines), width


def _assign_lanes(layout: Layout) -> dict[int, dict[tuple[int, int], int]]:
    """For each rank boundary (between rank r and r+1), assigns every hop
    crossing it a distinct lane index, ordered by the row of its endpoint in
    the lower rank to keep same-channel lanes roughly uncrossed."""
    hops_by_boundary: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            boundary = min(layout.positions[u].rank, layout.positions[v].rank)
            hops_by_boundary[boundary].append((u, v))

    lane_index: dict[int, dict[tuple[int, int], int]] = {}
    for boundary, hops in hops_by_boundary.items():
        ordered = sorted(hops, key=lambda hop: layout.positions[hop[0]].row)
        lane_index[boundary] = {hop: i for i, hop in enumerate(ordered)}
    return lane_index


@dataclass
class _Placement:
    coords: dict[int, tuple[int, int]]
    lane_coord: dict[int, dict[tuple[int, int], int]]


def _place(
    layout: Layout,
    box_size: dict[int, tuple[int, int]],
    band_size: int,
    lane_index: dict[int, dict[tuple[int, int], int]],
) -> tuple[dict[int, tuple[int, int]], dict[int, dict[tuple[int, int], int]]]:
    by_rank: dict[int, list[int]] = defaultdict(list)
    for nid, pos in layout.positions.items():
        by_rank[pos.rank].append(nid)
    for ids in by_rank.values():
        ids.sort(key=lambda nid: layout.positions[nid].row)

    col_width = {
        r: max((box_size[nid][1] for nid in ids if nid in box_size), default=1)
        for r, ids in by_rank.items()
    }

    lane_count = {boundary: max(len(hops), 1) for boundary, hops in lane_index.items()}

    # y-offset of every node within its rank's vertical stack, and each rank's
    # total stacked height.
    y_within: dict[int, int] = {}
    rank_height: dict[int, int] = {}
    for r, ids in by_rank.items():
        y = 0
        for nid in ids:
            y_within[nid] = y
            h = box_size[nid][0] if nid in box_size else 1
            y += h + _BOX_GAP
        rank_height[r] = max(0, y - _BOX_GAP)

    max_rank = max(by_rank)
    bands = sorted({r // band_size for r in by_rank})

    band_top: dict[int, int] = {}
    y_cursor = 0
    for band in bands:
        band_top[band] = y_cursor
        ranks_in_band = [r for r in by_rank if r // band_size == band]
        band_top_height = max(rank_height.get(r, 0) for r in ranks_in_band)
        y_cursor += band_top_height + _BAND_GAP

    rank_left: dict[int, int] = {}
    channel_left: dict[int, int] = {}
    for band in bands:
        ranks_in_band = sorted(r for r in by_rank if r // band_size == band)
        x = 0
        for r in ranks_in_band:
            rank_left[r] = x
            x += col_width[r]
            is_wrap = (
                r == max_rank or (r + 1) not in by_rank or (r + 1) // band_size != band
            )
            if not is_wrap:
                channel_left[r] = x + _CHANNEL_PADDING
                x += _CHANNEL_PADDING * 2 + lane_count.get(r, 1)

    coords: dict[int, tuple[int, int]] = {}
    for r, ids in by_rank.items():
        band = r // band_size
        for nid in ids:
            coords[nid] = (band_top[band] + y_within[nid], rank_left[r])

    lane_coord: dict[int, dict[tuple[int, int], int]] = {}
    for boundary, hops in lane_index.items():
        is_wrap = (
            boundary == max_rank
            or (boundary + 1) not in by_rank
            or ((boundary + 1) // band_size != boundary // band_size)
        )
        if is_wrap:
            band = boundary // band_size
            base = band_top[band] + max(
                rank_height.get(r, 0) for r in by_rank if r // band_size == band
            )
            lane_coord[boundary] = {
                hop: base + _CHANNEL_PADDING + idx for hop, idx in hops.items()
            }
        else:
            base = channel_left[boundary]
            lane_coord[boundary] = {hop: base + idx for hop, idx in hops.items()}

    return coords, lane_coord


def _anchor(
    node_id: int,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    dummy_set: set[int],
    side: str,
) -> tuple[int, int]:
    row, col = coords[node_id]
    if node_id in dummy_set:
        return row, col
    h, w = box_size[node_id]
    if side == "right":
        return row + h // 2, col + w
    if side == "left":
        return row + h // 2, col - 1
    if side == "bottom":
        return row + h, col + w // 2
    return row - 1, col + w // 2  # "top"


def _route(
    redge: RoutedEdge,
    layout: Layout,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    dummy_set: set[int],
    band_size: int,
    lane_coord: dict[int, dict[tuple[int, int], int]],
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for u, v in zip(redge.nodes, redge.nodes[1:]):
        ru, rv = layout.positions[u].rank, layout.positions[v].rank
        boundary = min(ru, rv)
        forward = rv > ru
        wrap = boundary // band_size != max(ru, rv) // band_size
        coord = lane_coord[boundary][(u, v)]

        if not wrap:
            side_u = "right" if forward else "left"
            side_v = "left" if forward else "right"
            u_anchor = _anchor(u, coords, box_size, dummy_set, side_u)
            v_anchor = _anchor(v, coords, box_size, dummy_set, side_v)
            hop = [u_anchor, (u_anchor[0], coord), (v_anchor[0], coord), v_anchor]
        else:
            side_u = "bottom" if forward else "top"
            side_v = "top" if forward else "bottom"
            u_anchor = _anchor(u, coords, box_size, dummy_set, side_u)
            v_anchor = _anchor(v, coords, box_size, dummy_set, side_v)
            hop = [u_anchor, (coord, u_anchor[1]), (coord, v_anchor[1]), v_anchor]

        if points and points[-1] == hop[0]:
            points.extend(hop[1:])
        else:
            points.extend(hop)
    return points
