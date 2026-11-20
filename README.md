# Clash of Clans CWL

Proyecto de ingeniería de datos para analizar el rendimiento de los clanes en la Liga de Guerras de Clanes (CWL). Quiero medir cuántos ataques usa cada clan, cuántas estrellas consigue por ataque y cómo cambia su rendimiento según la ronda, la división y el nivel de ayuntamiento del rival.

La primera captura corresponde a **octubre de 2026**, con 24 grupos por división salvo Leyenda, donde conseguí menos. El extractor está en Python y guarda los datos en JSON. La carga a Snowflake y los modelos de dbt son la siguiente fase.

## Empezar una captura

Coloca `cwl_collect.py` en la raíz de `COC/`, junto a `cwl_oct2026_def/`. Los datos de octubre se quedan en esa carpeta. En Linux o WSL, instala `requests` con `python3 -m pip install requests` y exporta `COC_API_TOKEN` en la terminal donde vayas a ejecutar el script.

Desde `COC/`, para buscar grupos de noviembre:

```bash
python3 cwl_collect.py discover \
  --season 2026-11 \
  --root cwl_nov2026 \
  --previous cwl_oct2026_def \
  --search-pages 10 \
  --min-members 15 \
  --max-per-division 24
```

`--previous` lee los tags del catálogo y del manifiesto de octubre. `--search-pages 10` añade hasta diez consultas de búsqueda de clanes por nombre en la API. `--min-members 15` filtra esas búsquedas adicionales. Repite el mismo comando para continuar la captura de noviembre.

## Cómo se forma la muestra

Cada tag de clan sirve para localizar **un grupo entero**. Si el script encuentra el mismo grupo por otro de sus ocho clanes, lo cuenta una sola vez. Un grupo completo tiene siete rondas y cuatro enfrentamientos por ronda. Se guardan **28 JSON de guerra**, uno por enfrentamiento y con los dos bandos dentro.

- **Mientras falte el ranking de la temporada**, el script prueba primero los clanes conocidos y luego los que encuentre en la búsqueda adicional. Si el grupo pertenece al mes actual y sigue activo, el extractor consulta `warLeague` en el perfil de un clan y confirma que el grupo sigue activo tras la consulta. Guarda esa división con el tag y la fecha como evidencia de la temporada. Si falta la evidencia, conserva el grupo y sus guerras en `_unclassified/`. La división anterior de un clan solo sirve como referencia histórica.
- **Cuando esté disponible el ranking**, prepara el catálogo de ClashSpot con el mismo esquema que el de octubre y guárdalo como `cwl_nov2026/seeds_used.json`. En la siguiente ejecución, el extractor asigna división a los grupos que coincidan con ese catálogo y busca más candidatos donde falten grupos. Las guerras ya guardadas se conservan. Si el ranking contradice una división guardada, el extractor señala el conflicto. Un ascenso o descenso posterior se observa en la temporada siguiente, sin sobrescribir la división anterior.

El cupo de 24 se aplica a **grupos aceptados con división verificada**. Para el análisis también importa cuántos de ellos tienen las 28 guerras finalizadas. Las comparativas describen los grupos accesibles que entraron en la muestra.

## Consultar lo recogido

```bash
python3 cwl_collect.py status \
  --season 2026-10 \
  --root cwl_oct2026_def \
  --max-per-division 24
```

| Archivo | Cuándo mirarlo |
|---|---|
| `manifest.json` | Para ver los grupos descubiertos y los intentos por clan. El extractor lo usa para reanudar. |
| `guerras_index.json` | Para localizar una guerra y comprobar si está guardada y finalizada. |
| `seeds_used.json` | Para consultar qué candidatos aporta el ranking y a qué división pertenecen en ese mes. |

## Ejecución nocturna

Para la recogida basta con cron. Durante la CWL, programa a las 00:00 un lanzador que entre en `COC/`, cargue el token desde un archivo privado y ejecute el comando de la temporada con el Python que tiene instalado `requests`. Por ejemplo, si guardas ese lanzador como `run_cwl.sh`:

```cron
0 0 1-15 11 * /bin/bash /ruta/a/COC/run_cwl.sh >> /ruta/a/COC/cwl_collect.log 2>&1
```

La entrada usa la zona horaria de cron y ejecuta el lanzador los primeros 15 días de noviembre de cada año mientras siga instalada. Retírala al terminar la captura o adapta el lanzador para calcular la temporada. Si WSL suele estar cerrado a esa hora, usa el Programador de tareas de Windows para arrancarlo y lanzar el comando.

Cada ejecución retoma los grupos conocidos, actualiza las guerras pendientes y salta las guardadas como `warEnded`. Si se agota el presupuesto de peticiones, repite el comando. Si devuelve un 401 o 403, revisa el token y el acceso a la API desde ese entorno.

## Siguiente fase del pipeline

Al cerrar la recogida, comprobar la cobertura por división y cargar los JSON en **RAW de Snowflake**. En **Silver**, separar grupos, clanes inscritos, guerras, participaciones y ataques. En **Gold**, calcular ataques usados, estrellas por ataque, destrucción media y porcentaje de triples por clan, ronda o división.
