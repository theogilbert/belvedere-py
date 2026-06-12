from typing import Any

from .protocol import SelectResult


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        pairs: list[tuple[str, Any]] = []
        for k, v in value.items():
            pairs.extend(_flatten(f"{prefix}.{k}", v))
        return pairs
    return [(prefix, value)]


def flatten_docs(columns: list[str], rows: list[list[Any]]) -> SelectResult:
    """Flatten rows where values may be nested dicts into a flat SelectResult.

    Dict values are recursively expanded with dot-notation column names.
    Columns absent in some rows are filled with None.
    """
    if not rows:
        return SelectResult(columns=columns, rows=[])

    flat_rows: list[dict[str, Any]] = []
    for row in rows:
        flat: dict[str, Any] = {}
        for col, val in zip(columns, row):
            for k, v in _flatten(col, val):
                flat[k] = v
        flat_rows.append(flat)

    all_cols: list[str] = []
    seen: set[str] = set()
    for flat in flat_rows:
        for k in flat:
            if k not in seen:
                all_cols.append(k)
                seen.add(k)

    result_rows = [[flat.get(col) for col in all_cols] for flat in flat_rows]
    return SelectResult(columns=all_cols, rows=result_rows)
