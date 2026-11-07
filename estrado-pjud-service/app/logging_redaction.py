"""Redacción central de secretos configurados antes de escribir cada log."""

import logging
import re
from collections.abc import Iterable


REDACTED = "[REDACTED]"
_PJUD_DOWNLOAD_TOKEN_PATTERN = re.compile(
    r"([?&](?:dtaDoc|dtaCert|valorDoc|valorFile)=)(?:\\.|[^&\s\"'\\])+",
    re.IGNORECASE,
)


#: Campos de config cuyo VALOR nunca puede llegar al journal. FUENTE ÚNICA: la
#: API y el worker leían esta membresía de dos tuplas escritas a mano, y así
#: fue como API_KEY e INTERNAL_CREDENTIALS_API_KEY quedaron afuera de las dos
#: durante meses. Ninguna de las dos listas se comparaba con la otra, y la
#: cobertura de tests tocaba solo una, así que la del worker podía perder una
#: llave con la suite verde.
#:
#: `getattr` absorbe la asimetría real entre las dos configs: WorkerConfig no
#: declara API_KEY. Agregar un nombre acá lo cubre en los dos procesos.
SECRET_CONFIG_FIELDS: tuple[str, ...] = (
    "API_KEY",
    "INTERNAL_CREDENTIALS_API_KEY",
    "SUPABASE_SERVICE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "OJV_PROXY_URL",
)


def secret_values(config: object, *extra: str | None) -> tuple[str, ...]:
    """Valores a redactar de una config, más los que el llamador agregue.

    Desenvuelve ``SecretStr`` por duck-typing y no importando pydantic: si
    alguien pasa un campo a ``SecretStr`` sin tocar esto, lo que se redactaría
    es la máscara ``'**********'`` y el secreto real seguiría saliendo en claro
    — un fallo que se ve idéntico a funcionar.

    ``extra`` existe para valores que no son atributos de la config, como la
    capability del import de prueba, que el worker ya desenvuelve por su cuenta.
    """
    values: list[str] = []
    for name in SECRET_CONFIG_FIELDS:
        value = getattr(config, name, None)
        reveal = getattr(value, "get_secret_value", None)
        if callable(reveal):
            value = reveal()
        if isinstance(value, str) and value:
            values.append(value)
    values.extend(value for value in extra if value)
    return tuple(values)


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
