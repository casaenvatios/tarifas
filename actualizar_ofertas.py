"""Genera reglas.json para Casa en Vatios.

Junta dos fuentes:
  1. ofertas-manuales.json: reglas fiscales y ofertas comprobadas a mano en la web
     de cada comercializadora (las que no están en la CNMC o tienen datos extra).
  2. La API pública del Comparador de Ofertas de Energía de la CNMC
     (https://comparador.cnmc.gob.es/api/swagger-ui/index.html). Licencia CC BY-SA 4.0:
     "información recabada y gestionada por la CNMC en el Comparador de Ofertas de Energía".

De la CNMC se toman las ofertas de electricidad para hogares (peaje 2.0TD):
  - "ofertas": precio fijo por tramo (punta, llano, valle). Es lo único que leen las
    versiones antiguas de la app.
  - "ofertas_horarias": precio con horas de descuento (Octopus Sun Club, Repsol 10 horas...),
    que solo se pueden calcular con el consumo hora a hora. Van en una clave aparte para que
    las versiones antiguas no las calculen como si fueran de precio fijo.
Si la API falla, no se toca reglas.json (sale con error).

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


def texto_oferta(o):
    """Descripción e información adicional: algunas compañías ponen ahí los excedentes."""
    return '\n'.join(o.get(c) or '' for c in ('descripcion', 'infoAdicional'))


def bateria_virtual(o):
    """La oferta guarda en euros lo que los excedentes no pueden compensar ese mes
    (batería virtual, "wallet"). No cuenta si el texto dice que no la incluye."""
    nombre = o.get('nombreOferta') or ''
    if re.search(r'(?i)bater[ií]a\s+virtual', nombre):
        return True
    texto = texto_oferta(o)
    if re.search(r'(?i)no\s+incluye[^.]{0,40}(bater[ií]a\s+virtual|solar\s+wallet)', texto):
        return False
    return bool(re.search(r'(?i)incluye[^.]{0,60}(bater[ií]a\s+virtual|solar\s+wallet)', texto))


def cuota_bateria(o):
    """€/mes sin IVA del servicio de batería virtual, si la oferta lo cobra aparte
    ("Requiere la contratación del Servicio Batería 2,99 €/mes")."""
    m = re.search(r'(?i)servicio\s+bater[ií]a[^.\n]{0,40}?(\d+[.,]\d+)\s*€\s*/\s*mes', texto_oferta(o))
    return float(m.group(1).replace(',', '.')) if m else 0.0


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
    # La mención a la CNMC la enseña la app una vez, al final de la lista de tarifas.

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
        'excedentes': excedentes(texto_oferta(o)),
    }
    if bateria_virtual(o):
        oferta['bateria_virtual'] = True
        cuota += cuota_bateria(o)
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


def corregir(oferta, correcciones, hoy):
    """Aplica la corrección de [correcciones] a una oferta de la CNMC, si la hay.

    Cada corrección guarda en "cnmc" los valores que traía la CNMC cuando se comprobó
    la web. Si la CNMC los cambia, la corrección ya no vale (la compañía ha
    actualizado sus datos): se descarta y se avisa para revisarla.
    """
    c = correcciones.get(oferta['id'])
    if not c:
        return oferta, None
    visto = c.get('cnmc', {})
    if any(abs((oferta.get(k) or 0) - v) > 1e-6 for k, v in visto.items()):
        return oferta, f'La CNMC ha cambiado {oferta["id"]}: revisa su corrección en ofertas-manuales.json'
    oferta = dict(oferta)
    oferta.update(c['cambios'])
    oferta['comprobado'] = c['comprobado']
    oferta['nota'] = (oferta.get('nota', '') + ' ' + c['motivo']).strip()
    return oferta, None


CAMPOS_PRECIO = ('punta', 'llano', 'valle', 'potencia_p1', 'potencia_p2')


def web_confirma(oferta, verificaciones, hoy, dias=14):
    """La comprobación semanal de su web encontró estos mismos precios hace menos de [dias] días."""
    v = verificaciones.get(oferta['id'])
    if not v or v.get('estado') != 'coincide':
        return None
    if (dt.date.fromisoformat(hoy) - dt.date.fromisoformat(v['fecha'])).days > dias:
        return None
    precios = v.get('precios', {})
    if any(abs((oferta.get(k) or 0) - (precios.get(k) or 0)) > 1e-6 for k in CAMPOS_PRECIO):
        return None  # la CNMC ha cambiado los precios después de comprobarlos
    return v['fecha']


def compania_de(o):
    return o['nombre'].split(' · ')[0].strip().lower()


def revisar(ofertas, verificaciones, anteriores, minimo_p1, minimo_p2, hoy):
    """Filtros automáticos contra datos erróneos. Devuelve (buenas, dudosas, informe).

    Lo único que confirma un precio es encontrarlo en la web de la compañía
    (verificar_webs.py, cada semana): esas ofertas llevan "comprobado_web". La app
    solo recomienda las confirmadas (o comprobadas a mano).

    Estos filtros solo levantan sospechas (pueden ser precios reales):
    - Potencia por debajo de los peajes y cargos regulados.
    - Potencia a más de un 30 % de la mediana de los planes de su compañía (así se
      habría visto el error de Imagina).
    - Un precio que cambia más de un 20 % de un día para otro.
    Una oferta sospechosa y sin confirmar va a "ofertas_dudosas" con el motivo: la
    app la enseña con el aviso y las versiones antiguas, que no leen esa clave, no la
    ven. Vuelve sola cuando su web confirma los precios. Las ofertas comprobadas a
    mano no pasan estos filtros.
    """
    previas = {o['id']: o for o in anteriores}
    por_compania = {}
    for o in ofertas:
        por_compania.setdefault(compania_de(o), []).append(o)

    def mediana(xs):
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    buenas, dudosas, informe = [], [], []
    for o in ofertas:
        motivos = []
        if o.get('origen') == 'cnmc':
            if (o.get('potencia_p1') or 0) < minimo_p1 - 1e-6 or (o.get('potencia_p2') or 0) < minimo_p2 - 1e-6:
                motivos.append('potencia por debajo de los peajes y cargos regulados')
            hermanas = [x for x in por_compania[compania_de(o)] if x is not o]
            for k in ('potencia_p1', 'potencia_p2'):
                if len(hermanas) >= 1:
                    m = mediana([x.get(k) or 0 for x in por_compania[compania_de(o)]])
                    if m > 0 and abs((o.get(k) or 0) - m) / m > 0.30:
                        tramo = 'punta' if k == 'potencia_p1' else 'valle'
                        motivos.append(f'potencia de {tramo} muy distinta de la de sus otros planes '
                                       f'({o.get(k)} frente a {round(m, 6)} €/kW día)')
            antes = previas.get(o['id'])
            if antes:
                for k in CAMPOS_PRECIO:
                    a, b = antes.get(k) or 0, o.get(k) or 0
                    if a > 0 and abs(b - a) / a > 0.20:
                        nombre = {'potencia_p1': 'potencia de punta', 'potencia_p2': 'potencia de valle'}.get(k, k)
                        motivos.append(f'{nombre} ha cambiado más de un 20 % desde ayer ({a} -> {b})')
        confirmada = web_confirma(o, verificaciones, hoy)
        if confirmada:
            o['comprobado_web'] = confirmada
        if motivos and not confirmada:
            d = dict(o)
            d['dudosa'] = '; '.join(motivos)
            dudosas.append(d)
            informe.append(f"- **{o['nombre']}** (`{o['id']}`): {d['dudosa']}. {o.get('fuente', '')}")
        else:
            buenas.append(o)
    return buenas, dudosas, informe


def misma_oferta(a, b):
    """La misma oferta dos veces (una comprobada a mano y otra de la CNMC): misma web y mismos precios."""
    if dominio(a.get('fuente')) != dominio(b.get('fuente')):
        return False
    return all(abs((a.get(k) or 0) - (b.get(k) or 0)) < 1e-4 for k in ('punta', 'llano', 'valle', 'potencia_p1', 'potencia_p2'))


DIAS_SEMANA = {'L': 1, 'M': 2, 'X': 3, 'J': 4, 'V': 5, 'S': 6, 'D': 7}


def es_para_empresas(o):
    """Ofertas de negocio (pymes, hostelería): no son para una casa."""
    nombre = (o.get('nombreOferta') or '').lower()
    web = (o.get('webOferta') or '').lower()
    return bool(re.search(r'pyme|negocio|horeca|empresa', nombre)) or 'empresas' in web


def _dia_mes(texto):
    d, m = texto.strip().split('/')
    return int(m), int(d)


def bloques_horarios(franja):
    """Horas con precio rebajado de una franja de la CNMC, o None si no se entiende.

    causaFlexible 4: horas fijas ("horasDia"), numeradas por la hora en que empiezan
    (Octopus Sun Club: 12..17 = de 12:00 a 18:00). El 24 es la medianoche, la hora 0 del
    día siguiente: Repsol 2ª Residencia pone "15..24" para "desde las 15 h del viernes" y
    "1..24" para el fin de semana entero. Se guarda como "medianoche": true.
    causaFlexible 2: el cliente elige "numHoras" horas al día."""
    bloques = []
    for fh in franja.get('franjaHoras') or []:
        desde_txt, hasta_txt = (fh.get('periodo') or '01/01-31/12').split('-')
        dias = [DIAS_SEMANA[c] for c in (fh.get('dias') or '') if c in DIAS_SEMANA]
        if not dias:
            return None
        bloque = {
            'desde': '%02d-%02d' % _dia_mes(desde_txt),
            'hasta': '%02d-%02d' % _dia_mes(hasta_txt),
            'dias': dias,
            'festivos': 'si' if fh.get('incluirFestivos') else ('no' if fh.get('excluirFestivos') else 'como_el_dia'),
        }
        causa = fh.get('causaFlexible')
        if causa == 4:
            horas = [int(h) for h in str(fh.get('horasDia') or '').split(',') if h.strip().isdigit()]
            if not horas:
                return None
            if any(h < 0 or h > 24 for h in horas):
                return None
            bloque.update({'tipo': 'fijas', 'horas': sorted({h for h in horas if h < 24})})
            if 24 in horas:
                bloque['medianoche'] = True
        elif causa == 2 and fh.get('numHoras'):
            bloque.update({'tipo': 'elige', 'num_horas': int(fh['numHoras'])})
        else:
            return None
        bloques.append(bloque)
    return bloques or None


def convertir_horaria(o, hoy):
    """Oferta 2.0TD con horas de descuento ("Flexible" en la CNMC), o (None, motivo)."""
    if o.get('tipoOferta') != 'Electricidad' or o.get('estado') != 'Activa':
        return None, 'no es luz activa'
    if (o.get('electricidad') or {}).get('tipoPlanElectricidad') != 'Flexible':
        return None, 'no es de horas de descuento'
    if es_para_empresas(o):
        return None, 'para empresas'
    periodos = o.get('periodos') or []
    if len(periodos) != 1:
        return None, 'horas de descuento con varios periodos'
    p = periodos[0]
    base, rebajas = {}, []
    for f in p.get('franjasElectricidad') or []:
        ref = f.get('refParams')
        precio = f.get('precio') or {}
        if ref in ('1HORAPUNTA', '2HORAVALLE'):
            base[ref] = precio.get('potencia')
        elif ref in ('3HORAPUNTA', '4HORALLANO', '5HORAVALLE'):
            base[ref] = precio.get('energia')
        elif f.get('franjaHoras'):
            bloques = bloques_horarios(f)
            if bloques is None or not precio.get('energia'):
                return None, 'horas de descuento que no se entienden'
            for b in bloques:
                b['precio'] = round(precio['energia'], 6)
            rebajas += bloques
    if len(base) != 5 or any(v is None or v <= 0 for v in base.values()) or not rebajas:
        return None, 'horas de descuento incompletas'
    z = zona(o.get('territorio'))
    if z == 'islas':
        return None, 'solo Baleares o Canarias'

    nombre_c = compania(o) or 'Comercializadora'
    nombre_o = (o.get('nombreOferta') or '').strip()
    hasta = (o.get('fechaHasta') or '')[:10] or None
    notas = []
    if o.get('limitada'):
        notas.append(o['limitada'].strip().rstrip('.') + '.')
    if o.get('serviciosObligatorios'):
        notas.append('Incluye servicios obligatorios: revisa sus condiciones.')
    if (o.get('electricidad') or {}).get('permanenciaElec'):
        notas.append('Tiene permanencia.')
    if any(b['tipo'] == 'elige' for b in rebajas):
        notas.append('Tú eliges las horas con descuento: la app calcula con las horas en que más gastas.')
    if z == 'local':
        notas.append('Según la CNMC, solo se ofrece en algunos códigos postales.')
    vigencia = []
    if hasta:
        d = dt.date.fromisoformat(hasta)
        vigencia.append(f'Oferta hasta el {d.day}/{d.month}/{d.year}')
    if o.get('revisionPrecios'):
        vigencia.append(f"revisión de precios {o['revisionPrecios'].strip().lower()}")
    cuota = max(0.0, (p.get('preciofijo') or 0) - BONO_SOCIAL_MES)
    if bateria_virtual(o):
        cuota += cuota_bateria(o)

    oferta = {
        'id': 'cnmc-h-' + slug(f'{nombre_c} {nombre_o}'),
        'nombre': f'{nombre_c} · {nombre_o}',
        'punta': round(base['3HORAPUNTA'], 6),
        'llano': round(base['4HORALLANO'], 6),
        'valle': round(base['5HORAVALLE'], 6),
        'potencia_p1': round(base['1HORAPUNTA'] / 365, 6),
        'potencia_p2': round(base['2HORAVALLE'] / 365, 6),
        'excedentes': excedentes(texto_oferta(o)),
        'horas_descuento': rebajas,
    }
    if cuota >= 0.05:
        oferta['cuota_mes'] = round(cuota, 4)
    if bateria_virtual(o):
        oferta['bateria_virtual'] = True
    pot_max = (o.get('electricidad') or {}).get('potenciaContratadaMaxima')
    if pot_max and pot_max < 15:
        oferta['potencia_max_kw'] = pot_max
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
        # Si solo queda una página general de la compañía (no la de esta oferta),
        # la oferta no se encuentra en su web: la app lo avisa y no la recomienda la primera.
        propias = {url.replace('://www.', '://', 1), *(u for u in alternativas.get(o['id'], []) if u)}
        if nuevo not in propias:
            o['sin_web'] = True
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
    a.add_argument('--verificaciones', default='verificaciones.json', help='resultado de verificar_webs.py')
    a.add_argument('--informe', default='informe-dudosas.md', help='lista de ofertas apartadas (para el aviso)')
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
    correcciones = manuales.get('correcciones', {})
    corregidas = 0
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
    horarias = []
    for o in crudas:
        conv, motivo = convertir(o, hoy)
        if not conv:
            hor, motivo_h = convertir_horaria(o, hoy)
            if hor:
                if hor['id'] not in ids:
                    ids.add(hor['id'])
                    horarias.append(hor)
                    alternativas[hor['id']] = [o.get('webParticular'), o.get('webContrato')]
                continue
            if motivo_h not in ('no es de horas de descuento', 'no es luz activa'):
                motivo = motivo_h
            descartes[motivo] = descartes.get(motivo, 0) + 1
            continue
        conv, aviso = corregir(conv, correcciones, hoy)
        if aviso:
            print(aviso, file=sys.stderr)
        elif conv['id'] in correcciones:
            corregidas += 1
        if any(misma_oferta(conv, m) for m in manuales['ofertas']):
            descartes['repetida de una manual'] = descartes.get('repetida de una manual', 0) + 1
            continue
        base, n = conv['id'], 2
        while conv['id'] in ids:
            conv['id'] = f'{base}-{n}'
            n += 1
        ids.add(conv['id'])
        ofertas.append(conv)
        alternativas[conv['id']] = [o.get('webParticular'), o.get('webContrato')]

    cambios = {} if args.sin_comprobar_enlaces else arreglar_enlaces(ofertas + horarias, alternativas)
    for viejo, nuevo in cambios.items():
        print(f'Enlace roto cambiado: {viejo} -> {nuevo}')

    # Filtros automáticos: las ofertas con datos que no cuadran se apartan en "ofertas_dudosas"
    # (las versiones de la app hasta la 1.0.13+22 no leen esa clave, así que no las ven).
    try:
        verificaciones = json.load(open(args.verificaciones, encoding='utf-8')).get('ofertas', {})
    except (OSError, ValueError):
        verificaciones = {}
    try:
        anteriores = json.load(open(args.salida, encoding='utf-8')).get('ofertas', [])
    except (OSError, ValueError):
        anteriores = []
    pvpc = manuales.get('pvpc', {})
    # Peajes y cargos de potencia (€/kW día): el término de potencia del PVPC menos su margen de comercialización.
    minimo_p1 = (pvpc.get('potencia_p1_eur_kw_año', 30.817413) - 3.113) / 365
    minimo_p2 = pvpc.get('potencia_p2_eur_kw_año', 0.725423) / 365
    ofertas, dudosas, informe = revisar(ofertas, verificaciones, anteriores, minimo_p1, minimo_p2, hoy)
    with open(args.informe, 'w', encoding='utf-8') as f:
        if informe:
            f.write(f'Ofertas sospechosas el {hoy}: sus datos no cuadran y su web no los ha confirmado. La app las '
                    'enseña con un aviso y nunca las recomienda. Vuelven solas cuando la comprobación semanal de su web '
                    'encuentra esos precios o la CNMC los corrige. No hace falta hacer nada.\n\n')
            f.write('\n'.join(informe) + '\n')
    confirmadas = sum(1 for o in ofertas if o.get('comprobado_web'))

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
        'ofertas_horarias': horarias,
        'ofertas_dudosas': dudosas,
    }
    with open(args.salida, 'w', encoding='utf-8') as f:
        json.dump(reglas, f, ensure_ascii=False, indent=1)
        f.write('\n')
    companias = {o['nombre'].split(' · ')[0] for o in cnmc}
    print(f'{args.salida}: {len(ofertas)} ofertas ({len(manuales["ofertas"])} manuales y {len(cnmc)} de la CNMC, '
          f'{len(companias)} compañías), {len(horarias)} con horas de descuento, {corregidas} corregidas a mano, '
          f'{confirmadas} confirmadas en su web, {len(dudosas)} apartadas por dudosas. Descartadas: {descartes}')
    for linea in informe:
        print('Apartada:', linea)
    return 0


if __name__ == '__main__':
    sys.exit(main())
