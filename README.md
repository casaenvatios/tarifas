# Tarifas de Casa en Vatios

Datos públicos que usa la app Casa en Vatios, publicados en GitHub Pages:

- `reglas.json`: ofertas de luz e impuestos, en https://casaenvatios.github.io/tarifas/reglas.json
- `pvpc.json`: precio del PVPC hora a hora de los últimos 400 días (y de mañana), en https://casaenvatios.github.io/tarifas/pvpc.json

## De dónde salen los datos

- **Ofertas de la CNMC** (`"origen": "cnmc"`): información recabada y gestionada por la CNMC en el [Comparador de Ofertas de Energía](https://comparador.cnmc.gob.es/), descargada de su [API pública](https://comparador.cnmc.gob.es/api/swagger-ui/index.html). Se usan las ofertas de electricidad para hogares (peaje 2.0TD):
  - `ofertas`: precio fijo por tramo. Es lo único que leen las versiones antiguas de la app.
  - `ofertas_horarias`: precio con horas de descuento (por ejemplo Octopus Sun Club o Repsol 10 horas), que la app solo calcula con el consumo hora a hora. Van en una clave aparte para que las versiones antiguas no las calculen como si fueran de precio fijo.
  - Las ofertas cuya página propia ya no existe llevan `"sin_web": true`; las que guardan en euros los excedentes que sobran, `"bateria_virtual": true`.
- **Ofertas comprobadas a mano** (`"origen": "manual"`): en `ofertas-manuales.json`, comprobadas en la web de cada comercializadora. Si una oferta de la CNMC es la misma (misma web y mismos precios), se queda solo la manual. Ahí están también las reglas fiscales (impuesto eléctrico, IVA, bono social, contador) y el término de potencia del PVPC (apartado `pvpc`).
- **PVPC**: archivo público 70 de [ESIOS](https://www.esios.ree.es/es/pvpc) (Red Eléctrica), "Precios voluntarios para el pequeño consumidor": término de energía del PVPC 2.0TD, precio horario del mercado y perfil de consumo tipo, hora a hora.

## Cómo se actualiza

La tarea `.github/workflows/actualizar.yml` se ejecuta dos veces al día:

- De madrugada: `actualizar_ofertas.py` descarga las ofertas de la CNMC y las junta con las manuales; `actualizar_pvpc.py` añade los precios del PVPC que falten.
- Por la tarde (después de las 20:15, cuando Red Eléctrica publica el día siguiente): lo mismo, para tener ya los precios de mañana.

Solo se publica si algo ha cambiado. Si la API de la CNMC falla, no se toca `reglas.json`; si ESIOS falla, se sigue con el `pvpc.json` anterior.

Para cambiar las ofertas manuales, los impuestos o el término de potencia del PVPC (cada 1 de enero, con los peajes de la CNMC y los cargos del Ministerio), edita `ofertas-manuales.json`. La próxima ejecución los incluirá (o lánzala a mano en la pestaña Actions → "Actualizar ofertas de la CNMC" → Run workflow).

## Licencia de los datos

Los datos de `reglas.json` se publican con licencia [Creative Commons Reconocimiento-CompartirIgual 4.0 Internacional (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/deed.es), la misma de los datos de la CNMC. La CNMC no se hace responsable del uso que se haga de esta información ni tiene ningún acuerdo con Casa en Vatios. Los precios del PVPC son datos públicos de Red Eléctrica (ESIOS).
