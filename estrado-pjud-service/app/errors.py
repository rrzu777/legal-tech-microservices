import re

_INTERNAL_PATTERN = re.compile(r"https?://\S+|/ADIR_\w+\S*|(?:causa|consulta|sesion|modal)\w*\.php")


def safe_error(e: Exception | str) -> str:
    """Return a user-safe error message, redacting internal URLs and paths.

    Acepta `str` ademas de `Exception` porque tiene dos llamadores con formas
    distintas: las rutas le pasan la excepcion cruda, y `blocked_error_message`
    le pasa un mensaje ya compuesto (`f"infra: {type(e).__name__}: {e}"`). La
    politica de redaccion tiene que ser UNA: el mismo `SessionError` con una URL
    de OJV adentro pasaba por aca yendo por la API y se guardaba crudo yendo por
    el worker.
    """
    return _INTERNAL_PATTERN.sub("[redacted]", str(e))
