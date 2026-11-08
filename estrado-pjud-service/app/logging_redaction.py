"""Redacción central de secretos configurados antes de escribir cada log."""

import logging
import os
import re
from collections.abc import Iterable, Mapping


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


#: Largo mínimo para que un valor del ENTORNO entre a la redacción.
#:
#: El escaneo ambiental de abajo es implícito: nadie lo declaró secreto en el
#: punto de instalación, así que redactar una cadena corta —o común— mancharía
#: logs que no tienen nada que ver. ``_usable_secrets`` solo descarta los
#: vacíos, porque para la lista EXPLÍCITA el llamador ya afirmó "esto es un
#: secreto" y ahí su palabra manda. Para lo ambiental hace falta este piso.
#:
#: 16 no es arbitrario: medido el 8-nov en el entorno del worker en producción,
#: los cinco valores miden 64, 64, 219, 46 y 71. El más corto es 46, así que
#: quedan 30 caracteres de margen, y los valores de test habituales
#: ("test-key", "synthetic", "relay") caen holgadamente afuera.
#:
#: Lo que esto NO hace: si alguien pone en producción un secreto más corto que
#: esto, el escaneo ambiental lo saltea en silencio. Es un fail-open conocido y
#: acotado — una API key de menos de 16 caracteres ya es un problema aparte—,
#: y `test_el_piso_ambiental_no_puede_subir_hasta_tapar_un_secreto_real` impide
#: que el umbral suba hasta donde empiece a saltear secretos reales.
AMBIENT_SECRET_MIN_LENGTH = 16


def ambient_secret_values(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Valores de ``SECRET_CONFIG_FIELDS`` tomados del entorno del proceso.

    Existe porque ``secret_values(config)`` solo alcanza a los llamadores que se
    pueden editar, y ``worker/__main__.py`` NO se puede: es uno de los 17
    archivos de ``wm_contract_paths`` que ``ops/deploy.sh`` congela byte a byte
    (ver ops/worker-maintenance.md). Ese worker arma su lista de secretos a mano
    y por lo tanto no incluye API_KEY ni INTERNAL_CREDENTIALS_API_KEY — y es
    justamente el proceso que usa la segunda para pedir la contraseña PJUD
    descifrada.

    Los dos units cargan ``EnvironmentFile=.../.env``, así que systemd pone esos
    valores en el entorno del proceso; verificado el 8-nov contra
    ``/proc/<MainPID>/environ`` de la API y del worker, los cinco presentes y no
    vacíos. Sin eso esto sería una guarda inerte.

    La raíz que arregla: "qué cuenta como secreto" es del módulo de redacción,
    no de cada punto de instalación. Que cada llamador armara su lista es
    exactamente cómo se perdieron las dos llaves.
    """
    env = os.environ if environ is None else environ
    values: list[str] = []
    for name in SECRET_CONFIG_FIELDS:
        value = env.get(name) or ""
        if len(value) >= AMBIENT_SECRET_MIN_LENGTH:
            values.append(value)
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
    """Instala o actualiza la redacción en handlers sin apilar wrappers.

    A lo que declara el llamador se le SUMA ``ambient_secret_values()``, para
    que un proceso cuya lista no se puede editar —el worker, congelado por el
    gate de contrato— quede cubierto igual. Las dos fuentes se unen; ninguna
    reemplaza a la otra: ``secrets`` sigue siendo el canal para valores que no
    son campos de config, como la capability del import de prueba.

    El local se llama `all_secrets` y no `secret_values` a propósito: ese
    nombre es de la función de arriba y sombrearlo acá lee como si fuera ella.
    """

    all_secrets = tuple(secrets) + ambient_secret_values()
    for handler in handlers:
        formatter = handler.formatter or logging.Formatter()
        if isinstance(formatter, SecretRedactingFormatter):
            formatter.set_secrets(all_secrets)
        else:
            handler.setFormatter(SecretRedactingFormatter(formatter, all_secrets))
