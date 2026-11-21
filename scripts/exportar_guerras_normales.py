#!/usr/bin/env python3
"""Exporta a NDJSON la recogida por clan de buscar_guerras_normales.py."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.buscar_guerras_normales import lock_root, read_json


def normalizar_tag(tag):
    if not isinstance(tag, str) or not tag.startswith("#") or not tag[1:].isascii() or not tag[1:].isalnum():
        raise ValueError(f"Tag no válido: {tag!r}")
    return tag.upper()


def fila_exportada(registro, clan, mes, origen):
    payload = registro["payload"]
    final = payload["endTime"]
    formato = "%Y%m%dT%H%M%S.%fZ" if "." in final else "%Y%m%dT%H%M%SZ"
    fin = datetime.strptime(final, formato).replace(tzinfo=timezone.utc)
    if registro["month"] != mes or fin.strftime("%Y-%m") != mes:
        raise ValueError(f"Una guerra de {clan} termina fuera de {mes}")
    if normalizar_tag(registro["clan_tag"]) != clan or normalizar_tag(payload["clan"]["tag"]) != clan:
        raise ValueError(f"El clan de una guerra no coincide con {clan}")
    rival = normalizar_tag(registro["opponent_tag"])
    if rival == clan or normalizar_tag(payload["opponent"]["tag"]) != rival:
        raise ValueError(f"Rival incorrecto para {clan} en {final}")
    if payload.get("attacksPerMember") != 2 or payload.get("result") not in {"win", "lose", "tie"}:
        raise ValueError(f"Registro ajeno al filtro de guerras normales para {clan} en {final}")

    fin_utc = fin.isoformat(timespec="microseconds")
    tags = sorted((clan, rival))
    return {
        "season": mes,
        "war_id": f"{fin_utc}|{tags[0]}|{tags[1]}",
        "clan_tag": clan,
        "opponent_tag": rival,
        "end_time_utc": fin_utc,
        "source_path": origen,
        "payload": payload,
    }


def exportar(raiz, salida):
    manifiesto = read_json(raiz / "manifest.json")
    mes = manifiesto["month"]
    tags = sorted({normalizar_tag(tag) for tag in manifiesto["clan_tags"]})
    pendientes = []
    estados = Counter()
    guerras = Counter()
    perspectivas = set()
    salida.mkdir(parents=True, exist_ok=True)
    destino = salida / "guerras_normales.ndjson"

    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=salida, delete=False) as temporal:
        ruta_temporal = Path(temporal.name)
        try:
            for clan in tags:
                ruta = raiz / "clanes" / f"{clan[1:]}.json"
                if not ruta.is_file():
                    pendientes.append(f"{clan} (sin descargar)")
                    continue
                captura = read_json(ruta)
                cobertura = captura["coverage"]
                registros = captura["records"]
                if (
                    cobertura["clan_tag"] != clan
                    or cobertura["month"] != mes
                    or cobertura["wars"] != len(registros)
                ):
                    raise ValueError(f"La cobertura no coincide con el contenido de {ruta}")

                estado = cobertura["status"]
                estados[estado] += 1
                if estado in {"http_403", "http_404"}:
                    continue
                if estado != "ok":
                    pendientes.append(f"{clan} ({estado})")
                    continue

                origen = (Path(raiz.name) / ruta.relative_to(raiz)).as_posix()
                for registro in registros:
                    fila = fila_exportada(registro, clan, mes, origen)
                    clave = fila["war_id"], clan
                    if clave in perspectivas:
                        raise ValueError(f"Perspectiva duplicada: {clave}")
                    perspectivas.add(clave)
                    guerras[fila["war_id"]] += 1
                    temporal.write(json.dumps(fila, ensure_ascii=False, separators=(",", ":")) + "\n")

            if pendientes:
                muestra = ", ".join(pendientes[:10])
                raise ValueError(f"Quedan {len(pendientes)} clanes pendientes: {muestra}. Reanuda la descarga.")

            temporal.close()
            ruta_temporal.replace(destino)
        finally:
            ruta_temporal.unlink(missing_ok=True)

    inaccesibles = estados["http_403"] + estados["http_404"]
    print(f"Mes: {mes} | clanes consultados: {len(tags)} | inaccesibles: {inaccesibles}")
    print(f"Filas clan-guerra: {len(perspectivas)}")
    print(f"Guerras únicas: {len(guerras)}")
    print(f"Guerras con ambas perspectivas: {sum(n == 2 for n in guerras.values())}")
    print(f"Archivo: {destino}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Directorio de la descarga por clan")
    parser.add_argument("--out", type=Path, required=True, help="Directorio de salida del lote")
    args = parser.parse_args()
    if not (args.root / "manifest.json").is_file():
        parser.error(f"No se encontró el manifiesto en {args.root}")

    try:
        with lock_root(args.root):
            exportar(args.root, args.out)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f"Exportación detenida: {error}\n")


if __name__ == "__main__":
    main()
