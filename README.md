# Clash of Clans · Ingesta de CWL y guerras normales

Este repositorio captura datos de la API de Clash of Clans y prepara los lotes NDJSON para Snowflake. La muestra permite analizar cuántos ataques usa cada clan, cuántas estrellas consigue por ataque y cómo varía su rendimiento según la ronda, la división y el ayuntamiento rival. El modelado y las transformaciones de dbt se mantienen en `COC_Medallion`.

La primera captura corresponde a **octubre de 2026**. El objetivo fue obtener 24 grupos por división. En Leyenda se consiguieron menos. Las comparativas describen los grupos accesibles incluidos en la muestra.

| Paso | Función | Resultado |
|---|---|---|
| 1 | Descubrir y capturar CWL | Manifiesto, grupos y guerras en `cwl/` |
| 2 | Exportar CWL | `groups.ndjson` y `wars.ndjson` |
| 3 | Capturar guerras normales de los clanes del mes | Manifiesto y JSON por clan en `wars/` |
| 4 | Exportar guerras normales y cobertura | `guerras_normales.ndjson` y `normal_war_coverage.ndjson` |

## Preparación

Ejecuta los comandos desde la raíz del repositorio en Linux o WSL. Los ejemplos usan las carpetas existentes de noviembre. Define estas variables en la misma terminal:

```bash
cd /mnt/c/Users/haowe/Desktop/COC
python3 -m pip install requests

MES=2026-11
CWL=cwl/cwl_nov2026_def
CWL_ANTERIOR=cwl/cwl_oct2026_def
NORMALES=wars/wars_nov2026_def
EXPORT="export/$MES"
LOTE_CWL=lote_001
LOTE_NORMALES=lote_002
```

Para consultar la API, utiliza una conexión cuya IP pública esté autorizada para el token. Si aún tienes que cargarlo en la terminal:

```bash
read -rsp 'Token de la API de CoC: ' COC_API_TOKEN
echo
export COC_API_TOKEN
```

Los scripts se versionan en Git. Las capturas, semillas de temporada y exportaciones se conservan como datos de trabajo fuera del repositorio remoto.

## 1. Descubrir y capturar CWL

```bash
python3 cwl_collect.py discover \
  --season "$MES" --root "$CWL" --previous "$CWL_ANTERIOR" \
  --search-pages 10 --min-members 15 --max-per-division 24
```

Cada tag candidato puede revelar un grupo entero. El descubrimiento deduplica los grupos. Un grupo de ocho clanes con siete rondas y cuatro enfrentamientos por ronda genera 28 JSON de guerra con ambos bandos. `--previous` aporta tags de la temporada anterior y `--search-pages` amplía la búsqueda en la API.

Si hay catálogo de ClashSpot, colócalo en `$CWL/seeds_used.json` y repite el descubrimiento. Mientras falta el ranking, el extractor puede verificar la división de un grupo activo del mes mediante el perfil de un clan y una segunda consulta al grupo. Si falta evidencia, guarda el grupo en `_unclassified/`. El cupo se aplica a grupos con división verificada. La división anterior sirve como referencia histórica.

Para completar guerras pendientes de grupos conocidos y consultar el avance:

```bash
python3 cwl_collect.py extract \
  --season "$MES" --root "$CWL" --max-requests 6000 --max-per-division 24

python3 cwl_collect.py status \
  --season "$MES" --root "$CWL" --max-per-division 24
```

Al alcanzar el presupuesto de peticiones, puedes repetir la operación. El extractor reutiliza las guerras guardadas como `warEnded`. En `$CWL/`, consulta `manifest.json` para los grupos e intentos, `guerras_index.json` para las rondas y el estado de cada guerra, y `seeds_used.json` para las semillas. Al mover una captura, revisa las rutas absolutas guardadas en el índice. El generador auxiliar `semillas_altas_nov2026.py` es específico de noviembre y su variable `ROOT` debe apuntar a `cwl/cwl_nov2026_def`.

## 2. Exportar los grupos y guerras CWL

El exportador incluye grupos clasificados y guerras finalizadas. Construye la carpeta de salida tomando el directorio padre de la captura. Con la estructura actual escribe en `cwl/export/`. Este bloque genera el lote y lo mueve a `export/` en la raíz del proyecto. Si el lote final ya existe, puedes usarlo directamente en el paso 3.

```bash
(
  set -e
  destino="$EXPORT/$LOTE_CWL"
  generado="$(dirname "$CWL")/export/$MES/$LOTE_CWL"
  if [ -e "$destino" ]; then
    echo "Ya existe el lote CWL: $destino"
    exit 0
  fi
  python3 export_cwl.py --root "$CWL" --batch "$LOTE_CWL"
  mkdir -p "$EXPORT"
  mv -T -- "$generado" "$destino"
)
```

El resultado es `export/2026-11/lote_001/{groups,wars}.ndjson`. Si amplías una captura y quieres publicar otra versión, asigna un nombre de lote nuevo antes de exportar.

## 3. Capturar el historial mensual de guerras normales

La lista de clanes procede del `groups.ndjson` del paso 2. El mes se selecciona por la fecha de fin de las guerras en UTC.

```bash
python3 buscar_guerras_normales.py \
  --groups "$EXPORT/$LOTE_CWL/groups.ndjson" \
  --month "$MES" --root "$NORMALES" --max-clans 0
```

`--max-clans 0` procesa todos los clanes. Para una prueba con cinco, usa `--max-clans 5` y una raíz de prueba independiente. Para reanudar una captura, repite el comando con la misma raíz.

Para una observación nueva, por ejemplo el **cierre de noviembre el 1 de diciembre**, conserva `MES=2026-11` y usa una raíz y un lote nuevos. Así también se conserva la captura de mitad de mes:

```bash
NORMALES=wars/wars_nov2026_cierre
LOTE_NORMALES=lote_003
python3 buscar_guerras_normales.py \
  --groups "$EXPORT/$LOTE_CWL/groups.ndjson" \
  --month "$MES" --root "$NORMALES" --max-clans 0
```

También existe `--refresh` para volver a consultar los clanes dentro de una misma raíz. Si falla la actualización de un clan que ya tenía una descarga correcta, el extractor conserva el JSON anterior.

## 4. Exportar guerras normales y cobertura

Ejecuta los dos exportadores sobre la misma captura y con el mismo destino. Hazlo al terminar la captura y antes de cargar el lote en Snowflake:

```bash
python3 exportar_guerras_normales.py \
  --root "$NORMALES" --out "$EXPORT/$LOTE_NORMALES" &&
python3 exportar_cobertura_guerras_normales.py \
  --root "$NORMALES" --out "$EXPORT/$LOTE_NORMALES"
```

`guerras_normales.ndjson` recoge las guerras desde la perspectiva del clan consultado. `normal_war_coverage.ndjson` recoge el estado de las capturas guardadas, incluidas las consultas correctas con cero guerras y los fallos registrados. Para contar los registros de ambos lotes:

```bash
wc -l "$EXPORT/$LOTE_CWL/"*.ndjson "$EXPORT/$LOTE_NORMALES/"*.ndjson
```

## Calendario y entrega a Snowflake

El calendario previsto para GitHub Actions contempla CWL a mitad de mes, una captura parcial de guerras normales tras completar CWL y otra de cierre el día 1 para el mes anterior. Ambos flujos admitirán ejecución manual. Los cuatro pasos anteriores ya se pueden ejecutar desde la terminal. La subida automática de los lotes a Bronze y el disparo posterior de dbt corresponden a la integración del workflow.

| Fichero NDJSON | Tabla de destino en Bronze |
|---|---|
| `groups.ndjson` | `CWL_GROUPS_RAW` |
| `wars.ndjson` | `CWL_WARS_RAW` |
| `guerras_normales.ndjson` | `NORMAL_WARS_RAW` |
| `normal_war_coverage.ndjson` | `NORMAL_WAR_COVERAGE_RAW` |
