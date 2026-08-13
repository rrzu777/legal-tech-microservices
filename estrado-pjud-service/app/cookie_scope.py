from collections.abc import Iterable


def flatten_cookie_name_values(
    records: Iterable[tuple[str, str | None]],
) -> dict[str, str]:
    """Flatten equivalent cookie scopes without choosing between values."""
    cookies: dict[str, str] = {}
    for name, value in records:
        if value is None or (name in cookies and cookies[name] != value):
            raise ValueError("ambiguous_cookie_scope")
        cookies[name] = value
    return cookies
