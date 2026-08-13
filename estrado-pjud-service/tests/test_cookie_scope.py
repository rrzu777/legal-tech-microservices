import pytest

from app.cookie_scope import flatten_cookie_name_values


@pytest.mark.parametrize(
    "records",
    [
        [("PHPSESSID", None)],
        [("PHPSESSID", None), ("PHPSESSID", "session")],
        [("PHPSESSID", "session"), ("PHPSESSID", None)],
    ],
)
def test_flatten_cookie_name_values_rejects_valueless_cookie(records):
    """A valueless httpx cookie cannot cross the string-only store boundary."""
    with pytest.raises(ValueError, match="^ambiguous_cookie_scope$"):
        flatten_cookie_name_values(records)


def test_flatten_cookie_name_values_accepts_empty_string_value():
    """An explicit empty value is a string and remains distinct from no value."""
    assert flatten_cookie_name_values([
        ("flag", ""),
        ("flag", ""),
    ]) == {"flag": ""}
