from belvedere.protocol import SelectResult
from belvedere.tabular import flatten_docs


def test_empty_rows_preserves_columns() -> None:
    result = flatten_docs(["a", "b"], [])
    assert result == SelectResult(columns=["a", "b"], rows=[], rows_total=0)


def test_scalar_values_stringified() -> None:
    result = flatten_docs(["x", "y"], [[1, "hello"]])
    assert result == SelectResult(
        columns=["x", "y"], rows=[["1", "hello"]], rows_total=1
    )


def test_top_level_dict_flattened() -> None:
    result = flatten_docs(["doc"], [[{"name": "Alice", "age": 30}]])
    assert result.columns == ["doc.name", "doc.age"]
    assert result.rows == [["Alice", "30"]]


def test_nested_dict_uses_dot_notation() -> None:
    result = flatten_docs(["doc"], [[{"foo": {"bar": 1}}]])
    assert result.columns == ["doc.foo.bar"]
    assert result.rows == [["1"]]


def test_deeply_nested_dict() -> None:
    result = flatten_docs(["d"], [[{"a": {"b": {"c": 42}}}]])
    assert result.columns == ["d.a.b.c"]
    assert result.rows == [["42"]]


def test_missing_key_filled_with_none() -> None:
    rows = [
        [{"name": "Alice", "age": 30}],
        [{"name": "Bob"}],
    ]
    result = flatten_docs(["doc"], rows)
    assert result.columns == ["doc.name", "doc.age"]
    assert result.rows == [["Alice", "30"], ["Bob", None]]


def test_extra_key_in_later_row() -> None:
    rows = [
        [{"name": "Alice"}],
        [{"name": "Bob", "email": "b@b.com"}],
    ]
    result = flatten_docs(["doc"], rows)
    assert result.columns == ["doc.name", "doc.email"]
    assert result.rows == [["Alice", None], ["Bob", "b@b.com"]]


def test_mixed_scalar_and_dict_columns() -> None:
    rows = [[1, {"x": 10, "y": 20}]]
    result = flatten_docs(["id", "pos"], rows)
    assert result.columns == ["id", "pos.x", "pos.y"]
    assert result.rows == [["1", "10", "20"]]


def test_list_value_formatted_as_set_notation() -> None:
    result = flatten_docs(["doc"], [[{"tags": ["a", "b"]}]])
    assert result.columns == ["doc.tags"]
    assert result.rows == [["{a, b}"]]


def test_list_with_single_item() -> None:
    result = flatten_docs(["doc"], [[{"labels": ["Person"]}]])
    assert result.rows == [["{Person}"]]


def test_list_with_multiple_items() -> None:
    result = flatten_docs(["doc"], [[{"labels": ["Person", "Employee"]}]])
    assert result.rows == [["{Person, Employee}"]]


def test_list_of_dicts_flattened_with_index() -> None:
    rows = [
        [{"items": [{"productId": "abc", "qty": 2}, {"productId": "def", "qty": 1}]}]
    ]
    result = flatten_docs(["doc"], rows)
    assert result.columns == [
        "doc.items[0].productId",
        "doc.items[0].qty",
        "doc.items[1].productId",
        "doc.items[1].qty",
    ]
    assert result.rows == [["abc", "2", "def", "1"]]


def test_list_of_dicts_variable_length_filled_with_none() -> None:
    rows = [
        [[{"qty": 2}]],
        [[{"qty": 1}, {"qty": 3}]],
    ]
    result = flatten_docs(["items"], rows)
    assert result.columns == ["items[0].qty", "items[1].qty"]
    assert result.rows == [["2", None], ["1", "3"]]


def test_column_order_follows_first_appearance() -> None:
    rows = [
        [{"b": 2, "a": 1}],
        [{"c": 3, "a": 4}],
    ]
    result = flatten_docs(["doc"], rows)
    assert result.columns == ["doc.b", "doc.a", "doc.c"]
    assert result.rows == [["2", "1", None], [None, "4", "3"]]


def test_multiple_rows_multiple_columns() -> None:
    rows = [
        [1, {"city": "Paris", "country": "FR"}],
        [2, {"city": "Berlin", "country": "DE"}],
    ]
    result = flatten_docs(["id", "loc"], rows)
    assert result.columns == ["id", "loc.city", "loc.country"]
    assert result.rows == [["1", "Paris", "FR"], ["2", "Berlin", "DE"]]
