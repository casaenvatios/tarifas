"""Genera reglas.json para Casa en Vatios.

Junta dos fuentes:
  1. ofertas-manuales.json: reglas fiscales y ofertas comprobadas a mano en la web
     de cada comercializadora (las que no están en la CNMC o tienen datos extra).
  2. La API pública del Comparador de Ofertas de Energía de la CNMC
     (https://comparador.cnmc.gob.es/api/swagger-ui/index.html). Licencia CC BY-SA 4.0:
     "información recabada y gestionada por la CNMC en el Comparador de Ofertas de Energía".

De la CNMC se toman solo las ofertas de electricidad para hogares (peaje 2.0TD) con
precio fijo por tramo. Si la API falla, no se toca reglas.json (sale con error).

Uso: python actualizar_ofertas.py [--fecha AAAA-MM-DD] [--salida reglas.json] [--cnmc ofertas.json]
"""
import argparse
import datetime as dt
import json
import re
import socket
import ssl
import sys
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = 'https://comparador.cnmc.gob.es/api/APIPublica/ofertas?fecha={fecha}'

# La financiación del bono social que la CNMC incluye en el "precio fijo" mensual de
# cada oferta. La app ya la suma aparte, así que se descuenta de la cuota.
BONO_SOCIAL_MES = 0.024688479 * 365 / 12

COMPANIAS = {
    'repsol.es': 'Repsol', 'chcenergia.es': 'CHC Energía', 'energianufri.com': 'Energía Nufri',
    'energyavm.es': 'AVM Energía', 'hscomercializadora.com': 'HS Comercializadora', 'inerenergia.es': 'Iner Energía',
    'naturgy.es': 'Naturgy', 'contigoenergia.com': 'Contigo Energía', 'octopusenergy.es': 'Octopus Energy',
    'lumisa.es': 'Lumisa', 'visalia.es': 'Visalia', 'eniplenitude.es': 'Plenitude', 'imaginaenergia.com': 'Imagina Energía',
    'elekluz.com': 'Elekluz', 'wekiwi.es': 'Wekiwi', 'agrienergia.com': 'Agri Energía', 'logosenergia.es': 'Logos Energía',
    'electricaguadassuar.es': 'Eléctrica de Guadassuar', 'xerramcomtu.es': 'Xerramcomtu', 'eleiaenergia.com': 'Eleia Energía',
    'energyasset.es': 'Energy Asset', 'maisenerxia.com': 'Máis Enerxía', 'contratar.maisenerxia.com': 'Máis Enerxía',
    'nordy.es': 'Nordy', 'niba.es': 'Niba', 'fenieenergia.es': 'Feníe Energía', 'ganaenergia.com': 'Gana Energía',
    'nexusenergia.com': 'Nexus Energía', 'nobe.es': 'Nobe', 'electricadealginet.com': 'Eléctrica de Alginet',
    'urusenergy.com': 'Urus Energy', 'airelimpioenergia.com': 'Aire Limpio Energía', 'catgas.cat': 'Catgas',
    'disagrupo.es': 'Disa', 'endesa.com': 'Endesa', 'papernest.es': 'Papernest Energía', 'iberdrola.es': 'Iberdrola',
    'masnorte.es': 'Más Norte', 'es.met.com': 'MET Energía', 'quantiumlux.com': 'Quantium Lux',
    'energiaelcorteingles.es': 'Energía El Corte Inglés', 'barcelonaenergia.cat': 'Barcelona Energia',
    'totalenergies.es': 'TotalEnergies', 'holaluz.com': 'Holaluz', 'lucera.es': 'Lucera', 'somenergia.coop': 'Som Energia',
    'pepeenergy.com': 'Pepeenergy', 'audaxrenovables.es': 'Audax Renovables', 'factorenergia.com': 'Factor Energía',
}

# Prefijos de código postal de la península (lo que ponen las ofertas nacionales).
PENINSULA = {'0****', '1****', '2****', '4****'}


def dominio(url):
    return urllib.parse.urlparse(url or '').netloc.lower().removeprefix('www.')


def compania(oferta):
    for campo in ('webOferta', 'webContrato', 'webParticular'):
        d = dominio(oferta.get(campo))
        if d in COMPANIAS:
            return COMPANIAS[d]
    marca = oferta.get('nombreMarca') or oferta.get('nombreEmpresa')
    if marca:
        return marca.strip().title()
    d = dominio(oferta.get('webOferta') or oferta.get('webContrato'))
    return d.split('.')[0].capitalize() if d else None


def slug(texto):
    t = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')[:60]


def zona(territorio):
    """'nacional', 'islas' (solo Baleares o Canarias) o 'local' (algunos códigos postales)."""
    codigos = {c.strip() for c in (territorio or '').split(',') if c.strip()}
    if not codigos or '*****' in codigos:
        return 'nacional'
    if codigos & PENINSULA or {'01***', '28***'} & codigos or len([c for c in codigos if c.endswith('***')]) >= 10:
        return 'nacional'
    if codigos <= {'07***', '35***', '38***'}:
        return 'islas'
    return 'local'


def excedentes(descripcion):
    """Precio de compensación de excedentes si la descripción lo dice (€/kWh)."""
    for frase in re.split(r'[\n.](?!\d)', descripcion or ''):
        if not re.search(r'(?i)excedent', frase):
            continue
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*€\s*/\s*(k|M)Wh', frase)
        if m:
            v = float(m.group(1).replace(',', '.'))
            if m.group(2) == 'M':
                v /= 1000
            if 0 < v < 0.3:
                return round(v, 6)
    return None


def _kwh(numero, unidad):
    v = float(numero.replace('.', '').replace(',', '.')) if unidad.lower() == 'k' else float(numero.replace(',', '.'))
    return v if unidad.lower() == 'k' else v * 1000


def limites_consumo(oferta):
    """(mínimo, máximo) de consumo anual en kWh si la oferta los pone, o None."""
    texto = ' '.join(oferta.get(c) or '' for c in ('nombreOferta', 'descripcion', 'infoRequisitos', 'limitada'))
    entre = re.search(r'(?i)(?:consumos?\s+)?entre\s*(\d+(?:[.,]\d+)?)\s*y\s*(\d+(?:[.,]\d+)?)\s*(k|M)Wh', texto)
    if entre:
        return _kwh(entre.group(1), entre.group(3)), _kwh(entre.group(2), entre.group(3))
    rango = re.search(r'(\d+)\s*-\s*(\d+)\s*(k|M)Wh', oferta.get('nombreOferta') or '')
    if rango:
        return _kwh(rango.group(1), rango.group(3)), _kwh(rango.group(2), rango.group(3))
    menos = re.search(r'(?i)consumos?\s+anual(?:es)?\s+inferior(?:es)?\s+a\s*([\d.,]+)\s*(k|M)Wh', texto)
    if menos:
        return None, _kwh(menos.group(1), menos.group(2))
    mas = re.search(r'(?i)consumos?\s+anual(?:es)?\s+superior(?:es)?\s+a\s*([\d.,]+)\s*(k|M)Wh', texto)
    if mas:
        return _kwh(mas.group(1), mas.group(2)), None
    return None, None


def precios_medios(periodos):
    """Precios medios del primer año, ponderando los meses de cada periodo de la oferta."""
    claves = {'1HORAPUNTA': 'p1', '2HORAVALLE': 'p2', '3HORAPUNTA': 'punta', '4HORALLANO': 'llano', '5HORAVALLE': 'valle'}
    suma = {k: 0.0 for k in list(claves.values()) + ['fijo']}
    meses_total = 0
    for i, p in enumerate(periodos):
        meses = p.get('meses') or 12
        meses = min(meses, 12 - meses_total) if i < len(periodos) - 1 else 12 - meses_total
        if meses <= 0:
            break
        vistos = set()
        for f in p.get('franjasElectricidad') or []:
            if f.get('@type') != 'FranjaEstable':
                return None
            k = claves.get(f.get('refParams'))
            if not k:
                return None
            precio = f.get('precio') or {}
            v = precio.get('potencia') if k in ('p1', 'p2') else precio.get('energia')
            if v is None:
                return None
            suma[k] += v * meses
            vistos.add(k)
        if vistos != set(claves.values()):
            return None
        suma['fijo'] += (p.get('preciofijo') or 0) * meses
        meses_total += meses
    if meses_total == 0:
        return None
    return {k: v / meses_total for k, v in suma.items()}


def convertir(o, hoy):
    if o.get('tipoOferta') != 'Electricidad' or o.get('estado') != 'Activa':
        return None, 'no es luz activa'
    periodos = o.get('periodos') or []
    medios = precios_medios(periodos)
    if not medios:
        return None, 'no es 2.0TD de precio fijo por tramo'
    z = zona(o.get('territorio'))
    if z == 'islas':
        return None, 'solo Baleares o Canarias'
    nombre_c = compania(o) or 'Comercializadora'
    nombre_o = re.sub(r'(?i)^clon\s+', '', (o.get('nombreOferta') or '').strip())
    if medios['punta'] <= 0 or medios['valle'] <= 0 or medios['p1'] <= 0:
        return None, 'precios a cero'
    # La punta nunca es bastante más barata que el valle: si lo es, la oferta está mal
    # dada de alta (tramos cambiados) y es mejor no recomendarla.
    if medios['punta'] < medios['valle'] * 0.9:
        return None, 'tramos incoherentes (punta más barata que valle)'

    hasta = (o.get('fechaHasta') or '')[:10] or None
    notas = []
    if len(periodos) > 1 and periodos[0].get('meses'):
        notas.append(f"Precio medio del primer año: los primeros {periodos[0]['meses']} meses tienen otro precio o descuento.")
    descuento = sum((p.get('descuentoGeneralElecEuro') or 0) for p in periodos)
    if descuento:
        notas.append(f'Incluye un descuento de {descuento:.2f} € que no entra en el cálculo.'.replace('.', ',', 1))
    if o.get('limitada'):
        notas.append(o['limitada'].strip().rstrip('.') + '.')
    if o.get('serviciosObligatorios'):
        notas.append('Incluye servicios obligatorios: revisa sus condiciones.')
    if z == 'local':
        notas.append('Según la CNMC, solo se ofrece en algunos códigos postales.')
    notas.append('Datos del Comparador de Ofertas de Energía de la CNMC.')

    vigencia = []
    if hasta:
        d = dt.date.fromisoformat(hasta)
        vigencia.append(f'Oferta hasta el {d.day}/{d.month}/{d.year}')
    if o.get('revisionPrecios'):
        vigencia.append(f"revisión de precios {o['revisionPrecios'].strip().lower()}")
    cuota = max(0.0, medios['fijo'] - BONO_SOCIAL_MES)

    oferta = {
        'id': 'cnmc-' + slug(f'{nombre_c} {nombre_o}'),
        'nombre': f'{nombre_c} · {nombre_o}' if nombre_o else nombre_c,
        'punta': round(medios['punta'], 6),
        'llano': round(medios['llano'], 6),
        'valle': round(medios['valle'], 6),
        'potencia_p1': round(medios['p1'] / 365, 6),
        'potencia_p2': round(medios['p2'] / 365, 6),
        'excedentes': excedentes(o.get('descripcion')),
    }
    if cuota >= 0.05:
        oferta['cuota_mes'] = round(cuota, 4)
    minimo, maximo = limites_consumo(o)
    if minimo:
        oferta['consumo_min_kwh_año'] = round(minimo)
    if maximo:
        oferta['consumo_max_kwh_año'] = round(maximo)
    oferta['vigencia'] = ', '.join(vigencia) if vigencia else 'Consulta las condiciones en su web'
    if hasta:
        oferta['valido_hasta'] = hasta
    oferta['fuente'] = o.get('webOferta') or o.get('webContrato') or 'https://comparador.cnmc.gob.es/'
    oferta['comprobado'] = hoy
    oferta['nota'] = ' '.join(notas)
    oferta['origen'] = 'cnmc'
    return oferta, None


NAVEGADOR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
    'Accept-Language': 'es-ES,es;q=0.9',
}


def estado_enlace(url):
    """'vivo', 'roto' (la página no existe o el dominio no responde) o 'dudoso' (no se sabe).

    Muchas webs bloquean a los programas (403): eso cuenta como vivo, porque en el
    navegador del usuario sí abren. Solo un 404/410 o un dominio que no existe es roto.
    """
    for metodo in ('HEAD', 'GET'):
        try:
            req = urllib.request.Request(url, method=metodo, headers=NAVEGADOR)
            with urllib.request.urlopen(req, timeout=20):
                return 'vivo'
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                if metodo == 'GET':
                    return 'roto'
                continue  # algunos servidores responden mal a HEAD: se confirma con GET
            if e.code in (401, 403, 405, 429) and metodo == 'GET':
                return 'vivo'
            if metodo == 'GET':
                return 'dudoso'
        except urllib.error.URLError as e:
            motivo = getattr(e, 'reason', None)
            if isinstance(motivo, socket.gaierror):
                return 'roto'  # el dominio no existe (como "www." que no está configurado)
            if isinstance(motivo, ssl.SSLCertVerificationError):
                return 'roto'  # certificado no válido: el móvil avisaría de "conexión no segura"
            if metodo == 'GET':
                return 'dudoso'
        except Exception:  # noqa: BLE001
            if metodo == 'GET':
                return 'dudoso'
    return 'dudoso'


def arreglar_enlaces(ofertas, alternativas):
    """Cambia los enlaces rotos por el primero que funcione: sin "www.", la página de
    particulares, la web de otra oferta de la misma compañía o su portada. Nunca un PDF."""
    unicos = sorted({o['fuente'] for o in ofertas})
    with ThreadPoolExecutor(max_workers=8) as ex:
        estados = dict(zip(unicos, ex.map(estado_enlace, unicos)))
    cache = dict(estados)

    def vivo(url):
        if url not in cache:
            cache[url] = estado_enlace(url)
        return cache[url] == 'vivo'

    # Webs que funcionan de cada compañía, para usarlas si una de sus ofertas tiene la suya rota.
    buenas = {}
    for u, e in estados.items():
        if e == 'vivo':
            buenas.setdefault(dominio(u), []).append(u)

    cambios = {}
    for o in ofertas:
        url = o['fuente']
        if estados.get(url) != 'roto':
            continue
        p = urllib.parse.urlparse(url)
        dom = p.netloc.removeprefix('www.')
        candidatos = []
        if p.netloc.startswith('www.'):
            candidatos.append(url.replace('://www.', '://', 1))
        candidatos += [u for u in alternativas.get(o['id'], []) if u and u != url]
        candidatos += buenas.get(dom, [])
        candidatos += [f'https://{dom}/', f'https://www.{dom}/']
        candidatos = [c for c in candidatos if not urllib.parse.urlparse(c).path.lower().endswith('.pdf')]
        nuevo = next((c for c in candidatos if vivo(c)), None)
        if nuevo:
            o['fuente'] = nuevo
            cambios[url] = nuevo
    return cambios


def descargar(fecha):
    url = API.format(fecha=urllib.parse.quote(fecha.strftime('%d/%m/%Y'), safe=''))
    req = urllib.request.Request(url, headers={'User-Agent': 'CasaEnVatios/1.0 (comparador de tarifas)'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--fecha', help='AAAA-MM-DD (por defecto, hoy)')
    a.add_argument('--salida', default='reglas.json')
    a.add_argument('--manuales', default='ofertas-manuales.json')
    a.add_argument('--cnmc', help='usar un JSON ya descargado de la API en lugar de descargarlo')
    a.add_argument('--sin-comprobar-enlaces', action='store_true', help='no revisar si las webs de las ofertas abren')
    args = a.parse_args()

    fecha = dt.date.fromisoformat(args.fecha) if args.fecha else dt.date.today()
    hoy = fecha.isoformat()
    try:
        crudas = json.load(open(args.cnmc, encoding='utf-8')) if args.cnmc else descargar(fecha)
    except Exception as e:  # noqa: BLE001
        print(f'No se ha podido leer la API de la CNMC: {e}. No se cambia {args.salida}.', file=sys.stderr)
        return 1
    if not isinstance(crudas, list) or not crudas:
        print(f'La API de la CNMC no ha devuelto ofertas. No se cambia {args.salida}.', file=sys.stderr)
        return 1

    manuales = json.load(open(args.manuales, encoding='utf-8'))
    ofertas = list(manuales['ofertas'])
    ids = {o['id'] for o in ofertas}
    descartes = {}
    alternativas = {}  # id -> otras webs de la oferta, por si la principal está rota
    # Las copias internas ("clon Plan X") sobran si también está la oferta original.
    originales = {(compania(o), (o.get('nombreOferta') or '').strip().lower()) for o in crudas}
    crudas = [
        o for o in crudas
        if not re.match(r'(?i)clon\s', o.get('nombreOferta') or '')
        or (compania(o), re.sub(r'(?i)^clon\s+', '', o['nombreOferta']).strip().lower()) not in originales
    ]
    for o in crudas:
        conv, motivo = convertir(o, hoy)
        if not conv:
            descartes[motivo] = descartes.get(motivo, 0) + 1
            continue
        base, n = conv['id'], 2
        while conv['id'] in ids:
            conv['id'] = f'{base}-{n}'
            n += 1
        ids.add(conv['id'])
        ofertas.append(conv)
        alternativas[conv['id']] = [o.get('webParticular'), o.get('webContrato')]

    cambios = {} if args.sin_comprobar_enlaces else arreglar_enlaces(ofertas, alternativas)
    for viejo, nuevo in cambios.items():
        print(f'Enlace roto cambiado: {viejo} -> {nuevo}')

    cnmc = [o for o in ofertas if o.get('origen') == 'cnmc']
    if len(cnmc) < 20:
        print(f'Solo {len(cnmc)} ofertas válidas de la CNMC: algo va mal. No se cambia {args.salida}.', file=sys.stderr)
        return 1

    reglas = {
        'version': hoy,
        'fiscal': manuales['fiscal'],
        'fuentes': {
            'cnmc': 'Información recabada y gestionada por la CNMC en el Comparador de Ofertas de Energía '
                    '(https://comparador.cnmc.gob.es/). Licencia CC BY-SA 4.0.',
        },
        'ofertas': ofertas,
    }
    with open(args.salida, 'w', encoding='utf-8') as f:
        json.dump(reglas, f, ensure_ascii=False, indent=1)
        f.write('\n')
    companias = {o['nombre'].split(' · ')[0] for o in cnmc}
    print(f'{args.salida}: {len(ofertas)} ofertas ({len(manuales["ofertas"])} manuales y {len(cnmc)} de la CNMC, '
          f'{len(companias)} compañías). Descartadas: {descartes}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
