"""Computes concrete canvas geometry from a layout (box placement, channel
sizing, per-edge lane assignment) and routes every edge as an orthogonal path
between box borders. This is the only module that turns abstract rank/row
positions into character coordinates.
"""

import functools
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
_LONG_SKIP_RANKS = 2
"""Ranks a skip edge can cross before it's treated as long-haul and routed
around the diagram's content (see `_place_skip_waypoints`) instead of
through it."""


def render(nodes: list[GraphNode], layout: Layout) -> tuple[str, list[DiagramRegion]]:
    node_by_id = {n.id: n for n in nodes}
    box_lines = {n.id: _box_lines(n) for n in nodes}
    box_size = {nid: _box_size(lines) for nid, lines in box_lines.items()}
    dummy_set = {nid for nid, pos in layout.positions.items() if pos.dummy}
    band_size = layout.band_size
    vertical_hops = _skip_hop_ends(layout)

    lane_index = _assign_lanes(layout, vertical_hops)
    coords, _ = _place(layout, box_size, band_size, lane_index, dummy_set)
    anchor_slots = _assign_anchor_slots(
        layout, dummy_set, coords, box_size, vertical_hops
    )
    lane_index, detour_hops = _reorder_lanes(
        layout, lane_index, anchor_slots, coords, box_size, dummy_set, vertical_hops
    )
    # Re-placing waypoints now that anchor_slots is known (real box
    # coordinates are unaffected, so this only refines dummy positions)
    # removes a residual jog on the final hop into any target that shares
    # its side with another edge.
    coords, lane_coord = _place(
        layout, box_size, band_size, lane_index, dummy_set, anchor_slots
    )
    # Beyond every real box's right edge — a shared detour lane for hops
    # whose crossing (see `_reorder_lanes`) no lane order can avoid.
    outer_col = (
        max(coords[nid][1] + box_size[nid][1] for nid in box_size) + _CHANNEL_PADDING
    )

    canvas = Canvas()
    for node_id, lines in box_lines.items():
        top, left = coords[node_id]
        canvas.blit_box(lines, top, left)

    for redge in layout.routed_edges:
        points = _route(
            redge,
            layout,
            coords,
            box_size,
            dummy_set,
            lane_coord,
            anchor_slots,
            vertical_hops,
            detour_hops,
            outer_col,
        )
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


_Channel = tuple[str, int]
"""Where a hop's lane lives: ``("h", boundary_rank)`` for the vertical channel
between two in-band rank columns, ``("w", upper_band)`` for the horizontal wrap
region between two bands, ``("v", node_id)`` for the local vertical-exit region
just below one specific box — keyed by node, not rank, since a rank can stack
several boxes and the exit must stay scoped to its own box's bottom edge, not
whichever rank-mate happens to sit lowest."""


def _skip_hop_ends(layout: Layout) -> set[tuple[int, int]]:
    """The first hop of every multi-rank skip edge (one decomposed through at
    least one dummy waypoint) — the leg leaving the edge's real source. This
    gets a vertical exit (top or bottom) instead of competing with direct
    neighbors for the box's left/right sides, which stay reserved for hops
    between adjacent ranks that benefit from precise row alignment far more
    than a long skip edge does over its much longer path."""
    hops = set()
    for redge in layout.routed_edges:
        if len(redge.nodes) > 2:
            hops.add((redge.nodes[0], redge.nodes[1]))
    return hops


def _prefers_top_exit(u: int, layout: Layout) -> bool:
    """Whether a same-band vertical exit from ``u`` should go up instead of
    down — picks whichever direction has fewer rank-mates stacked in the
    way, using ordinal stacking position within the rank as a cheap proxy
    for available slack. A box near the top of the diagram may not have
    room above it for this, which can push rows negative — `_place` shifts
    the whole diagram down afterwards to cover that, rather than special-
    casing it here, so this stays a single simple rule every caller agrees
    on."""
    pu = layout.positions[u]
    above = sum(
        1
        for pos in layout.positions.values()
        if pos.rank == pu.rank and pos.row < pu.row
    )
    below = sum(
        1
        for pos in layout.positions.values()
        if pos.rank == pu.rank and pos.row > pu.row
    )
    return above < below


def _hop_channel(u: int, v: int, layout: Layout, vertical: bool = False) -> _Channel:
    pu, pv = layout.positions[u], layout.positions[v]
    if pu.band != pv.band:
        return ("w", min(pu.band, pv.band))
    if vertical:
        # Same-band skip-edge exit: a local vertical run just above or below
        # this specific box (see `_route`'s non-"h" shape), not a genuine
        # band boundary — kept separate from "w" so it doesn't have to
        # detour all the way past every other rank's content in the band.
        return ("v", u)
    return ("h", min(pu.rank, pv.rank))


def _assign_lanes(
    layout: Layout, vertical_hops: set[tuple[int, int]]
) -> dict[_Channel, dict[tuple[int, int], int]]:
    """For each channel, assigns every hop passing through it a distinct lane
    index, ordered by the rank-sibling ordinal of the hop's first endpoint.
    Used for an initial pass, before real placement is known — only the
    resulting *count* per channel matters at this stage (channel width). See
    `_reorder_lanes` for the placement-aware pass that fixes each hop's
    actual lane position."""
    hops_by_channel: dict[_Channel, list[tuple[int, int]]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            channel = _hop_channel(u, v, layout, (u, v) in vertical_hops)
            hops_by_channel[channel].append((u, v))

    lane_index: dict[_Channel, dict[tuple[int, int], int]] = {}
    for channel, hops in hops_by_channel.items():
        ordered = sorted(hops, key=lambda hop: layout.positions[hop[0]].row)
        lane_index[channel] = {hop: i for i, hop in enumerate(ordered)}
    return lane_index


def _reorder_lanes(
    layout: Layout,
    lane_index: dict[_Channel, dict[tuple[int, int], int]],
    anchor_slots: dict[tuple[int, tuple[int, int]], tuple[int, int]],
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    dummy_set: set[int],
    vertical_hops: set[tuple[int, int]],
) -> tuple[dict[_Channel, dict[tuple[int, int], int]], set[tuple[int, int]]]:
    """Re-orders each channel's lanes now that every hop's actual endpoint
    anchors are known, so a hop that must jog from its source row/column to
    its target doesn't cut across another hop's own row/column on the way —
    exactly the crossing a same-side fan-out would otherwise create right
    where both hops leave their shared box.

    A hop occupies its [min(source, target), max(source, target)] span for
    its whole run up to its own lane, then collapses to just its target from
    there on — so placing hop A in the lane closest to the box is safe with
    respect to hop B only if B's source falls outside A's span (`_fits_closer`).
    When exactly one of the two orders is safe, that settles it. When
    *neither* order is safe, no lane assignment can avoid a crossing between
    that pair by itself — that hop is reported back in the second return
    value so `_route` can instead detour it around the conflict entirely
    (see `_LONG_SKIP_RANKS`-style routing in `_place_skip_waypoints` for the
    same idea applied to skip-edge waypoints). Otherwise, ties fall back to a
    stable ordering by span start."""

    def endpoints(hop: tuple[int, int], axis: int) -> tuple[int, int]:
        u, v = hop
        side_u, side_v = _hop_sides(u, v, layout, hop in vertical_hops)
        slot_u, count_u = anchor_slots.get((u, hop), (0, 1))
        slot_v, count_v = anchor_slots.get((v, hop), (0, 1))
        u_anchor = _anchor(u, coords, box_size, dummy_set, side_u, slot_u, count_u)
        v_anchor = _anchor(v, coords, box_size, dummy_set, side_v, slot_v, count_v)
        return u_anchor[axis], v_anchor[axis]

    def fits_closer(source: int, span: tuple[int, int]) -> bool:
        lo, hi = span
        return not (lo <= source <= hi)

    reordered: dict[_Channel, dict[tuple[int, int], int]] = {}
    needs_detour: set[tuple[int, int]] = set()
    for channel, hops in lane_index.items():
        axis = 0 if channel[0] == "h" else 1
        info = {hop: endpoints(hop, axis) for hop in hops}
        spans = {hop: (min(s, t), max(s, t)) for hop, (s, t) in info.items()}

        def closer(a: tuple[int, int], b: tuple[int, int]) -> int:
            a_ok = fits_closer(info[b][0], spans[a])
            b_ok = fits_closer(info[a][0], spans[b])
            if a_ok and not b_ok:
                return -1
            if b_ok and not a_ok:
                return 1
            return (spans[a][0] - spans[b][0]) or (spans[a][1] - spans[b][1])

        ordered = sorted(hops, key=functools.cmp_to_key(closer))

        # With 3+ hops, "closer" isn't guaranteed transitive (a can beat b,
        # b beat c, and c beat a), so even the best achievable order can
        # still leave some pair unsafe. Check every pair against the order
        # actually produced, not just in isolation: for each closer/farther
        # pair, the farther hop's own source-to-lane sweep crosses the
        # closer hop's lane-row run if the farther hop's source falls
        # inside the closer hop's span — that farther hop goes around.
        for i, closer_hop in enumerate(ordered):
            for farther_hop in ordered[i + 1 :]:
                if not fits_closer(info[farther_hop][0], spans[closer_hop]):
                    needs_detour.add(farther_hop)

        reordered[channel] = {hop: i for i, hop in enumerate(ordered)}
    return reordered, needs_detour


def _place(
    layout: Layout,
    box_size: dict[int, tuple[int, int]],
    band_size: int,
    lane_index: dict[_Channel, dict[tuple[int, int], int]],
    dummy_set: set[int],
    anchor_slots: dict[tuple[int, tuple[int, int]], tuple[int, int]] | None = None,
) -> tuple[dict[int, tuple[int, int]], dict[_Channel, dict[tuple[int, int], int]]]:
    by_rank: dict[int, list[int]] = defaultdict(list)
    for nid, pos in layout.positions.items():
        by_rank[pos.rank].append(nid)
    for ids in by_rank.values():
        ids.sort(key=lambda nid: layout.positions[nid].row)

    # Skip-edge waypoints ("dummy" bend points) don't stack alongside real
    # boxes at all — they're placed afterwards, once real box positions are
    # final (see `_place_skip_waypoints`), so a skip edge never displaces or
    # takes stacking space from a table it happens to pass by.
    real_by_rank = {
        r: [nid for nid in ids if nid in box_size] for r, ids in by_rank.items()
    }

    col_width = {
        r: max((box_size[nid][1] for nid in ids if nid in box_size), default=1)
        for r, ids in by_rank.items()
    }

    lane_count = {channel: max(len(hops), 1) for channel, hops in lane_index.items()}

    height = {
        nid: box_size[nid][0] if nid in box_size else 1 for nid in layout.positions
    }

    # y-offset of every real box within its band: stack top-down first, then
    # nudge boxes toward their neighbors' rows so edges run straight where
    # possible.
    y_within: dict[int, int] = {}
    for ids in real_by_rank.values():
        y = 0
        for nid in ids:
            y_within[nid] = y
            y += height[nid] + _BOX_GAP

    _align_rows(layout, real_by_rank, y_within, height)

    bands = sorted({r // band_size for r in by_rank})

    band_top: dict[int, int] = {}
    band_height: dict[int, int] = {}
    y_cursor = 0
    for band in bands:
        in_band = [
            nid
            for r, ids in real_by_rank.items()
            if r // band_size == band
            for nid in ids
        ]
        band_top[band] = y_cursor
        if not in_band:
            # a band with only skip-edge waypoints passing through it, no
            # real box of its own
            band_height[band] = 1
        else:
            shift = min(y_within[nid] for nid in in_band)
            for nid in in_band:
                y_within[nid] -= shift
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
    for r, ids in real_by_rank.items():
        band = r // band_size
        for nid in ids:
            coords[nid] = (band_top[band] + y_within[nid], rank_left[r])

    _place_skip_waypoints(layout, coords, box_size, rank_left, dummy_set, anchor_slots)

    lane_coord: dict[_Channel, dict[tuple[int, int], int]] = {}
    for channel, hops in lane_index.items():
        kind, idx = channel
        if kind == "w":
            base = band_top[idx] + band_height[idx]
            lane_coord[channel] = {
                hop: base + _CHANNEL_PADDING + i for hop, i in hops.items()
            }
        elif kind == "v":
            # Just above or below this specific box, not its whole rank (a
            # rank can stack several boxes) or the whole band, so the exit
            # stays a short local dip. Going up can push rows negative for a
            # box near the top of the diagram — handled below by shifting
            # everything down, rather than special-cased here, so this
            # always agrees with `_hop_sides`'s choice of side.
            box_top = coords[idx][0]
            if _prefers_top_exit(idx, layout):
                lane_coord[channel] = {
                    hop: box_top - _CHANNEL_PADDING - 1 - i for hop, i in hops.items()
                }
            else:
                box_bottom = box_top + box_size[idx][0]
                lane_coord[channel] = {
                    hop: box_bottom + _CHANNEL_PADDING + i for hop, i in hops.items()
                }
        else:
            base = channel_left[idx]
            lane_coord[channel] = {hop: base + i for hop, i in hops.items()}

    # A top exit can land above row 0 for a box near the top of the diagram;
    # shift every row-valued coordinate down rather than clamping, so the
    # exit keeps the geometry `_hop_sides` chose instead of collapsing back
    # onto the box it was meant to clear.
    row_values = [r for r, _ in coords.values()]
    row_values += [
        v
        for channel, vals in lane_coord.items()
        if channel[0] != "h"
        for v in vals.values()
    ]
    shift = -min(row_values, default=0)
    if shift > 0:
        coords = {nid: (r + shift, c) for nid, (r, c) in coords.items()}
        lane_coord = {
            channel: (
                {hop: v + shift for hop, v in vals.items()}
                if channel[0] != "h"
                else vals
            )
            for channel, vals in lane_coord.items()
        }

    return coords, lane_coord


def _place_skip_waypoints(
    layout: Layout,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    rank_left: dict[int, int],
    dummy_set: set[int],
    anchor_slots: dict[tuple[int, tuple[int, int]], tuple[int, int]] | None = None,
) -> None:
    """Places every skip-edge waypoint ("dummy" bend point) at its edge's
    target row directly, rather than interpolating from the source, so only
    the first hop (out of the source, which may already bend via a vertical
    exit — see `_skip_hop_ends`) needs to bend at all; every subsequent hop,
    including the final one into the target, then runs flat.

    The target row itself is the target's real anchor if ``anchor_slots`` is
    given (the second, placement-aware pass — see `render`), or its
    approximate anchor otherwise (as if it had no fan-out of its own, which
    isn't known yet on the first pass). Using the real anchor once it's
    known removes a residual jog that would otherwise show up whenever the
    target shares its side with another edge, since its true row can differ
    from the count-of-one estimate.

    A chain crossing more than two ranks (`_LONG_SKIP_RANKS`) instead cruises
    at a shared row beyond every real box (`_cruise_row`) for every waypoint
    but its last, only dropping to the target's row on the final approach —
    so a long-haul edge passing near a target several other, more local
    edges also reach doesn't converge onto the same row as them for its
    entire crossing and cross their paths; it goes around instead.

    Each waypoint is clamped clear of any real box's row range at its own
    rank — so the edge passes the box on its own row instead of cutting
    through its interior (which the canvas would silently swallow, since box
    cells always win) or detouring around it via extra stacking space.
    Waypoints share the rank's normal column; this runs after real box
    placement is final, since it depends on real boxes' finished
    coordinates. Mutates `coords`."""
    cruise_row = _cruise_row(box_size, coords)
    for redge in layout.routed_edges:
        chain = redge.nodes
        if len(chain) < 3:
            continue  # direct hop, no waypoints

        last_hop = (chain[-2], chain[-1])
        _, entry_side = _hop_sides(*last_hop, layout)
        slot, count = (
            anchor_slots.get((chain[-1], last_hop), (0, 1))
            if anchor_slots is not None
            else (0, 1)
        )
        end_row = _anchor(
            chain[-1], coords, box_size, dummy_set, entry_side, slot, count
        )[0]

        waypoints = chain[1:-1]
        long_haul = len(chain) - 1 > _LONG_SKIP_RANKS
        for i, nid in enumerate(waypoints):
            target_row = (
                end_row if not long_haul or i == len(waypoints) - 1 else cruise_row
            )
            rank = layout.positions[nid].rank
            row = _clamp_outside_boxes(target_row, layout, box_size, coords, rank)
            coords[nid] = (row, rank_left[rank])

    _resolve_waypoint_collisions(layout, coords, box_size)


def _cruise_row(
    box_size: dict[int, tuple[int, int]], coords: dict[int, tuple[int, int]]
) -> int:
    """A row beyond every real box in the diagram, used as a shared outer
    lane for long-haul skip-edges so they travel around the diagram's
    content instead of through its middle, where shorter, more local edges
    live."""
    return max(coords[nid][0] + box_size[nid][0] for nid in box_size) + _CHANNEL_PADDING


def _box_row_ranges(
    layout: Layout,
    box_size: dict[int, tuple[int, int]],
    coords: dict[int, tuple[int, int]],
    rank: int,
) -> list[tuple[int, int]]:
    """Inclusive row ranges occupied by every real box at `rank`."""
    ranges = []
    for nid, pos in layout.positions.items():
        if pos.rank == rank and nid in box_size:
            top, _ = coords[nid]
            h, _ = box_size[nid]
            ranges.append((top, top + h - 1))
    return ranges


def _clamp_outside_boxes(
    row: int,
    layout: Layout,
    box_size: dict[int, tuple[int, int]],
    coords: dict[int, tuple[int, int]],
    rank: int,
) -> int:
    """Nudges `row` to the nearest row outside every real box at `rank`."""
    return _clamp_outside(row, _box_row_ranges(layout, box_size, coords, rank))


def _clamp_outside(row: int, ranges: list[tuple[int, int]]) -> int:
    changed = True
    while changed:
        changed = False
        for lo, hi in ranges:
            if lo <= row <= hi:
                row = lo - 1 if row - lo <= hi - row else hi + 1
                changed = True
    return row


def _resolve_waypoint_collisions(
    layout: Layout,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
) -> None:
    """Nudges apart any skip-edge waypoints that landed on the same row
    within the same rank, keeping them clear of that rank's real box(es)."""
    by_rank: dict[int, list[int]] = defaultdict(list)
    for nid, pos in layout.positions.items():
        if pos.dummy and nid in coords:
            by_rank[pos.rank].append(nid)

    for rank, ids in by_rank.items():
        ranges = _box_row_ranges(layout, box_size, coords, rank)
        ordered = sorted(ids, key=lambda nid: coords[nid][0])
        prev_row: int | None = None
        for nid in ordered:
            row, col = coords[nid]
            if prev_row is not None and row <= prev_row:
                row = prev_row + 1
            row = _clamp_outside(row, ranges)
            coords[nid] = (row, col)
            prev_row = row


def _align_rows(
    layout: Layout,
    by_rank: dict[int, list[int]],
    y_within: dict[int, int],
    height: dict[int, int],
) -> None:
    """Nudges real boxes up or down within their rank so a direct edge between
    two adjacent ranks lands on the same row on both ends, giving it a
    straight line instead of a jog. Only considers real-to-real edges —
    skip-edges route around obstacles via a bypass lane instead of displacing
    boxes (see `_route_bypass_dummies`), so their waypoints don't participate
    here. Priority: the better-connected box wins a contested nudge."""
    left_partners: dict[int, list[int]] = defaultdict(list)
    right_partners: dict[int, list[int]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            if layout.positions[u].dummy or layout.positions[v].dummy:
                continue  # skip-edge waypoint — handled by the bypass lane
            if layout.positions[u].band != layout.positions[v].band:
                continue  # wrap hop — routed vertically, row alignment is moot
            ru, rv = layout.positions[u].rank, layout.positions[v].rank
            lo, hi = (u, v) if ru < rv else (v, u)
            right_partners[lo].append(hi)
            left_partners[hi].append(lo)

    priority = {
        nid: len(left_partners[nid]) + len(right_partners[nid])
        for nid in layout.positions
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


def _hop_sides(
    u: int, v: int, layout: Layout, vertical: bool = False
) -> tuple[str, str]:
    """Which side of ``u`` and ``v`` a hop between them attaches to.
    ``vertical`` forces a same-band hop to exit/enter top/bottom instead of
    left/right — used for a skip edge's real ends (see `_skip_hop_ends`)."""
    pu, pv = layout.positions[u], layout.positions[v]
    if pu.band != pv.band:
        downward = pv.band > pu.band
        return ("bottom", "top") if downward else ("top", "bottom")
    if vertical:
        return ("top", "bottom") if _prefers_top_exit(u, layout) else ("bottom", "top")
    # In an odd (right-to-left) band a forward hop travels leftward, so pick
    # sides from the display columns rather than the ranks.
    rightward = pv.col > pu.col
    return ("right", "left") if rightward else ("left", "right")


def _assign_anchor_slots(
    layout: Layout,
    dummy_set: set[int],
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    vertical_hops: set[tuple[int, int]],
) -> dict[tuple[int, tuple[int, int]], tuple[int, int]]:
    """Assigns every hop touching a real box's side a slot among its side-mates,
    keyed by ``(node_id, hop)``, so several edges through the same side fan out
    across the box's border instead of bunching at one fixed point. Dummy nodes
    (pure bend points) are excluded — only real boxes have a border to spread
    across. Each side's hops are ordered by the other endpoint's approximate
    anchor value (its center, i.e. as if it had no other edges of its own — a
    real other-endpoint's own fan-out isn't known yet here, but this is a much
    closer proxy than its box's raw top-left coordinate), so a hop's slot here
    tends to land where it's really headed instead of jogging to reach it.

    This monotonic (order-preserving) assignment is deliberately conservative:
    a hop that lands exactly on its target occupies that row across its
    *entire* span back to the box, since a same-row hop never bends away from
    it — which would leave no room for a neighboring hop to cross that row at
    all, forcing an actual crossing rather than a bend. Preferring a small,
    consistent jog over some hops landing exactly keeps that room open; see
    `_reorder_lanes`, which then orders lanes so those jogs don't cross each
    other either."""
    groups: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
    for redge in layout.routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            side_u, side_v = _hop_sides(u, v, layout, (u, v) in vertical_hops)
            if u not in dummy_set:
                groups[(u, side_u)].append((u, v))
            if v not in dummy_set:
                groups[(v, side_v)].append((u, v))

    def other_anchor_value(hop: tuple[int, int], node_id: int, axis: int) -> int:
        u, v = hop
        side_u, side_v = _hop_sides(u, v, layout, hop in vertical_hops)
        other, other_side = (v, side_v) if node_id == u else (u, side_u)
        return _anchor(other, coords, box_size, dummy_set, other_side, 0, 1)[axis]

    slots: dict[tuple[int, tuple[int, int]], tuple[int, int]] = {}
    for (node_id, side), hops in groups.items():
        axis = 0 if side in ("left", "right") else 1
        ordered = sorted(hops, key=lambda hop: other_anchor_value(hop, node_id, axis))
        count = len(ordered)
        for i, hop in enumerate(ordered):
            slots[(node_id, hop)] = (i, count)
    return slots


def _spread(slot: int, count: int, size: int) -> int:
    """Spreads ``count`` slots evenly across ``size`` interior positions
    (0-indexed), centering a single slot rather than pinning it to an edge."""
    if size <= 1 or count <= 1:
        return max(size, 1) // 2
    return round(slot * (size - 1) / (count - 1))


def _anchor(
    node_id: int,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    dummy_set: set[int],
    side: str,
    slot: int = 0,
    count: int = 1,
) -> tuple[int, int]:
    row, col = coords[node_id]
    if node_id in dummy_set:
        return row, col
    h, w = box_size[node_id]
    if side == "right":
        return row + 1 + _spread(slot, count, h - 2), col + w
    if side == "left":
        return row + 1 + _spread(slot, count, h - 2), col - 1
    if side == "bottom":
        return row + h, col + 1 + _spread(slot, count, w - 2)
    return row - 1, col + 1 + _spread(slot, count, w - 2)  # "top"


def _route(
    redge: RoutedEdge,
    layout: Layout,
    coords: dict[int, tuple[int, int]],
    box_size: dict[int, tuple[int, int]],
    dummy_set: set[int],
    lane_coord: dict[_Channel, dict[tuple[int, int], int]],
    anchor_slots: dict[tuple[int, tuple[int, int]], tuple[int, int]],
    vertical_hops: set[tuple[int, int]],
    detour_hops: set[tuple[int, int]],
    outer_col: int,
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for u, v in zip(redge.nodes, redge.nodes[1:]):
        vertical = (u, v) in vertical_hops
        channel = _hop_channel(u, v, layout, vertical)
        coord = lane_coord[channel][(u, v)]
        side_u, side_v = _hop_sides(u, v, layout, vertical)
        slot_u, count_u = anchor_slots.get((u, (u, v)), (0, 1))
        slot_v, count_v = anchor_slots.get((v, (u, v)), (0, 1))
        u_anchor = _anchor(u, coords, box_size, dummy_set, side_u, slot_u, count_u)
        v_anchor = _anchor(v, coords, box_size, dummy_set, side_v, slot_v, count_v)

        if channel[0] == "h":
            hop = [u_anchor, (u_anchor[0], coord), (v_anchor[0], coord), v_anchor]
        elif (u, v) in detour_hops:
            # No lane order avoids this hop's crossing (`_reorder_lanes`) —
            # go around instead: out to a shared lane past every box's right
            # edge first, then down/up to the channel row, then in.
            hop = [
                u_anchor,
                (u_anchor[0], outer_col),
                (coord, outer_col),
                (coord, v_anchor[1]),
                v_anchor,
            ]
        else:
            hop = [u_anchor, (coord, u_anchor[1]), (coord, v_anchor[1]), v_anchor]

        if points and points[-1] == hop[0]:
            points.extend(hop[1:])
        else:
            points.extend(hop)
    return points
