"""Utilidades compartidas entre módulos de test."""


def find_update_payload(mock_sb, **match):
    """Devuelve el primer payload de .update() que matchea todos los pares clave/valor.

    Los tests inspeccionan el dict que el engine manda a Supabase; el mock encadena
    from_().update(), así que hay que barrer call_args_list y filtrar por contenido.
    """
    for call in mock_sb.from_.return_value.update.call_args_list:
        args = call[0] if call[0] else ()
        kwargs = call[1] if call[1] else {}
        payload = args[0] if args else kwargs.get("data")
        if payload and all(payload.get(k) == v for k, v in match.items()):
            return payload
    return None


def http_status_error(code: int) -> "httpx.HTTPStatusError":
    """Un `HTTPStatusError` de OJV con el status pedido.

    Lo arman cinco tests entre `test_failure_kind`, `test_engine` y
    `test_routes`, con tres argumentos que ninguno de ellos quiere describir. En
    la PR que convierte el status HTTP en el eje de la clasificacion, es el
    fixture que mas se va a volver a necesitar.
    """
    import httpx

    return httpx.HTTPStatusError(
        str(code),
        request=httpx.Request("POST", "https://ojv.test"),
        response=httpx.Response(code),
    )


def infra_exceptions() -> list:
    """Las excepciones que significan "se cayo NUESTRO lado".

    Una sola lista: estaba copiada en tres parametrize, asi que agregar un tipo
    al clasificador exigia acordarse de tres lugares — y el que se olvidara no
    fallaba, simplemente dejaba de estar testeado. Los tres ultimos son los que
    la lista vieja de las rutas dejaba afuera, y son justo lo que produce un
    pool de IPs residenciales.
    """
    import httpx

    return [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("refused"),
        httpx.ProxyError("proxy down"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ]
