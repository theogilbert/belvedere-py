"""A 2D character canvas that table boxes and connector lines are drawn onto,
replacing the old single-pass, strictly-left-to-right text assembly. Regions
are tracked by character column during drawing and converted to byte offsets
(``DiagramRegion`` requires byte offsets, since box-drawing characters are
multi-byte in UTF-8) only once, when the canvas is flattened to text.
"""

from dataclasses import dataclass, field

from ..protocol import DiagramRegion

_LINE_CHARS: dict[frozenset[str], str] = {
    frozenset({"left", "right"}): "─",
    frozenset({"up", "down"}): "│",
    frozenset({"left"}): "─",
    frozenset({"right"}): "─",
    frozenset({"up"}): "│",
    frozenset({"down"}): "│",
    frozenset({"down", "right"}): "┌",
    frozenset({"down", "left"}): "┐",
    frozenset({"up", "right"}): "└",
    frozenset({"up", "left"}): "┘",
    frozenset({"up", "down", "right"}): "├",
    frozenset({"up", "down", "left"}): "┤",
    frozenset({"down", "left", "right"}): "┬",
    frozenset({"up", "left", "right"}): "┴",
    frozenset({"up", "down", "left", "right"}): "┼",
}


@dataclass
class _Segment:
    text: str
    path: list[str] | None = None
    """Path this span resolves to via explore.describe; None for unlabeled text."""
    kind: str | None = None
    """``"table"`` or ``"column"``; None for unlabeled text (``path`` is also None then)."""


_Line = list[_Segment]


@dataclass
class _CharRegion:
    row: int
    char_start: int
    char_end: int
    kind: str
    path: list[str]


@dataclass
class Canvas:
    _cells: dict[tuple[int, int], str] = field(default_factory=dict)
    _line_dirs: dict[tuple[int, int], set[str]] = field(default_factory=dict)
    _char_regions: list[_CharRegion] = field(default_factory=list)
    _labels: dict[tuple[int, int], str] = field(default_factory=dict)
    """Cardinality markers drawn at an edge's endpoints, keyed by canvas cell;
    override the line-direction glyph but never a box cell."""

    def blit_box(self, box_lines: list[_Line], top: int, left: int) -> None:
        """Writes a pre-rendered box's lines at ``(top, left)``, translating each
        segment's region to canvas-global character coordinates."""
        for r, line in enumerate(box_lines):
            col = left
            for seg in line:
                start = col
                for ch in seg.text:
                    self._cells[(top + r, col)] = ch
                    col += 1
                if seg.path is not None:
                    self._char_regions.append(
                        _CharRegion(
                            row=top + r,
                            char_start=start,
                            char_end=col,
                            kind=seg.kind or "table",
                            path=seg.path,
                        )
                    )

    def draw_edge(
        self,
        points: list[tuple[int, int]],
        *,
        start: str | None = None,
        end: str | None = None,
        path: list[str] | None = None,
    ) -> None:
        """Draws an orthogonal connector through ``points`` (row, col) — waypoints
        of a polyline, each consecutive pair sharing a row or column. Every unit
        cell along each straight run gets marked, not just the waypoints. Cells
        already occupied by a box are left untouched — boxes always win over
        routed lines. ``start``/``end`` optionally mark the first/last cell with
        a cardinality glyph instead of the usual line character. ``path``, if
        given, records an ``"edge"``-kind region per row the connector touches,
        all sharing that path."""
        cells = _expand(points)
        for i, point in enumerate(cells):
            if i > 0:
                self._add_direction(point, _direction_to(cells[i - 1], point))
            if i < len(cells) - 1:
                self._add_direction(point, _direction_to(cells[i + 1], point))
        if cells:
            if start is not None:
                self._set_label(cells[0], start)
            if end is not None:
                self._set_label(cells[-1], end)
        if path is not None:
            self._record_edge_regions(cells, path)

    def _set_label(self, point: tuple[int, int], char: str) -> None:
        if point in self._cells:
            return  # a box border already owns this cell
        self._labels[point] = char

    def _record_edge_regions(
        self, cells: list[tuple[int, int]], path: list[str]
    ) -> None:
        """Groups the connector's cells into one region per contiguous run
        within a row (skipping cells a box border already owns)."""
        run_row: int | None = None
        run_start = 0
        run_end = 0
        for row, col in cells:
            if (row, col) in self._cells:
                continue  # a box border already owns this cell
            if row != run_row:
                if run_row is not None:
                    self._char_regions.append(
                        _CharRegion(run_row, run_start, run_end, "edge", path)
                    )
                run_row, run_start = row, col
            run_end = col + 1
        if run_row is not None:
            self._char_regions.append(
                _CharRegion(run_row, run_start, run_end, "edge", path)
            )

    def _add_direction(self, point: tuple[int, int], direction: str) -> None:
        if point in self._cells:
            return  # a box border already owns this cell
        self._line_dirs.setdefault(point, set()).add(direction)

    def render(self) -> tuple[str, list[DiagramRegion]]:
        """Flattens the canvas to text and byte-offset regions."""
        if not self._cells and not self._line_dirs:
            return "", []
        max_row = max(r for r, _ in [*self._cells, *self._line_dirs])
        max_col = max(c for _, c in [*self._cells, *self._line_dirs])

        rows: list[str] = []
        for r in range(max_row + 1):
            chars = []
            for c in range(max_col + 1):
                if (r, c) in self._cells:
                    chars.append(self._cells[(r, c)])
                elif (r, c) in self._labels:
                    chars.append(self._labels[(r, c)])
                elif (r, c) in self._line_dirs:
                    chars.append(
                        _LINE_CHARS.get(frozenset(self._line_dirs[(r, c)]), " ")
                    )
                else:
                    chars.append(" ")
            rows.append("".join(chars).rstrip())

        regions = []
        for cr in self._char_regions:
            line = rows[cr.row] if cr.row < len(rows) else ""
            byte_start = len(line[: cr.char_start].encode())
            byte_end = len(line[: cr.char_end].encode())
            regions.append(
                DiagramRegion(
                    row=cr.row,
                    col_start=byte_start,
                    col_end=byte_end,
                    kind=cr.kind,
                    path=cr.path,
                )
            )
        return "\n".join(rows), regions


def _direction_to(target: tuple[int, int], origin: tuple[int, int]) -> str:
    dr, dc = target[0] - origin[0], target[1] - origin[1]
    if dr < 0:
        return "up"
    if dr > 0:
        return "down"
    return "left" if dc < 0 else "right"


def _expand(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Turns a polyline's waypoints into every unit cell along it."""
    if not points:
        return []
    cells = [points[0]]
    for (r0, c0), (r1, c1) in zip(points, points[1:]):
        if r0 == r1:
            step = 1 if c1 > c0 else -1
            cells.extend((r0, c) for c in range(c0 + step, c1 + step, step))
        else:
            step = 1 if r1 > r0 else -1
            cells.extend((r, c0) for r in range(r0 + step, r1 + step, step))
    return cells
