import re

_INTERNAL_PATTERN = re.compile(r"https?://\S+|/ADIR_\w+\S*|\w+\.php")


def safe_error(e: Exception) -> str:
    """Return a user-safe error message, redacting internal URLs and paths."""
    msg = str(e)
    return _INTERNAL_PATTERN.sub("[redacted]", msg)
