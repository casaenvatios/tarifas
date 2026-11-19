"""Comprueba cada oferta de reglas.json contra la web de la compañía.

Se ejecuta una vez por semana (.github/workflows/verificar.yml). Para cada oferta
abre su web ("fuente"), saca todos los números de la página y mira si aparecen
sus precios: energía (punta, llano, valle) y potencia (P1 y P2). La potencia se
busca en €/kW y día, en €/kW y año y en €/kW y mes; todo, sin impuestos y con
ellos. La energía también en céntimos.

Resultado por oferta, en verificaciones.json:
  - "coincide": todos sus precios aparecen en la web.
  - "no_coincide": la web enseña precios, pero alguno de los nuestros no está.
  - "sin_datos": no se ha podido leer la página o no enseña precios.

Se guardan los precios comprobados: si la CNMC los cambia después, la
comprobación ya no vale para esa oferta hasta la semana siguiente.

Uso: python verificar_webs.py [--reglas reglas.json] [--salida verificaciones.json]
Si está instalado Playwright (en GitHub Actions sí), las páginas que necesitan
JavaScript se abren con un navegador; si no, solo con una descarga normal.
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CABECERAS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
    'Accept-Language': 'es-ES,es;q=0.9',
}
CAMPOS_ENERGIA = ('punta', 'llano', 'valle')
CAMPOS_POTENCIA = ('potencia_p1', 'potencia_p2')


def descargar(url, timeout=25):
    req = urllib.request.Request(url, headers=CABECERAS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        datos = r.read(4_000_000)
        juego = r.headers.get_content_charset() or 'utf-8'
    return datos.decode(juego, errors='replace')


def texto_de_html(h):
    h = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', h)
    h = re.sub(r'(?s)<[^>]+>', ' ', h)
    return html.unescape(h)


def numeros(texto):
    """Todos los números decimales del texto (formato español o inglés)."""
    out = []
    for m in re.finditer(r'(?<![\d.,])\d{1,4}(?:[.,]\d{3})*[.,]\d+|(?<![\d.,])\d+(?![\d.,])', texto):
        s = m.group(0)
        if ',' in s and '.' in s:
            s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
        elif ',' in s:
            s = s.replace(',', '.')
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def enseña_precios(nums):
    """La página tiene algo con pinta de precio de luz (€/kWh entre 0,02 y 0,6)."""
    return sum(1 for n in nums if 0.02 <= n <= 0.6) >= 2


def aparece(valor, nums, tipo, impuestos):
    """Si [valor] aparece en la página en alguna de sus formas habituales."""
    if valor is None or valor <= 0:
        return True  # no hay nada que comprobar
    candidatos = []
    for factor in (1.0, impuestos):
        v = valor * factor
        if tipo == 'energia':
            # €/kWh con 3 decimales (redondeado o cortado: hasta 0,0011 de diferencia), o en céntimos.
            candidatos += [(v, 0.0011), (v * 100, 0.11)]
        else:
            candidatos += [(v, max(0.00051, v * 0.003)), (v * 365, v * 365 * 0.003 + 0.0051),
                           (v * 365 / 12, v * 365 / 12 * 0.003 + 0.0051)]
    return any(abs(n - c) <= tol for c, tol in candidatos for n in nums)


def leer_pagina(url, navegador):
    try:
        nums = numeros(texto_de_html(descargar(url)))
    except Exception:  # noqa: BLE001
        nums = []
    if enseña_precios(nums) or navegador is None:
        return nums
    # Puede que la página pinte los precios con JavaScript: se abre con el navegador.
    try:
        pagina = navegador.new_page()
        pagina.goto(url, timeout=45000, wait_until='networkidle')
        texto = pagina.inner_text('body')
        pagina.close()
        return numeros(texto)
    except Exception:  # noqa: BLE001
        return nums


def comprobar(oferta, nums, impuestos):
    precios = {k: oferta.get(k) for k in CAMPOS_ENERGIA + CAMPOS_POTENCIA}
    if not enseña_precios(nums):
        return {'estado': 'sin_datos', 'precios': precios}
    faltan = []
    vistos = set()
    for k in CAMPOS_ENERGIA:
        v = oferta.get(k)
        if v is None or round(v, 6) in vistos:
            continue
        vistos.add(round(v, 6))
        if not aparece(v, nums, 'energia', impuestos):
            faltan.append(k)
    for k in CAMPOS_POTENCIA:
        if not aparece(oferta.get(k), nums, 'potencia', impuestos):
            faltan.append(k)
    return {'estado': 'no_coincide' if faltan else 'coincide', 'faltan': faltan, 'precios': precios}


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--reglas', default='reglas.json')
    a.add_argument('--salida', default='verificaciones.json')
    args = a.parse_args()
    reglas = json.load(open(args.reglas, encoding='utf-8'))
    fiscal = reglas['fiscal']
    impuestos = (1 + fiscal['impuesto_electrico']) * (1 + fiscal['iva'])
    hoy = dt.date.today().isoformat()
    # Se comprueban también las apartadas por dudosas: si su web ya coincide, vuelven.
    ofertas = [o for o in reglas['ofertas'] + reglas.get('ofertas_dudosas', [])
               if o.get('fuente', '').startswith('http') and 'cnmc.gob.es' not in o.get('fuente', '')]

    urls = sorted({o['fuente'] for o in ofertas})
    with ThreadPoolExecutor(max_workers=8) as ex:
        paginas = dict(zip(urls, ex.map(lambda u: leer_pagina(u, None), urls)))

    # Las que no enseñan precios en una descarga normal, otra vez con un navegador (si lo hay).
    pendientes = [u for u, n in paginas.items() if not enseña_precios(n)]
    if pendientes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None
        if sync_playwright:
            with sync_playwright() as p:
                nav = p.chromium.launch()
                for u in pendientes:
                    paginas[u] = leer_pagina(u, nav)
                nav.close()

    resultado = {}
    for o in ofertas:
        r = comprobar(o, paginas.get(o['fuente'], []), impuestos)
        r['fecha'] = hoy
        r['url'] = o['fuente']
        resultado[o['id']] = r

    cuenta = {}
    for r in resultado.values():
        cuenta[r['estado']] = cuenta.get(r['estado'], 0) + 1
    with open(args.salida, 'w', encoding='utf-8') as f:
        json.dump({'fecha': hoy, 'ofertas': resultado}, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print(f'{args.salida}: {len(resultado)} ofertas comprobadas en su web: {cuenta}')
    for k, r in sorted(resultado.items()):
        if r['estado'] == 'no_coincide':
            print(f'  No coincide: {k} (faltan {", ".join(r["faltan"])}) {r["url"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
