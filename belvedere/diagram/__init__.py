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
:class:`~belvedere.protocol.DiagramRegion`, so a client can map a cursor
position back to an ``explore.describe`` path.
"""

from dataclasses import dataclass

from ..protocol import DiagramRegion
from .canvas import Canvas
from .graph import Describe, DiagramError, discover
from .layout import compute_layout
from .place import place
from .route import compact, route

__all__ = ["DiagramError", "DiagramResult", "build_diagram"]


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
        DiagramError: If path does not resolve to a table.
    """
    nodes, edges = await discover(path, describe)
    layout = compute_layout(nodes, edges)
    place_result = place(nodes, edges, layout)
    routed_edges = route(nodes, edges, place_result)
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
