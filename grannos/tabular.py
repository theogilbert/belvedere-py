from typing import Any

from .protocol import ReadResult


def flatten_docs(
    columns: list[str], rows: list[list[Any]], rows_total: int | None = None
) -> ReadResult:
    """Flatten rows where values may be nested dicts into a flat ReadResult.

    Dict values are recursively expanded with dot-notation column names.
    Columns absent in some rows are filled with None.
    rows_total defaults to len(rows) when omitted; pass it explicitly when the
    driver knows the full result set exceeds what was returned (e.g. ES hits).
    """
    if not rows:
        return ReadResult(
            columns=columns,
            rows=[],
            rows_total=rows_total if rows_total is not None else 0,
        )

    flat_rows: list[dict[str, Any]] = []
    for row in rows:
        flat: dict[str, Any] = {}
        for col, val in zip(columns, row):
            for k, v in _flatten(col, val):
                flat[k] = v
        flat_rows.append(flat)

    # We dont keep track of cols using single set to also preserve order, hence
    # the list.
    all_cols: list[str] = []
    seen: set[str] = set()
    for flat in flat_rows:
        for k in flat:
            if k not in seen:
                all_cols.append(k)
                seen.add(k)

    result_rows = [[_to_str(flat.get(col)) for col in all_cols] for flat in flat_rows]
    return ReadResult(
        columns=all_cols,
        rows=result_rows,
        rows_total=rows_total if rows_total is not None else len(result_rows),
    )


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return "{" + ", ".join(str(v) for v in value) + "}"
    return str(value)


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        pairs: list[tuple[str, Any]] = []
        for k, v in value.items():
            pairs.extend(_flatten(f"{prefix}.{k}", v))
        return pairs
    if isinstance(value, list) and any(isinstance(v, dict) for v in value):
        pairs = []
        for i, v in enumerate(value):
            pairs.extend(_flatten(f"{prefix}[{i}]", v))
        return pairs
    return [(prefix, value)]
