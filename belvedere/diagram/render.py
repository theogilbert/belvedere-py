"""Computes concrete canvas geometry from a layout (box placement, channel
sizing, per-edge lane assignment) and routes every edge as an orthogonal path
between box borders. This is the only module that turns abstract rank/row
positions into character coordinates.
"""

from collections import defaultdict

from ..protocol import ColumnInfo, DiagramRegion
from .canvas import Canvas, _Line, _Segment
from .graph import GraphNode
from .layout import Layout, RoutedEdge

_CHANNEL_PADDING = 1
"""Blank columns/rows on each side of a channel, outside the lane columns/rows."""
_BOX_GAP = 1
"""Blank rows between boxes stacked within the same rank."""
_BAND_GAP = 3
"""Minimum blank rows between vertically-stacked bands, hosting wrap-around
connectors; grows when more wrap lanes must fit."""
_ALIGN_SWEEPS = 2
"""Rounds of vertical alignment; each round pulls every rank toward its left
neighbors, then every rank toward its right neighbors."""
_DUMMY_PRIORITY = 1_000_000
"""Alignment priority of dummy bend-points — always above any real box's degree,
so long edges straighten out and boxes move out of their way."""


def render(nodes: list[GraphNode], layout: Layout) -> tuple[str, list[DiagramRegion]]:
    node_by_id = {n.id: n for n in nodes}
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
        points = _route(redge, layout, coords, box_size, dummy_set, lane_coord)
        if redge.one_to_one:
            start, end = "1", "1"
        else:
            start, end = ("*", "1") if redge.fk_at_start else ("1", "*")
        owner_id = redge.nodes[0] if redge.fk_at_start else redge.nodes[-1]
        owner_path = node_by_id[owner_id].path
        edge_path = [*owner_path, "relationships", redge.fk_column]
        canvas.draw_edge(points, start=start, end=end, path=edge_path)

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
        _Segment(node.name, node.path, kind="table"),
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
                [
                    _Segment("│ "),
                    _Segment(col.name, col_path, kind="column"),
                    _Segment(rest + " │"),
                ]
            )
        if hidden:
            ellipsis = content_lines[-1]
            padded = f"{ellipsis:<{inner_w}}"
            cols_path = [*node.path, "columns"]
            body.append(
                [
                    _Segment("│ "),
                    _Segment(ellipsis, cols_path, kind="column"),
                    _Segment(padded[len(ellipsis) :] + " │"),
                ]
            )

    return [top, *body, bottom]


def _box_size(lines: list[_Line]) -> tuple[int, int]:
    width = max(sum(len(seg.text) for seg in line) for line in lines)
    return len(lines), width


_Channel = tuple[str, int]
"""Where a hop's lane lives: ``("h", boundary_rank)`` for the vertical channel
between two in-band rank columns, ``("w", upper_band)`` for the horizontal wrap
region between two bands."""


def _hop_channel(u: int, v: int, layout: Layout) -> _Channel:
    pu, pv = layout.positions[u], layout.positions[v]
    if pu.band != pv.band:
        return ("w", min(pu.band, pv.band))
    return ("h", min(pu.rank, pv.rank))


def _assign_lanes(layout: Layout) -> dict[_Channel, dict[tuple[int, int], int]]:
    """For each channel, assigns every hop passing through it a distinct lane
    index, ordered by the row of the hop's first endpoint to keep same-channel
    lanes roughly uncrossed."""
    hops_by_channel: dict[_Channel, list[tuple[int, int]]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            hops_by_channel[_hop_channel(u, v, layout)].append((u, v))

    lane_index: dict[_Channel, dict[tuple[int, int], int]] = {}
    for channel, hops in hops_by_channel.items():
        ordered = sorted(hops, key=lambda hop: layout.positions[hop[0]].row)
        lane_index[channel] = {hop: i for i, hop in enumerate(ordered)}
    return lane_index


def _place(
    layout: Layout,
    box_size: dict[int, tuple[int, int]],
    band_size: int,
    lane_index: dict[_Channel, dict[tuple[int, int], int]],
) -> tuple[dict[int, tuple[int, int]], dict[_Channel, dict[tuple[int, int], int]]]:
    by_rank: dict[int, list[int]] = defaultdict(list)
    for nid, pos in layout.positions.items():
        by_rank[pos.rank].append(nid)
    for ids in by_rank.values():
        ids.sort(key=lambda nid: layout.positions[nid].row)

    col_width = {
        r: max((box_size[nid][1] for nid in ids if nid in box_size), default=1)
        for r, ids in by_rank.items()
    }

    lane_count = {channel: max(len(hops), 1) for channel, hops in lane_index.items()}

    height = {
        nid: box_size[nid][0] if nid in box_size else 1 for nid in layout.positions
    }

    # y-offset of every node within its band: stack top-down first, then nudge
    # nodes toward their neighbors' rows so edges run straight where possible.
    y_within: dict[int, int] = {}
    for ids in by_rank.values():
        y = 0
        for nid in ids:
            y_within[nid] = y
            y += height[nid] + _BOX_GAP

    _align_rows(layout, by_rank, y_within, height)

    bands = sorted({r // band_size for r in by_rank})

    band_top: dict[int, int] = {}
    band_height: dict[int, int] = {}
    y_cursor = 0
    for band in bands:
        in_band = [
            nid for r, ids in by_rank.items() if r // band_size == band for nid in ids
        ]
        shift = min(y_within[nid] for nid in in_band)
        for nid in in_band:
            y_within[nid] -= shift
        band_top[band] = y_cursor
        band_height[band] = max(y_within[nid] + height[nid] for nid in in_band)
        wrap_lanes = len(lane_index.get(("w", band), {}))
        y_cursor += band_height[band] + max(
            _BAND_GAP, 2 * _CHANNEL_PADDING + wrap_lanes
        )

    # Bands snake (LayoutNode.col mirrors odd bands) and share one global
    # column grid, so a wrapped rank lines up under its neighbor in the band
    # above. Every display column is as wide as its widest rank in any band,
    # and every inter-column channel as wide as its busiest band's lanes.
    rank_col = {r: layout.positions[ids[0]].col for r, ids in by_rank.items()}
    disp_width: dict[int, int] = defaultdict(int)
    for r in by_rank:
        disp_width[rank_col[r]] = max(disp_width[rank_col[r]], col_width[r])
    gap_lanes: dict[int, int] = defaultdict(int)
    for channel in lane_index:
        kind, boundary = channel
        if kind == "h":
            c = min(rank_col[boundary], rank_col[boundary + 1])
            gap_lanes[c] = max(gap_lanes[c], lane_count[channel])

    max_col = max(rank_col.values())
    col_x: dict[int, int] = {}
    chan_x: dict[int, int] = {}
    x = 0
    for c in range(max_col + 1):
        col_x[c] = x
        x += disp_width.get(c, 0)
        if c < max_col:
            chan_x[c] = x + _CHANNEL_PADDING
            x += _CHANNEL_PADDING * 2 + max(gap_lanes.get(c, 0), 1)

    rank_left = {r: col_x[rank_col[r]] for r in by_rank}
    channel_left = {
        boundary: chan_x[min(rank_col[boundary], rank_col[boundary + 1])]
        for kind, boundary in lane_index
        if kind == "h"
    }

    coords: dict[int, tuple[int, int]] = {}
    for r, ids in by_rank.items():
        band = r // band_size
        for nid in ids:
            coords[nid] = (band_top[band] + y_within[nid], rank_left[r])

    lane_coord: dict[_Channel, dict[tuple[int, int], int]] = {}
    for channel, hops in lane_index.items():
        kind, idx = channel
        if kind == "w":
            base = band_top[idx] + band_height[idx]
            lane_coord[channel] = {
                hop: base + _CHANNEL_PADDING + i for hop, i in hops.items()
            }
        else:
            base = channel_left[idx]
            lane_coord[channel] = {hop: base + i for hop, i in hops.items()}

    return coords, lane_coord


def _align_rows(
    layout: Layout,
    by_rank: dict[int, list[int]],
    y_within: dict[int, int],
    height: dict[int, int],
) -> None:
    """Nudges nodes up or down within their rank so edge endpoints land on the
    same row wherever possible, giving edges a straight corridor instead of a
    detour around the boxes of intermediate ranks. Priority method: dummy nodes
    are pure bend points, so they outrank real boxes and push them aside; among
    real boxes, the better-connected one wins."""
    left_partners: dict[int, list[int]] = defaultdict(list)
    right_partners: dict[int, list[int]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            if layout.positions[u].band != layout.positions[v].band:
                continue  # wrap hop — routed vertically, row alignment is moot
            ru, rv = layout.positions[u].rank, layout.positions[v].rank
            lo, hi = (u, v) if ru < rv else (v, u)
            right_partners[lo].append(hi)
            left_partners[hi].append(lo)

    priority = {
        nid: _DUMMY_PRIORITY
        if pos.dummy
        else len(left_partners[nid]) + len(right_partners[nid])
        for nid, pos in layout.positions.items()
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

    ranks = sorted(by_rank)
    for _ in range(_ALIGN_SWEEPS):
        for r in ranks:
            pull(by_rank[r], left_partners)
        for r in reversed(ranks):
            pull(by_rank[r], right_partners)


def _nudge(
    ids: list[int],
    i: int,
    desired: int,
    y: dict[int, int],
    height: dict[int, int],
    priority: dict[int, int],
) -> None:
    """Moves ``ids[i]`` as close to ``desired`` as its rank-mates allow: strictly
    lower-priority nodes in the way get pushed along, while an equal-or-higher
    priority node is a hard barrier."""
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
    lane_coord: dict[_Channel, dict[tuple[int, int], int]],
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for u, v in zip(redge.nodes, redge.nodes[1:]):
        channel = _hop_channel(u, v, layout)
        coord = lane_coord[channel][(u, v)]

        if channel[0] == "h":
            # In an odd (right-to-left) band a forward hop travels leftward, so
            # pick sides from the display columns rather than the ranks.
            rightward = layout.positions[v].col > layout.positions[u].col
            side_u = "right" if rightward else "left"
            side_v = "left" if rightward else "right"
            u_anchor = _anchor(u, coords, box_size, dummy_set, side_u)
            v_anchor = _anchor(v, coords, box_size, dummy_set, side_v)
            hop = [u_anchor, (u_anchor[0], coord), (v_anchor[0], coord), v_anchor]
        else:
            downward = layout.positions[v].band > layout.positions[u].band
            side_u = "bottom" if downward else "top"
            side_v = "top" if downward else "bottom"
            u_anchor = _anchor(u, coords, box_size, dummy_set, side_u)
            v_anchor = _anchor(v, coords, box_size, dummy_set, side_v)
            hop = [u_anchor, (coord, u_anchor[1]), (coord, v_anchor[1]), v_anchor]

        if points and points[-1] == hop[0]:
            points.extend(hop[1:])
        else:
            points.extend(hop)
    return points
