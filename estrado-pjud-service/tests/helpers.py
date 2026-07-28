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
