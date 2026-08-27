"""Renders a table and all tables connected to it (recursively via foreign keys)
as an ASCII graph diagram.

Every reachable table is discovered once (see ``graph.py``) and drawn as
exactly one box — never as floating text, never twice. Boxes are placed with
a hub-centered layered layout (see ``layout.py``): the most-connected table
becomes a structural hub, and every other table gets a signed column by BFS
distance and side. ``place.py`` turns that abstract layout into concrete box
rectangles with overprovisioned channels; ``route.py`` runs a per-edge A*
search over the resulting character grid to draw every relationship as an
orthogonal connector that never crosses a box, then compacts the unused
overprovisioned space back out. See ``canvas.py`` for the character-grid
primitives both stages draw onto.

Every table and column name drawn in the diagram is also tracked as a
:class:`~grannos.protocol.DiagramRegion`, so a client can map a cursor
position back to an ``explore.describe`` path.
"""

from dataclasses import dataclass

from ..protocol import DiagramRegion
from .canvas import Canvas
from .graph import Describe, DiagramError, GraphEdge, GraphNode, discover
from .layout import Layout, compute_layout
from .place import PlaceResult, Spacing, place
from .route import NoRouteError, RoutedEdge, compact, route

__all__ = ["DiagramError", "DiagramResult", "build_diagram"]


def _spacing_ladder(edge_count: int) -> list[Spacing]:
    """Placements to try in order until every edge routes.

    Each rung hands ``route.py`` more anchors and more lanes. The row gap goes
    first: at a gap of 1 no stacked box can anchor an edge on the side facing
    its neighbour, so widening it is what actually adds anchors, and it costs
    only blank lines. Channel width comes next, and the last two rungs size it
    from the graph — a channel with a lane per edge in the whole diagram
    cannot be congested, whatever shape the layout took. Width is the scarcer
    direction on a terminal, which is why it is spent last and only as far as
    the graph demands.
    """
    return [
        Spacing(),
        Spacing(box_gap=2),
        Spacing(box_gap=2, channel_padding=3),
        Spacing(box_gap=3, channel_padding=1 + edge_count // 2),
        Spacing(box_gap=3, channel_padding=1 + edge_count),
    ]


@dataclass
class DiagramResult:
    diagram: str
    """The rendered diagram, as a multi-line string."""
    regions: list[DiagramRegion]
    """Byte-offset spans naming a table or column at each point in ``diagram``."""


async def build_diagram(path: list[str], describe: Describe) -> DiagramResult:
    """Fetch the table at ``path`` and all connected tables, and render them as
    an ASCII graph diagram.

    Args:
        path: Path segments identifying a table (e.g. ``["dbo", "orders"]``).
        describe: Async callback resolving a path to its describe result.

    Returns:
        The rendered diagram text and the regions naming each table/column
        drawn within it. No max width/height is applied to the diagram text —
        the caller should render it without line-wrapping.

    Raises:
        DiagramError: If path does not resolve to a table, or if no spacing
            lets every relationship be drawn (see ``_place_and_route``).
    """
    nodes, edges = await discover(path, describe)
    layout = compute_layout(nodes, edges)
    place_result, routed_edges = _place_and_route(nodes, edges, layout)
    rects, routed_edges, _ = compact(place_result.rects, routed_edges)

    canvas = Canvas()
    for node_id, lines in place_result.box_lines.items():
        rect = rects[node_id]
        canvas.blit_box(lines, rect.top, rect.left)
    for redge in routed_edges:
        canvas.draw_edge(
            redge.points, start=redge.start, end=redge.end, path=redge.path
        )

    diagram, regions = canvas.render()
    return DiagramResult(diagram=diagram, regions=regions)


def _place_and_route(
    nodes: list[GraphNode], edges: list[GraphEdge], layout: Layout
) -> tuple[PlaceResult, list[RoutedEdge]]:
    """Place and route the graph, re-placing with a roomier spacing whenever an
    edge comes out unroutable.

    An unroutable edge means the router ran out of free anchors or lanes
    around a box, not that the graph is undrawable, so every retry simply
    gives it more of both, up to a last rung sized from the graph itself. The
    tight default is kept as the first attempt because it is what almost every
    diagram routes on, and the roomier ones cost real terminal space.

    Raises:
        DiagramError: If even the roomiest spacing leaves an edge unroutable.
            Not observed on any graph so far — the diagram is failed rather
            than drawn without the relationship, since a box-and-connector
            diagram missing a connector reads as "these tables are unrelated".
    """
    ladder = _spacing_ladder(len(edges))
    for spacing in ladder:
        place_result = place(nodes, edges, layout, spacing)
        try:
            return place_result, route(nodes, edges, place_result)
        except NoRouteError:
            continue
    raise DiagramError(
        f"could not lay out {len(edges)} relationships between "
        f"{len(nodes)} tables without dropping one"
    )
