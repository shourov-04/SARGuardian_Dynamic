#!/usr/bin/env python3
"""Run the pinned pixel-stack implementation over an explicit raster inventory.

The upstream CLI scans one flat directory, where distinct geometries can have
the same date-derived basename. This adapter changes only input enumeration;
all parsing, alignment, stacking, reporting and cross-geometry checks execute
through the pinned science module itself.
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any


WINTER_MONTHS = (11, 12, 1, 2, 3)


def geometry_partition(source: str, fallback: str) -> str:
    """Keep adjacent NISAR frame/subswath grids out of the same time chain."""
    match = re.search(r"_GUNW_\d+_(\d+)_([AD])_(\d+)_", source)
    if not match:
        return fallback
    return f"{match.group(2)}{int(match.group(1)):03d}-F{int(match.group(3)):03d}"


def partition_raster_geometry(row: dict[str, Any]) -> dict[str, Any]:
    try:
        import rasterio
        with rasterio.open(row["path"]) as dataset:
            source = dataset.tags().get("source", "")
        return {**row, "geom": geometry_partition(source, str(row.get("geom", "?")))}
    except Exception:
        return row


def load_inventory(path: Path) -> list[Path]:
    value = json.loads(path.read_text(encoding="utf-8"))
    raw_files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("Pixel-stack inventory contains no files.")
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_files:
        if not isinstance(raw, str):
            raise ValueError("Pixel-stack inventory has an invalid path.")
        candidate = Path(raw).resolve()
        if candidate in seen or not candidate.is_file() or candidate.suffix.lower() != ".tif":
            raise ValueError("Pixel-stack inventory file is missing, duplicated or invalid.")
        seen.add(candidate)
        files.append(candidate)
    return files


def run_inventory(pixel_stack: Any, files: list[Path], season: str) -> int:
    rasters = [partition_raster_geometry(row) for row in
               (pixel_stack.parse_export(path) for path in sorted(files)) if row]
    rasters = [row for row in rasters if row["proc"] == "PR"]
    if season != "all":
        want_winter = season == "winter"
        rasters = [row for row in rasters if (row["ref"].month in WINTER_MONTHS) == want_winter]
    if not rasters:
        print(f"No {season} rasters in the explicit inventory.", file=sys.stderr, flush=True)
        return 1

    print(f"PIXEL_STACK_INVENTORY_COUNT={len(rasters)} season={season}", flush=True)
    runs = [run for run in pixel_stack.chain(rasters) if len(run) >= 2]
    if not runs:
        print("No contiguous chain of 2+ pairs; nothing to stack.", file=sys.stderr, flush=True)
        return 1

    found: dict[str, Any] = {}
    for index, run in enumerate(runs, 1):
        result = pixel_stack.stack(run, remove_common_mode=True)
        survived = pixel_stack.report(result, f"{run[0]['geom']}  chain {index}")
        footprint = pixel_stack.cluster_footprint(result)
        if footprint and survived:
            found.setdefault(run[0]["geom"], footprint)
    pixel_stack.cross_geometry_check(found)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--science-root", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--season", choices=("winter", "summer", "all"), default="all")
    args = parser.parse_args()
    science_src = (args.science_root.resolve() / "src")
    if not (science_src / "pixel_stack.py").is_file():
        raise SystemExit("Pinned pixel_stack.py is missing.")
    sys.path.insert(0, str(science_src))
    pixel_stack = importlib.import_module("pixel_stack")
    return run_inventory(pixel_stack, load_inventory(args.inventory), args.season)


if __name__ == "__main__":
    raise SystemExit(main())
