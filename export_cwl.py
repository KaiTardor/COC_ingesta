#!/usr/bin/env python3
"""Export a local CWL capture as two NDJSON files for Snowflake Bronze."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def write_record(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def export(root, batch_name):
    manifest = read_json(root / "manifest.json")
    index = read_json(root / "guerras_index.json")
    season = manifest["season"]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", season) or index["season"] != season:
        raise ValueError("Temporada inválida o distinta entre manifiesto e índice.")

    output = root.parent / "export" / season / batch_name
    if output.exists():
        raise ValueError(f"El lote ya existe: {output}")

    divisions = manifest.get("league_ids_by_name", {})
    counts = Counter()
    seen = set()
    unclassified = unfinished = complete = group_count = war_count = 0
    output.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=output.parent) as temporary:
        batch = Path(temporary) / batch_name
        batch.mkdir()
        with (batch / "groups.ndjson").open("w", encoding="utf-8") as groups, (
            batch / "wars.ndjson"
        ).open("w", encoding="utf-8") as wars:
            for group_id, group in sorted(manifest["groups"].items()):
                slug = group.get("division_slug")
                if not slug:
                    unclassified += 1
                    continue
                name = group["division_name"]
                league_id = group.get("league_id")
                division_id = league_id or divisions.get(name)
                if not division_id:
                    raise ValueError(f"{group_id}: falta el ID de división para {name}.")
                if group["group_id"] != group_id or group["season"] != season:
                    raise ValueError(f"{group_id}: ID o temporada incoherente.")
                if league_id and group.get("group_folder"):
                    folder = Path(str(league_id)) / group["group_folder"]
                else:
                    folder = Path(slug) / group_id
                path = folder / "league_group.json"
                metadata = {
                    "season": season,
                    "group_id": group_id,
                    "division_id": str(division_id),
                    "division_name": name,
                }
                write_record(groups, {
                    **metadata, "source_path": path.as_posix(), "payload": read_json(root / path),
                })
                counts[name] += 1
                group_count += 1
                finished_in_group = 0

                for tag in sorted(group["wars"]):
                    entry = index["wars"].get(tag)
                    if not entry or entry["group_id"] != group_id or entry["season"] != season:
                        raise ValueError(f"{tag}: el índice no coincide con el grupo {group_id}.")
                    if tag in seen:
                        raise ValueError(f"{tag}: guerra repetida entre grupos.")
                    seen.add(tag)
                    if not entry["finished"]:
                        unfinished += 1
                        continue

                    round_number = entry["round"]
                    if not entry["saved"] or type(round_number) is not int or not 1 <= round_number <= 7:
                        raise ValueError(f"{tag}: ronda o estado incoherente en el índice.")
                    path = folder / "guerras" / f"{tag.lstrip('#')}.json"
                    payload = read_json(root / path)
                    if payload["state"] != "warEnded":
                        raise ValueError(f"{path}: el índice la marca finalizada pero el JSON difiere.")
                    write_record(wars, {
                        **metadata, "war_tag": tag, "round_number": round_number,
                        "source_path": path.as_posix(), "payload": payload,
                    })
                    finished_in_group += 1
                    war_count += 1

                complete += finished_in_group == 28

        if not group_count or not war_count:
            raise ValueError("La captura debe contener grupos clasificados y guerras finalizadas.")
        batch.rename(output)

    print(f"Lote generado: {output}")
    print(f"groups.ndjson: {group_count} filas ({complete} grupos con 28 guerras finalizadas)")
    print(f"wars.ndjson:   {war_count} filas")
    print(f"Fuera del lote: {len(manifest.get('archived', {}))} grupos archivados, "
          f"{unclassified} sin clasificar y {unfinished} guerras sin finalizar en grupos clasificados.")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count} grupos")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Directorio de la captura local")
    parser.add_argument("--batch", default="lote_001", help="Nombre del directorio del lote")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch):
        parser.error("--batch solo admite letras, números, guiones y guiones bajos")
    try:
        export(args.root.resolve(), args.batch)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"Error de exportación: {exc}\n")


if __name__ == "__main__":
    main()
