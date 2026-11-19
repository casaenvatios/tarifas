"""Pruebas de los filtros automáticos y de la comprobación de webs: python test_filtros.py"""

import actualizar_ofertas as a
import verificar_webs as v

MIN_P1, MIN_P2 = (30.817413 - 3.113) / 365, 0.725423 / 365
HOY = '2026-11-19'


def oferta(id_, p2, valle=0.09, origen='cnmc', compania='Imagina Energía'):
    return {'id': id_, 'nombre': f'{compania} · {id_}', 'punta': 0.2, 'llano': 0.13, 'valle': valle,
            'potencia_p1': 0.108533, 'potencia_p2': p2, 'origen': origen, 'fuente': 'https://ejemplo.es'}


def test_imagina():
    # El caso real: tres planes con la potencia de valle a 0,0552 y uno a 0,0233.
    ofertas = [oferta('a', 0.055196), oferta('b', 0.055196), oferta('c', 0.055196), oferta('mal', 0.023291)]
    buenas, dudosas, informe = a.revisar(ofertas, {}, [], MIN_P1, MIN_P2, HOY)
    assert [d['id'] for d in dudosas] == ['mal'], dudosas
    assert 'potencia de valle muy distinta' in dudosas[0]['dudosa']
    assert len(buenas) == 3 and len(informe) == 1


def test_la_web_confirma_y_vuelve():
    mal = oferta('mal', 0.023291)
    precios = {k: mal[k] for k in a.CAMPOS_PRECIO}
    verif = {'mal': {'estado': 'coincide', 'fecha': '2026-11-16', 'precios': precios}}
    ofertas = [oferta('a', 0.055196), oferta('b', 0.055196), mal]
    buenas, dudosas, _ = a.revisar(ofertas, verif, [], MIN_P1, MIN_P2, HOY)
    assert not dudosas and mal['comprobado_web'] == '2026-11-16'
    # Si la CNMC cambia los precios después de comprobarlos, la comprobación ya no vale.
    otra = dict(mal, valle=0.08)
    assert a.web_confirma(otra, verif, HOY) is None
    # Ni si es de hace más de dos semanas.
    assert a.web_confirma(mal, verif, '2026-12-05') is None


def test_cambio_brusco_y_manuales():
    antes = [oferta('x', 0.04)]
    buenas, dudosas, _ = a.revisar([oferta('x', 0.04, valle=0.13)], {}, antes, MIN_P1, MIN_P2, HOY)
    assert dudosas and 'más de un 20 %' in dudosas[0]['dudosa']
    # Las comprobadas a mano no pasan los filtros.
    buenas, dudosas, _ = a.revisar([oferta('m', 0.001, origen='manual')], {}, [], MIN_P1, MIN_P2, HOY)
    assert not dudosas


def test_numeros_y_busqueda():
    nums = v.numeros('Valle: 0,093 €/kWh  Llano 0,127  Punta: 0,201  Potencia: 20,146 €/kW año y 39,614 €/kW año 1.234,5')
    assert 0.093 in nums and 20.146 in nums and 1234.5 in nums
    imp = 1.05112696 * 1.21
    assert v.aparece(0.093, nums, 'energia', imp)
    assert v.aparece(0.0552, nums, 'potencia', imp), 'en €/kW año'
    assert not v.aparece(0.023291, nums, 'potencia', imp), 'el dato mal publicado no aparece'
    oferta_bien = {'punta': 0.201, 'llano': 0.127, 'valle': 0.093, 'potencia_p1': 39.614 / 365, 'potencia_p2': 20.146 / 365}
    assert v.comprobar(oferta_bien, nums, imp)['estado'] == 'coincide'
    oferta_mal = dict(oferta_bien, potencia_p2=0.023291)
    r = v.comprobar(oferta_mal, nums, imp)
    assert r['estado'] == 'no_coincide' and r['faltan'] == ['potencia_p2']
    assert v.comprobar(oferta_bien, [1.0, 2.0], imp)['estado'] == 'sin_datos'


if __name__ == '__main__':
    for nombre, f in list(globals().items()):
        if nombre.startswith('test_'):
            f()
            print('ok', nombre)
