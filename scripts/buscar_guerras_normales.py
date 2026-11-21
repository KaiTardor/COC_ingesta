#!/usr/bin/env python3
"""Busca guerras normales cerradas en un mes en el warlog oficial de CoC."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from getpass import getpass
import json
import os
from pathlib import Path
from random import Random
import sys
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def clan_tags(groups_file, month):
    tags = set()
    with groups_file.open(encoding="utf-8-sig") as records:
        for line in records:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("season") != month:
                continue
            for clan in record["payload"]["clans"]:
                tag = clan["tag"].strip().upper().lstrip("#")
                if not tag or not tag.isascii() or not tag.isalnum():
                    raise ValueError(f"Tag de clan no válido: {clan['tag']!r}")
                tags.add("#" + tag)
    return sorted(tags)


def fetch_json(url, token):
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if attempt == 3 or error.code not in {429, 500, 502, 503, 504}:
                raise
        except (URLError, TimeoutError):
            if attempt == 3:
                raise
        sleep(2 ** attempt)


def warlog(tag, token):
    url = f"https://api.clashofclans.com/v1/clans/{quote(tag, safe='')}/warlog"
    params = {"limit": 50}
    seen_cursors = set()
    while True:
        page = fetch_json(f"{url}?{urlencode(params)}", token)
        yield from page["items"]
        after = page.get("paging", {}).get("cursors", {}).get("after")
        if not after:
            return
        if after in seen_cursors:
            raise ValueError("La API repitió un cursor de paginación")
        seen_cursors.add(after)
        params["after"] = after


def collect_clan(tag, month, token):
    records = {}
    oldest = newest = None
    status = "ok"
    error_reason = None
    try:
        for war in warlog(tag, token):
            end = war["endTime"]
            end_day = datetime.strptime(end[:8], "%Y%m%d").date()
            oldest = end_day if oldest is None else min(oldest, end_day)
            newest = end_day if newest is None else max(newest, end_day)

            # En el warlog algunos resúmenes de CWL figuran como empate.
            # Exigimos el cupo de guerra normal y un rival identificado.
            opponent_tag = (war.get("opponent") or {}).get("tag")
            if (
                end_day.strftime("%Y-%m") != month
                or war.get("result") not in {"win", "lose", "tie"}
                or war.get("attacksPerMember") != 2
                or not opponent_tag
            ):
                continue

            records[(end, opponent_tag)] = {
                "month": month,
                "clan_tag": tag,
                "opponent_tag": opponent_tag,
                "payload": war,
            }
    except HTTPError as error:
        try:
            error_reason = json.load(error).get("reason")
        except (ValueError, AttributeError):
            error_reason = None
        if error.code == 401 or error_reason in {"accessDenied.invalidIp", "accessDenied.invalidToken"}:
            raise RuntimeError("Revisa el token y la IP autorizada en Supercell") from None
        status = f"http_{error.code}"
    except (URLError, TimeoutError, ValueError, KeyError) as error:
        status = "error"
        error_reason = str(error)

    month_start_seen = oldest is not None and oldest.strftime("%Y-%m-%d") <= f"{month}-01"
    summary = {
        "clan_tag": tag,
        "month": month,
        "status": status,
        "error_reason": error_reason,
        "wars": len(records),
        "oldest_log_day": oldest.isoformat() if oldest else None,
        "newest_log_day": newest.isoformat() if newest else None,
        "history_reaches_month_start": month_start_seen,
    }
    return list(records.values()), summary


def write_ndjson(path, rows):
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json_atomically(path, data):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        tmp_path = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
            stream.close()
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


@contextmanager
def lock_root(root):
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / ".extraccion.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Ya hay una extracción escribiendo en {root}") from None
        yield


def print_progress(index, total, summary, cached=False):
    source = " | guardado" if cached else ""
    print(
        f"[{index}/{total}] {summary['clan_tag']}: {summary['wars']} guerras | "
        f"{summary['status']} | histórico desde {summary['oldest_log_day'] or '-'}{source}",
        flush=True,
    )


def collect_to_root(root, groups, month, tags, token, refresh):
    manifest_path = root / "manifest.json"
    if manifest_path.exists() and read_json(manifest_path)["month"] != month:
        raise RuntimeError(f"{root} ya contiene una extracción de otro mes")

    clans_dir = root / "clanes"
    clans_dir.mkdir(exist_ok=True)
    manifest = {"month": month, "groups_file": str(groups), "clan_tags": tags}
    write_json_atomically(manifest_path, manifest)

    rows = inaccessible = retryable = 0
    reusable = {"ok", "http_403", "http_404"}
    for index, tag in enumerate(tags, 1):
        path = clans_dir / f"{tag[1:]}.json"
        previous = read_json(path) if path.exists() else None
        cached = previous is not None and not refresh and previous["coverage"]["status"] in reusable
        if cached:
            summary = previous["coverage"]
        else:
            wars, summary = collect_clan(tag, month, token)
            snapshot = {
                "coverage": summary,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "records": wars,
            }
            if summary["status"] == "ok" or previous is None or previous["coverage"]["status"] != "ok":
                write_json_atomically(path, snapshot)
            else:
                print(f"{tag}: se conserva la descarga anterior ({summary['status']} al actualizar).")

        print_progress(index, len(tags), summary, cached)
        if summary["status"] == "ok":
            rows += summary["wars"]
        elif summary["status"] in reusable:
            inaccessible += 1
        else:
            retryable += 1

    print(
        f"{rows} filas clan-guerra; {inaccessible} clanes inaccesibles; "
        f"{retryable} clanes pendientes de reintento. Salida: {root}"
    )
    return int(retryable > 0 or inaccessible == len(tags))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", type=Path, required=True, help="Ruta al groups.ndjson")
    parser.add_argument("--month", default="2026-10", help="Mes de cierre en UTC (YYYY-MM)")
    parser.add_argument("--max-clans", type=int, help="0 = todos; por defecto todos con --root y 15 con --out")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--root", type=Path, help="Directorio de archivos JSON por clan (Linux/WSL)")
    output.add_argument("--out", type=Path, help="Directorio para los dos NDJSON del piloto")
    parser.add_argument("--refresh", action="store_true", help="Volver a consultar los clanes ya guardados con --root")
    args = parser.parse_args()

    try:
        month = datetime.strptime(args.month, "%Y-%m").strftime("%Y-%m")
        tags = clan_tags(args.groups, month)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"Revisa --month y --groups: {error}")
    if not tags:
        parser.error(f"No hay tags en payload.clans para {month}")
    if args.refresh and args.root is None:
        parser.error("--refresh requiere --root")
    max_clans = args.max_clans
    if max_clans is None:
        max_clans = 0 if args.root is not None else 15
    if max_clans < 0:
        parser.error("--max-clans debe ser mayor o igual a cero")
    if 0 < max_clans < len(tags):
        tags = sorted(Random(2026).sample(tags, max_clans))

    token = os.environ.get("COC_API_TOKEN") or getpass("Token de la API de Supercell: ")
    token = token.strip()
    if not token:
        parser.error("Introduce un token o define COC_API_TOKEN")

    if args.root is not None:
        try:
            with lock_root(args.root):
                return collect_to_root(args.root, args.groups, month, tags, token, args.refresh)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            parser.exit(2, f"{error}\n")
        except KeyboardInterrupt:
            parser.exit(130, "Descarga interrumpida. Repite el comando para continuar.\n")

    all_wars = []
    coverage = []
    for index, tag in enumerate(tags, 1):
        try:
            wars, summary = collect_clan(tag, month, token)
        except RuntimeError as error:
            parser.exit(2, f"{error}\n")
        all_wars.extend(wars)
        coverage.append(summary)
        print_progress(index, len(tags), summary)

    args.out.mkdir(parents=True, exist_ok=True)
    write_ndjson(args.out / "guerras_normales.ndjson", all_wars)
    write_ndjson(args.out / "cobertura_clanes.ndjson", coverage)
    failures = sum(row["status"] != "ok" for row in coverage)
    print(f"{len(all_wars)} filas clan-guerra, {failures} clanes con errores. Salida: {args.out}")
    return 1 if failures == len(tags) else 0


if __name__ == "__main__":
    sys.exit(main())
