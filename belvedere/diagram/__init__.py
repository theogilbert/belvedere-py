"""Renders a table and all tables connected to it (recursively via foreign keys)
as an ASCII graph diagram.

Every reachable table is discovered once (see ``graph.py``) and drawn as
exactly one box — never as floating text, never twice. Boxes are placed with
a layered graph layout (see ``layout.py``): ranked by BFS distance from the
source table, ordered within each rank to reduce line crossings (best-effort),
and wrapped into vertically-stacked bands once a row would grow past a handful
of columns wide. Relationships are drawn as routed orthogonal connector lines
between box borders (see ``canvas.py`` and ``render.py``), not as text.

Every table and column name drawn in the diagram is also tracked as a
:class:`~belvedere.protocol.DiagramRegion`, so a client can map a cursor
position back to an ``explore.describe`` path.
"""

from dataclasses import dataclass

from ..protocol import DiagramRegion
from .graph import Describe, DiagramError, discover
from .layout import compute_layout
from .render import render

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
    diagram, regions = render(nodes, layout)
    return DiagramResult(diagram=diagram, regions=regions)
