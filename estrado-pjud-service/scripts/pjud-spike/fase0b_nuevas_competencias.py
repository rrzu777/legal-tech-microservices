#!/usr/bin/env python3
"""
Spike de viabilidad para nuevas competencias PJUD:
- Suprema
- Apelaciones (con filtro de corte)
- Penal

Objetivo: Obtener fixtures HTML reales de cada competencia para analizar diferencias estructurales.
"""

import httpx
import re
from pathlib import Path
from datetime import datetime

# Configuración
OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

# Casos de prueba conocidos (ajustar según sea necesario)
TEST_CASES = {
    "suprema": {
        "search_params": {
            "competencia": "suprema",
            "case_type": "rol",
            "case_number": "C-100-2025"  # Ajustar a un caso real existente
        },
        "search_endpoint": "/ADIR_871/suprema/consultaRitSuprema.php",
        "detail_endpoint": "/ADIR_871/suprema/modal/causaSuprema.php",
    },
    "apelaciones": {
        "search_params": {
            "competencia": "apelaciones",
            "case_type": "rol",
            "case_number": "Proteccion-4490-2025",
            "corte": 91  # C.A. de San Miguel
        },
        "search_endpoint": "/ADIR_871/apelaciones/consultaRitApelaciones.php",
        "detail_endpoint": "/ADIR_871/apelaciones/modal/causaApelaciones.php",
    },
    "penal": {
        "search_params": {
            "competencia": "penal",
            "case_type": "rit",
            "case_number": "T-500-2024"  # Ajustar a un caso real existente
        },
        "search_endpoint": "/ADIR_871/penal/consultaRitPenal.php",
        "detail_endpoint": "/ADIR_871/penal/modal/causaPenal.php",
    },
}


def save_response_html(response: httpx.Response, filename: str):
    """Guarda el HTML de una respuesta HTTP."""
    filepath = FIXTURES_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(response.text)
    print(f"✓ Guardado: {filename}")
    return filepath


def save_response_headers(response: httpx.Response, filename: str):
    """Guarda los headers de una respuesta HTTP."""
    filepath = FIXTURES_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"Status Code: {response.status_code}\n")
        f.write(f"Encoding: {response.encoding}\n")
        f.write(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")
        f.write(f"Set-Cookie: {response.headers.get('set-cookie', 'N/A')}\n")
        f.write(f"\nAll Headers:\n")
        for key, value in response.headers.items():
            f.write(f"{key}: {value}\n")
    print(f"✓ Guardado: {filename}")
    return filepath


def extract_detail_key(html: str, pattern: str) -> str | None:
    """Extrae la key de detalle del HTML de búsqueda."""
    match = re.search(pattern, html)
    if match:
        return match.group(1)
    return None


def detect_captcha_fields(html: str) -> list[str]:
    """Detecta campos de captcha en el HTML."""
    captcha_fields = []
    if 'recaptcha' in html.lower():
        captcha_fields.append('recaptcha')
    if 'captcha' in html.lower():
        captcha_fields.append('captcha')
    return captcha_fields


def analyze_html_structure(html: str, filename: str) -> dict:
    """Analiza la estructura del HTML para detectar campos únicos."""
    analysis = {
        'filename': filename,
        'encoding': 'utf-8',  # Asumimos utf-8 por defecto
        'captcha_fields': detect_captcha_fields(html),
        'unique_fields': [],
        'has_movements': 'movimiento' in html.lower() or 'actuacion' in html.lower(),
        'has_litigantes': 'litigante' in html.lower() or 'interviniente' in html.lower(),
    }
    
    # Detectar campos específicos por competencia
    if 'suprema' in filename.lower():
        if 'sala' in html.lower():
            analysis['unique_fields'].append('Sala')
        if 'relator' in html.lower():
            analysis['unique_fields'].append('Relator')
        if 'ministro' in html.lower():
            analysis['unique_fields'].append('Ministros')
    
    if 'apelaciones' in filename.lower():
        if 'recurso' in html.lower():
            analysis['unique_fields'].append('Recurso')
        if 'estado recurso' in html.lower():
            analysis['unique_fields'].append('Estado Recurso')
        if 'ubicacion' in html.lower():
            analysis['unique_fields'].append('Ubicacion')
    
    if 'penal' in filename.lower():
        if 'audiencia' in html.lower():
            analysis['unique_fields'].append('Audiencias')
        if 'interviniente' in html.lower():
            analysis['unique_fields'].append('Intervinientes')
        if 'rit' in html.lower() or 'ruc' in html.lower():
            analysis['unique_fields'].append('RIT/RUC')
    
    return analysis


async def spike_competencia(name: str, config: dict, client: httpx.AsyncClient):
    """Ejecuta el spike para una competencia específica."""
    print(f"\n{'='*60}")
    print(f"SPIKE: {name.upper()}")
    print(f"{'='*60}")
    
    results = {
        'name': name,
        'search_success': False,
        'detail_success': False,
        'analysis': None,
        'errors': [],
    }
    
    try:
        # 1. Búsqueda
        print(f"\n1. Ejecutando búsqueda: {config['search_params']}")
        search_response = await client.post(
            f"{OJV_BASE}{config['search_endpoint']}",
            data=config['search_params'],
            timeout=30.0,
        )
        
        # Guardar HTML y headers
        save_response_html(search_response, f"{name}_search.html")
        save_response_headers(search_response, f"{name}_search_headers.txt")
        
        # Analizar estructura
        search_analysis = analyze_html_structure(search_response.text, f"{name}_search.html")
        
        # Extraer detail key
        detail_fn_pattern = config.get('detail_fn_pattern', r"detalleCausa\w+\('([^']+)'\)")
        detail_key = extract_detail_key(search_response.text, detail_fn_pattern)
        
        if detail_key:
            print(f"✓ Detail key encontrada: {detail_key}")
            results['search_success'] = True
        else:
            print(f"✗ No se encontró detail key con el pattern: {detail_fn_pattern}")
            results['errors'].append(f"No se encontró detail key en búsqueda")
        
        # 2. Detalle (si encontramos la key)
        if detail_key:
            print(f"\n2. Ejecutando detalle: {detail_key}")
            detail_response = await client.get(
                f"{OJV_BASE}{config['detail_endpoint']}?key={detail_key}",
                timeout=30.0,
            )
            
            # Guardar HTML y headers
            save_response_html(detail_response, f"{name}_detail.html")
            save_response_headers(detail_response, f"{name}_detail_headers.txt")
            
            # Analizar estructura
            detail_analysis = analyze_html_structure(detail_response.text, f"{name}_detail.html")
            results['detail_success'] = True
            
            # Combinar análisis
            results['analysis'] = {
                'search': search_analysis,
                'detail': detail_analysis,
            }
        else:
            results['analysis'] = {'search': search_analysis, 'detail': None}
        
    except Exception as e:
        print(f"✗ Error: {e}")
        results['errors'].append(str(e))
    
    return results


def generate_diferencias_md(results: list[dict]):
    """Genera el archivo DIFERENCIAS.md con el análisis de todas las competencias."""
    filepath = FIXTURES_DIR / "DIFERENCIAS.md"
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("# Diferencias entre Competencias PJUD\n\n")
        f.write(f"Generado: {datetime.now().isoformat()}\n\n")
        f.write("---\n\n")
        
        # Resumen por competencia
        f.write("## Resumen por Competencia\n\n")
        for result in results:
            f.write(f"### {result['name'].upper()}\n\n")
            f.write(f"- Búsqueda: {'✓' if result['search_success'] else '✗'}\n")
            f.write(f"- Detalle: {'✓' if result['detail_success'] else '✗'}\n")
            
            if result['errors']:
                f.write(f"- Errores: {', '.join(result['errors'])}\n")
            
            if result['analysis']:
                search = result['analysis'].get('search', {})
                detail = result['analysis'].get('detail', {})
                
                if search.get('captcha_fields'):
                    f.write(f"- Captcha: {', '.join(search['captcha_fields'])}\n")
                
                if search.get('unique_fields'):
                    f.write(f"- Campos únicos (búsqueda): {', '.join(search['unique_fields'])}\n")
                
                if detail and detail.get('unique_fields'):
                    f.write(f"- Campos únicos (detalle): {', '.join(detail['unique_fields'])}\n")
            
            f.write("\n")
        
        # Tabla comparativa
        f.write("---\n\n")
        f.write("## Tabla Comparativa\n\n")
        f.write("| Competencia | Captcha | Campos Únicos | Movimientos | Litigantes/Intervinientes |\n")
        f.write("|-------------|---------|---------------|-------------|----------------------------|\n")
        
        for result in results:
            name = result['name'].capitalize()
            analysis = result.get('analysis', {})
            search = analysis.get('search', {}) if analysis else {}
            detail = analysis.get('detail', {}) if analysis else {}
            
            captcha = ', '.join(search.get('captcha_fields', [])) or 'No'
            detail_fields = detail.get('unique_fields', []) if detail else []
            unique_fields = ', '.join(search.get('unique_fields', []) + detail_fields) or 'No'
            has_movements = 'Sí' if search.get('has_movements') or (detail and detail.get('has_movements')) else 'No'
            has_litigantes = 'Sí' if search.get('has_litigantes') or (detail and detail.get('has_litigantes')) else 'No'
            
            f.write(f"| {name} | {captcha} | {unique_fields} | {has_movements} | {has_litigantes} |\n")
        
        # Recomendaciones
        f.write("\n---\n\n")
        f.write("## Recomendaciones\n\n")
        
        f.write("### Para la implementación:\n\n")
        f.write("1. **Apelaciones**: Requiere campo adicional `corte` (código de corte). Validar contra lista de 17 cortes.\n\n")
        f.write("2. **Penal**: Usa RIT/RUC en vez de ROL. Verificar estructura de movimientos (audiencias vs resoluciones).\n\n")
        f.write("3. **Suprema**: Verificar campos adicionales (Sala, Relator, Ministros).\n\n")
        
        f.write("### Riesgos detectados:\n\n")
        for result in results:
            if result['errors']:
                f.write(f"- **{result['name'].capitalize()}**: {', '.join(result['errors'])}\n")
        
        f.write("\n### Próximos pasos:\n\n")
        f.write("1. Revisar fixtures HTML generados\n")
        f.write("2. Implementar Apelaciones primero (caso más complejo con filtro de corte)\n")
        f.write("3. Implementar Suprema y Penal (copiar patrón)\n")
        f.write("4. Agregar tests unitarios con los fixtures\n")
        f.write("5. Verificar worker de sync (debería fluir automático)\n")
    
    print(f"\n✓ DIFERENCIAS.md generado: {filepath}")
    return filepath


async def main():
    """Ejecuta el spike completo."""
    print("="*60)
    print("SPIKE: NUEVAS COMPETENCIAS PJUD")
    print("="*60)
    print(f"Fixtures directory: {FIXTURES_DIR}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    
    results = []
    
    async with httpx.AsyncClient(
        base_url=OJV_BASE,
        timeout=30.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "es-CL,es;q=0.9",
        }
    ) as client:
        for name, config in TEST_CASES.items():
            result = await spike_competencia(name, config, client)
            results.append(result)
    
    # Generar DIFERENCIAS.md
    generate_diferencias_md(results)
    
    # Resumen final
    print("\n" + "="*60)
    print("RESUMEN FINAL")
    print("="*60)
    
    for result in results:
        status = "✓" if (result['search_success'] and result['detail_success']) else "✗"
        print(f"{status} {result['name'].capitalize()}: {len(result['errors'])} errores")
        if result['errors']:
            for error in result['errors']:
                print(f"   - {error}")
    
    print(f"\nFixtures guardados en: {FIXTURES_DIR}")
    print(f"Listar: ls -la {FIXTURES_DIR}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
