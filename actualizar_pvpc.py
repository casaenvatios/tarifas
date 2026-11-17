"""Genera pvpc.json: el precio del PVPC 2.0TD hora a hora de los últimos 400 días (y de mañana
en cuanto Red Eléctrica lo publica, hacia las 20:15).

Fuente: archivo público 70 de ESIOS (Red Eléctrica), "Precios voluntarios para el pequeño
consumidor", el mismo que usa la web esios.ree.es/es/pvpc. No hace falta clave.

Por cada día y hora (hora local de la península, la que empieza a esa hora) se guardan:
  p: término de energía del PVPC (PCB), en €/kWh, sin impuestos. Ya incluye peajes y cargos.
  m: precio horario del mercado (PMHPCB), en €/kWh. Sirve para estimar el precio al que el
     PVPC compensa los excedentes de autoconsumo (precio de mercado menos un pequeño coste de
     desvío que este archivo no trae: por eso es aproximado).
  c: coeficiente del perfil de consumo tipo 2.0TD (COF2TD) × 10^9, entero. Sirve para estimar
     el PVPC de quien solo tiene facturas (sin consumo hora a hora).

El día del cambio de hora de marzo tiene 23 horas: la hora de las 2 queda en null. El de
octubre tiene 25: las dos horas de las 2 se promedian (el consumo hora a hora de la app
tampoco las distingue).

El término de potencia del PVPC (peajes + cargos + margen de comercialización) se copia de
ofertas-manuales.json, apartado "pvpc", donde se cambia cada año con su BOE.

Uso: python actualizar_pvpc.py [--salida pvpc.json] [--dias 400]
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.request

ARCHIVO = 'https://api.esios.ree.es/archives/70/download_json?locale=es&date={fecha}'
CABECERAS = {'User-Agent': 'CasaEnVatios/1.0 (precios PVPC)', 'Accept': 'application/json'}


def numero(texto):
    """'169,25' -> 169.25"""
    return float(str(texto).replace('.', '').replace(',', '.'))


def descargar_dia(fecha):
    """Lista de horas del archivo 70 para [fecha], o None si aún no está publicado."""
    url = ARCHIVO.format(fecha=fecha.isoformat())
    for intento in range(3):
        try:
            req = urllib.request.Request(url, headers=CABECERAS)
            with urllib.request.urlopen(req, timeout=60) as r:
                datos = json.load(r)
            horas = datos.get('PVPC') if isinstance(datos, dict) else None
            return horas or None
        except Exception as e:  # noqa: BLE001
            if intento == 2:
                print(f'{fecha}: no se ha podido descargar ({e})', file=sys.stderr)
                return None
            time.sleep(3)
    return None


def convertir_dia(horas):
    """Archivo 70 de un día -> {'p': [24], 'm': [24], 'c': [24]} por hora local de inicio."""
    acumulado = {h: [] for h in range(24)}
    for i, fila in enumerate(horas):
        if len(horas) == 25:
            # Día de 25 horas: ESIOS las numera seguidas ("00-01" ... "24-25"). La 3.ª y la 4.ª
            # son las dos horas de las 2 (antes y después de atrasar el reloj).
            inicio = i if i <= 2 else i - 1
        else:
            # Días de 24 y 23 horas: la etiqueta es la hora del reloj ("02-03" falta en marzo).
            inicio = int(fila['Hora'].split('-')[0])
        if not 0 <= inicio <= 23:
            raise ValueError(f'hora inesperada en el archivo de ESIOS: {fila["Hora"]}')
        acumulado[inicio].append((numero(fila['PCB']) / 1000, numero(fila['PMHPCB']) / 1000, numero(fila['COF2TD'])))
    dia = {'p': [], 'm': [], 'c': []}
    for h in range(24):
        valores = acumulado[h]
        if not valores:
            dia['p'].append(None)
            dia['m'].append(None)
            dia['c'].append(None)
            continue
        n = len(valores)
        dia['p'].append(round(sum(v[0] for v in valores) / n, 6))
        dia['m'].append(round(sum(v[1] for v in valores) / n, 6))
        dia['c'].append(round(sum(v[2] for v in valores) / n * 1e9))
    return dia


def comprobar_dia(dia):
    """Un día raro (sin precios o precios absurdos) no se guarda."""
    precios = [p for p in dia['p'] if p is not None]
    if len(precios) < 23:
        return False
    return all(-0.5 < p < 2 for p in precios)


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--salida', default='pvpc.json')
    a.add_argument('--manuales', default='ofertas-manuales.json')
    a.add_argument('--dias', type=int, default=400)
    a.add_argument('--hoy', help='AAAA-MM-DD (por defecto, hoy)')
    args = a.parse_args()

    hoy = dt.date.fromisoformat(args.hoy) if args.hoy else dt.date.today()
    manuales = json.load(open(args.manuales, encoding='utf-8'))
    potencia = manuales.get('pvpc')
    if not potencia:
        print('Falta el apartado "pvpc" en ofertas-manuales.json.', file=sys.stderr)
        return 1

    try:
        anterior = json.load(open(args.salida, encoding='utf-8'))
        dias = anterior.get('dias', {})
    except (OSError, ValueError):
        dias = {}

    desde = hoy - dt.timedelta(days=args.dias)
    nuevos = 0
    fecha = desde
    # Hasta mañana: se publica cada tarde hacia las 20:15.
    while fecha <= hoy + dt.timedelta(days=1):
        clave = fecha.isoformat()
        # Los días ya guardados no cambian, salvo hoy y mañana (por si se republican).
        if clave not in dias or fecha >= hoy:
            horas = descargar_dia(fecha)
            if horas:
                dia = convertir_dia(horas)
                if comprobar_dia(dia):
                    if dias.get(clave) != dia:
                        nuevos += 1
                    dias[clave] = dia
                else:
                    print(f'{clave}: datos raros, no se guardan', file=sys.stderr)
        fecha += dt.timedelta(days=1)

    dias = {k: v for k, v in sorted(dias.items()) if k >= desde.isoformat()}
    if len(dias) < 300:
        print(f'Solo hay {len(dias)} días de PVPC: algo va mal. No se cambia {args.salida}.', file=sys.stderr)
        return 1

    salida = {
        'version': max(dias),
        'fuente': 'Red Eléctrica (ESIOS), archivo 70 "Precios voluntarios para el pequeño consumidor". '
                  'Término de energía del PVPC 2.0TD (PCB) y precio horario del mercado (PMHPCB), en €/kWh sin impuestos.',
        'potencia': potencia,
        'dias': dias,
    }
    with open(args.salida, 'w', encoding='utf-8') as f:
        json.dump(salida, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    print(f'{args.salida}: {len(dias)} días, del {min(dias)} al {max(dias)} ({nuevos} nuevos o cambiados).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
