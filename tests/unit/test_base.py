"""Unit tests for shared driver helpers in grannos.drivers.base."""

from grannos.drivers.base import build_column_samples


class TestBuildColumnSamples:
    def test_dedupes_repeated_values(self) -> None:
        rows = [("x",), ("y",), ("x",)]
        assert build_column_samples(["VAL"], rows, 3) == {"VAL": ["x", "y"]}

    def test_skips_nulls(self) -> None:
        rows = [(None,), ("a",), (None,)]
        assert build_column_samples(["VAL"], rows, 3) == {"VAL": ["a"]}

    def test_caps_at_n_values(self) -> None:
        rows = [("a",), ("b",), ("c",), ("d",)]
        assert build_column_samples(["VAL"], rows, 2) == {"VAL": ["a", "b"]}

    def test_skips_unserialisable_types(self) -> None:
        rows = [(b"\x00",), ("ok",)]
        assert build_column_samples(["VAL"], rows, 3) == {"VAL": ["ok"]}

    def test_all_null_column_yields_empty_list(self) -> None:
        rows = [(1, None), (2, None)]
        result = build_column_samples(["ID", "VAL"], rows, 3)
        assert result == {"ID": [1, 2], "VAL": []}

    def test_no_rows_yields_empty_lists(self) -> None:
        assert build_column_samples(["ID", "VAL"], [], 3) == {"ID": [], "VAL": []}
