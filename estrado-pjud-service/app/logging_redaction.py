"""Redacción central de secretos configurados antes de escribir cada log."""

import logging
import re
from collections.abc import Iterable


REDACTED = "[REDACTED]"
_PJUD_DOWNLOAD_TOKEN_PATTERN = re.compile(
    r"([?&](?:dtaDoc|dtaCert|valorDoc|valorFile)=)(?:\\.|[^&\s\"'\\])+",
    re.IGNORECASE,
)


def _usable_secrets(values: Iterable[str]) -> tuple[str, ...]:
    # Más largo primero evita que secretos con un prefijo común dejen visible
    # el sufijo. Los vacíos no pueden entrar: ``text.replace("", ...)``
    # corrompería cada carácter del log.
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


class SecretRedactingFormatter(logging.Formatter):
    """Envuelve un formatter y redacta su salida final sin mutar el record.

    Redactar después de ``delegate.format`` cubre los tres caminos de logging:
    ``msg`` ya renderizado, interpolación diferida en ``args`` (incluidos
    objetos como ``httpx.URL``) y texto de excepciones. El ``LogRecord`` queda
    intacto para otros handlers y los formatos existentes —texto o JSON— se
    conservan.
    """

    def __init__(self, delegate: logging.Formatter, secrets: Iterable[str]):
        super().__init__()
        self._delegate = delegate
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = _usable_secrets(secrets)

    def format(self, record: logging.LogRecord) -> str:
        output = self._delegate.format(record)
        for secret in self._secrets:
            output = output.replace(secret, REDACTED)
        return _PJUD_DOWNLOAD_TOKEN_PATTERN.sub(rf"\1{REDACTED}", output)


def install_secret_redaction(
    handlers: Iterable[logging.Handler],
    secrets: Iterable[str],
) -> None:
    """Instala o actualiza la redacción en handlers sin apilar wrappers."""

    secret_values = tuple(secrets)
    for handler in handlers:
        formatter = handler.formatter or logging.Formatter()
        if isinstance(formatter, SecretRedactingFormatter):
            formatter.set_secrets(secret_values)
        else:
            handler.setFormatter(SecretRedactingFormatter(formatter, secret_values))
