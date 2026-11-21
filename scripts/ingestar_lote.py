"""Valida, registra y carga lotes cerrados de Clash of Clans."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

FILES = {
    "cwl": {
        "groups.ndjson": "CWL_GROUPS_RAW",
        "wars.ndjson": "CWL_WARS_RAW",
    },
    "normales": {
        "guerras_normales.ndjson": "NORMAL_WARS_RAW",
        "normal_war_coverage.ndjson": "NORMAL_WAR_COVERAGE_RAW",
    },
}


def inventory(folder, season, batch, kind):
    files = {}
    for name in FILES[kind]:
        digest = hashlib.sha256()
        rows = 0
        with (folder / name).open("rb") as stream:
            for line in stream:
                digest.update(line)
                if not line.strip():
                    continue
                record = json.loads(line)
                if record["season"] != season:
                    raise ValueError(f"{name}, línea {rows + 1}: temporada inesperada")
                rows += 1
        files[name] = {"sha256": digest.hexdigest(), "rows": rows}
    return {"season": season, "batch": batch, "kind": kind, "files": files}


@contextmanager
def single_runner():
    with (Path.home() / ".coc_ingesta.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_file(cursor, stage, ledger, folder, manifest, name, table):
    season, batch = manifest["season"], manifest["batch"]
    expected = manifest["files"][name]
    key = f"{season}/{batch}/{name}"

    cursor.execute(
        f"select sha256, rows_loaded from {ledger} where file_key = %s",
        (key,),
    )
    previous = cursor.fetchall()
    if previous:
        if previous != [(expected["sha256"], expected["rows"])]:
            raise ValueError(f"Conflicto con el registro de carga: {key}")
        print(f"Ya cargado: {key}")
        return

    # Verifica que la copia local conserva el hash del manifiesto.
    with tempfile.TemporaryDirectory() as temp:
        snapshot = Path(temp) / name
        shutil.copyfile(folder / name, snapshot)
        with snapshot.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected["sha256"]:
                raise ValueError(f"El fichero cambió: {name}")

        path = f"@{stage}/{season}/{batch}/{expected['sha256']}"
        staged_name = f"{name}.gz"
        if expected["rows"]:
            cursor.execute(
                f"put 'file://{snapshot}' {path}/ "
                "auto_compress=true overwrite=false"
            )

        cursor.execute("begin")
        try:
            if expected["rows"]:
                cursor.execute(
                    f"copy into {table} (record) from (select $1 from {path}/) "
                    f"files=('{staged_name}') "
                    "file_format=(type=json compression=gzip) "
                    "on_error=abort_statement"
                )
                result = cursor.fetchall()
                if (
                    len(result) != 1
                    or result[0][1] != "LOADED"
                    or result[0][3] != expected["rows"]
                ):
                    raise ValueError(f"Comprobar COPY de {key}: {result}")

            cursor.execute(
                f"insert into {ledger} (file_key, sha256, rows_loaded) values (%s, %s, %s)",
                (key, expected["sha256"], expected["rows"]),
            )
            cursor.execute("commit")
        except Exception:
            cursor.execute("rollback")
            raise

    print(f"Cargado: {key} ({expected['rows']} filas)")


def build_models(manifest):
    host = os.environ.get("DBT_CLOUD_HOST")
    if not host:
        subprocess.run(
            ["dbt", "build"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
        )
        return

    endpoint = urlsplit(host)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.path.strip("/"):
        raise ValueError("DBT_CLOUD_HOST debe ser la URL HTTPS de tu instancia")

    account = int(os.environ["DBT_CLOUD_ACCOUNT_ID"])
    job = int(os.environ["DBT_CLOUD_JOB_ID"])
    base = f"{host.rstrip('/')}/api/v2/accounts/{account}"
    headers = {
        "Authorization": "Token " + os.environ["DBT_CLOUD_TOKEN"],
        "Content-Type": "application/json",
    }

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(base + path, data=data, headers=headers)
        with urlopen(req, timeout=60) as response:
            return json.load(response)["data"]

    cause = f"Ingesta COC {manifest['season']}/{manifest['batch']}"
    run_id = request(f"/jobs/{job}/run/", {"cause": cause})["id"]
    print(f"dbt Cloud run {run_id}", flush=True)
    while True:
        run = request(f"/runs/{run_id}/")
        if run["status"] == 10:
            return
        if run["status"] in (20, 30):
            raise RuntimeError(f"dbt Cloud run {run_id}: {run['status']}")
        time.sleep(15)


def ingest(folder, manifest, schema="BRONZE", *, only_load=False):
    import snowflake.connector

    database = os.environ["DBT_COC_BRONZE_DB"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ValueError("Nombre de base Bronze no válido")

    prefix = f"{database}.{schema}."
    ledger = prefix + "COC_LOAD_FILES"
    stage = prefix + "COC_BATCH_STAGE"

    with snowflake.connector.connect(
        connection_name=os.environ["COC_SNOWFLAKE_CONNECTION"]
    ) as conn:
        with conn.cursor() as cursor:
            if schema.upper() != "BRONZE":
                cursor.execute(f"create stage if not exists {stage}")
                cursor.execute(
                    f"create table if not exists {ledger} "
                    "(file_key varchar, sha256 varchar, rows_loaded number, "
                    "loaded_at timestamp_tz default current_timestamp())"
                )

            for name, table in FILES[manifest["kind"]].items():
                load_file(cursor, stage, ledger, folder, manifest, name, prefix + table)

    if not only_load:
        build_models(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["cerrar", "verificar", "cargar"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("season")
    parser.add_argument("batch")
    parser.add_argument("kind", choices=FILES)
    parser.add_argument("--schema", default="BRONZE", help="Esquema de destino en Snowflake")
    parser.add_argument("--solo-carga", action="store_true", help="Cargar el lote sin ejecutar dbt")
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", args.season):
        parser.error("La temporada debe tener formato AAAA-MM")
    if not re.fullmatch(r"lote_[0-9]+", args.batch):
        parser.error("El identificador debe seguir el formato lote_002")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.schema):
        parser.error("Nombre de esquema no válido")
    if args.action == "cargar" and args.schema.upper() != "BRONZE" and not args.solo_carga:
        parser.error("Usa --solo-carga al cargar en un esquema distinto de BRONZE")

    with single_runner():
        folder = args.folder.resolve()
        path = folder / "manifest_ingesta.json"
        manifest = inventory(folder, args.season, args.batch, args.kind)

        if args.action == "cerrar" and not path.exists():
            with tempfile.NamedTemporaryFile("w", dir=folder, delete=False) as stream:
                json.dump(manifest, stream, indent=2)
            os.replace(stream.name, path)

        if json.loads(path.read_text()) != manifest:
            raise ValueError("El contenido difiere del manifiesto cerrado")

        print(json.dumps(manifest, indent=2))
        if args.action == "cargar":
            ingest(folder, manifest, args.schema, only_load=args.solo_carga)


if __name__ == "__main__":
    main()
