# Tarifas de Casa en Vatios

Lista de ofertas de luz que usa la app Casa en Vatios: `reglas.json`, publicada en https://casaenvatios.github.io/tarifas/reglas.json

## De dónde salen los datos

- **Ofertas de la CNMC** (`"origen": "cnmc"`): información recabada y gestionada por la CNMC en el [Comparador de Ofertas de Energía](https://comparador.cnmc.gob.es/), descargada de su [API pública](https://comparador.cnmc.gob.es/api/swagger-ui/index.html). Se usan las ofertas de electricidad para hogares (peaje 2.0TD) con precio fijo por tramo.
- **Ofertas comprobadas a mano** (`"origen": "manual"`): en `ofertas-manuales.json`, comprobadas en la web de cada comercializadora. Ahí están también las reglas fiscales (impuesto eléctrico, IVA, bono social, contador).

## Cómo se actualiza

Cada día, la tarea `.github/workflows/actualizar.yml` ejecuta `actualizar_ofertas.py`, que descarga las ofertas de la CNMC, las junta con las manuales y publica `reglas.json` si algo ha cambiado. Si la API de la CNMC falla, no se toca la lista.

Para cambiar las ofertas manuales o los impuestos, edita `ofertas-manuales.json`. La próxima ejecución los incluirá (o lánzala a mano en la pestaña Actions → "Actualizar ofertas de la CNMC" → Run workflow).

## Licencia de los datos

Los datos de `reglas.json` se publican con licencia [Creative Commons Reconocimiento-CompartirIgual 4.0 Internacional (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/deed.es), la misma de los datos de la CNMC. La CNMC no se hace responsable del uso que se haga de esta información ni tiene ningún acuerdo con Casa en Vatios.
