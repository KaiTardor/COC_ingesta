#!/usr/bin/env python3
"""Exporta la cobertura de la captura local de guerras normales a NDJSON."""

import argparse
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.buscar_guerras_normales import lock_root, read_json


def exportar(root, out):
    manifest = read_json(root / "manifest.json")
    season = manifest["month"]
    tags = manifest["clan_tags"]
    if not tags or len(tags) != len(set(tags)):
        raise ValueError("El manifiesto está vacío o contiene tags duplicados")
    out.mkdir(parents=True, exist_ok=True)
    output = out / "normal_war_coverage.ndjson"

    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=out, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for tag in sorted(tags):
                if not re.fullmatch(r"#[0-9A-Z]+", tag):
                    raise ValueError(f"Tag de clan inválido en el manifiesto: {tag!r}")
                path = root / "clanes" / f"{tag[1:]}.json"
                capture = read_json(path)
                coverage = capture["coverage"]
                if coverage["clan_tag"] != tag or coverage["month"] != season:
                    raise ValueError(f"Clan o temporada incoherente en {path}")
                row = {
                    "season": season,
                    "clan_tag": tag,
                    "fetched_at_utc": capture["fetched_at_utc"],
                    "payload": coverage,
                }
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

            stream.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    print(f"Temporada: {season} | clanes exportados: {len(tags)}")
    print(f"Archivo: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Directorio de captura por clan")
    parser.add_argument("--out", type=Path, required=True, help="Directorio de salida del lote")
    args = parser.parse_args()
    if not (args.root / "manifest.json").is_file():
        parser.error(f"No se encontró el manifiesto en {args.root}")
    try:
        with lock_root(args.root):
            exportar(args.root, args.out)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Exportación detenida: {error}\n")


if __name__ == "__main__":
    main()
