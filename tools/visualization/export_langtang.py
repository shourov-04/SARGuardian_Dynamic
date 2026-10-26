#!/usr/bin/env python3
"""Export the two verified Langtang GOFF GeoTIFFs as static web-map assets.

The exporter is deliberately downstream of the scientific pipeline. It verifies
the immutable source contract, never edits the TIFFs, preserves native numeric
band values in little-endian binary files, and creates transparent display-only
Web Mercator PNGs. Browser inspection reads the numeric files, never PNG colors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds


SCHEMA_VERSION = "sarguardian-langtang-web-data/1.0"
RUN_ID = "40212069747"
RUN_ATTEMPT = 1
SCIENCE_STATUS = "PASS_WITH_LIMITATIONS"
AOI_WKT = (
    "POLYGON ((85.463902 28.357602, 85.378759 28.208853, "
    "85.610158 28.184949, 85.670584 28.305019, 85.463902 28.357602))"
)
AOI_COORDINATES = [
    [85.463902, 28.357602],
    [85.378759, 28.208853],
    [85.610158, 28.184949],
    [85.670584, 28.305019],
    [85.463902, 28.357602],
]
EXPECTED_CRS = "EPSG:32645"
EXPECTED_PROJ4 = "+proj=utm +zone=45 +datum=WGS84 +units=m +no_defs +type=crs"
EXPECTED_WIDTH = 364
EXPECTED_HEIGHT = 247
EXPECTED_TRANSFORM = (80.0, 0.0, 340720.0, 0.0, -80.0, 3138000.0)
EXPECTED_BOUNDS = (340720.0, 3118240.0, 369840.0, 3138000.0)


@dataclass(frozen=True)
class ProductSpec:
    product_id: str
    source_relative_path: str
    source_sha256: str
    reference_date: str
    secondary_date: str
    track: str = "D048"
    maturity: str = "PROVISIONAL"
    width: int = EXPECTED_WIDTH
    height: int = EXPECTED_HEIGHT
    crs: str = EXPECTED_CRS
    transform: tuple[float, float, float, float, float, float] = EXPECTED_TRANSFORM
    bounds: tuple[float, float, float, float] = EXPECTED_BOUNDS


PRODUCT_SPECS = (
    ProductSpec(
        product_id="GOFF-005",
        source_relative_path="product-data/GOFF-005/GOFF_20260723_20260816_PR_HH-layer2.tif",
        source_sha256="3ed36031d8d5b05e8c14a3c870c579801991e277bff0637164650f8ac062680e",
        reference_date="2026-07-23",
        secondary_date="2026-08-16",
    ),
    ProductSpec(
        product_id="GOFF-009",
        source_relative_path="product-data/GOFF-009/GOFF_20260816_20260828_PR_HH-layer2.tif",
        source_sha256="20aa7c9d412cd16a83e7ab34c9d460a901372d0502ea24f3808b2c188ff7d311",
        reference_date="2026-08-16",
        secondary_date="2026-08-28",
    ),
)

BANDS = (
    {"source_band": 1, "key": "slant_range_offset", "label": "Slant-range offset", "unit": "mm"},
    {"source_band": 2, "key": "along_track_offset", "label": "Along-track offset", "unit": "mm"},
    {"source_band": 3, "key": "correlation", "label": "Correlation", "unit": "unitless"},
)


class ExportContractError(RuntimeError):
    """Raised when source data cannot satisfy the immutable SG-001 contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _close(left: Iterable[float], right: Iterable[float], tolerance: float = 1e-6) -> bool:
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right, strict=True))


def _polygon_coordinates(wkt: object) -> list[list[float]] | None:
    if not isinstance(wkt, str) or not wkt.strip().upper().startswith("POLYGON"):
        return None
    numbers = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", wkt)
    if len(numbers) != len(AOI_COORDINATES) * 2:
        return None
    values = [float(value) for value in numbers]
    return [[values[index], values[index + 1]] for index in range(0, len(values), 2)]


def _source_footprint(transform: Affine, width: int, height: int, crs: str) -> list[list[float]]:
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    edge_samples = 24
    pixel_edges: list[tuple[float, float]] = []
    for value in np.linspace(0, width, edge_samples, endpoint=False):
        pixel_edges.append((float(value), 0.0))
    for value in np.linspace(0, height, edge_samples, endpoint=False):
        pixel_edges.append((float(width), float(value)))
    for value in np.linspace(width, 0, edge_samples, endpoint=False):
        pixel_edges.append((float(value), float(height)))
    for value in np.linspace(height, 0, edge_samples, endpoint=False):
        pixel_edges.append((0.0, float(value)))
    pixel_edges.append(pixel_edges[0])
    points: list[list[float]] = []
    for column, row in pixel_edges:
        x, y = transform @ (column, row)
        longitude, latitude = transformer.transform(x, y)
        points.append([round(float(longitude), 9), round(float(latitude), 9)])
    return points


def _verify_source(source: rasterio.io.DatasetReader, path: Path, spec: ProductSpec) -> None:
    if source.width != spec.width or source.height != spec.height:
        raise ExportContractError(f"{spec.product_id}: expected {spec.width}x{spec.height}, got {source.width}x{source.height}")
    if source.count != 3:
        raise ExportContractError(f"{spec.product_id}: expected exactly three GOFF bands")
    if source.crs is None or source.crs.to_string() != spec.crs:
        raise ExportContractError(f"{spec.product_id}: expected CRS {spec.crs}, got {source.crs}")
    if not _close(tuple(source.transform)[:6], spec.transform):
        raise ExportContractError(f"{spec.product_id}: affine transform does not match the verified contract")
    if not _close(tuple(source.bounds), spec.bounds):
        raise ExportContractError(f"{spec.product_id}: bounds do not match the verified contract")
    if source.nodata is None or not math.isnan(float(source.nodata)):
        raise ExportContractError(f"{spec.product_id}: expected NaN nodata")
    if any(dtype not in {"float32", "float64"} for dtype in source.dtypes):
        raise ExportContractError(f"{spec.product_id}: numeric bands must be floating point")
    actual_hash = sha256(path)
    if actual_hash != spec.source_sha256:
        raise ExportContractError(
            f"{spec.product_id}: source SHA-256 mismatch; expected {spec.source_sha256}, got {actual_hash}"
        )


def _numeric_dtype(dtype: np.dtype[Any]) -> tuple[np.dtype[Any], str, str]:
    if dtype == np.dtype("float32"):
        return np.dtype("<f4"), "float32-le", "f32.bin"
    if dtype == np.dtype("float64"):
        return np.dtype("<f8"), "float64-le", "f64.bin"
    raise ExportContractError(f"Unsupported native numeric dtype: {dtype}")


def _offset_rgba(values: np.ndarray, clip: float) -> np.ndarray:
    valid = np.isfinite(values)
    scaled = np.where(valid, np.clip(values / clip, -1.0, 1.0), 0.0)
    magnitude = np.abs(scaled)[..., None]
    white = np.array([246.0, 248.0, 242.0])
    blue = np.array([34.0, 91.0, 168.0])
    red = np.array([185.0, 48.0, 61.0])
    target = np.where((scaled >= 0)[..., None], red, blue)
    rgb = white + (target - white) * magnitude
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 220, 0).astype(np.uint8)
    return rgba


def _correlation_rgba(values: np.ndarray) -> np.ndarray:
    valid = np.isfinite(values)
    scaled = np.where(valid, np.clip(values, 0.0, 1.0), 0.0)[..., None]
    low = np.array([44.0, 20.0, 76.0])
    high = np.array([244.0, 226.0, 92.0])
    rgb = low + (high - low) * scaled
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 220, 0).astype(np.uint8)
    return rgba


def _preview(
    values: np.ndarray,
    *,
    source_transform: Affine,
    source_crs: str,
    band_key: str,
    offset_clip: float,
    destination: Path,
) -> dict[str, Any]:
    destination_transform, width, height = calculate_default_transform(
        source_crs,
        "EPSG:3857",
        values.shape[1],
        values.shape[0],
        *rasterio.transform.array_bounds(values.shape[0], values.shape[1], source_transform),
    )
    warped = np.full((height, width), np.nan, dtype=np.float32)
    reproject(
        source=values,
        destination=warped,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=np.nan,
        dst_transform=destination_transform,
        dst_crs="EPSG:3857",
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    rgba = _correlation_rgba(warped) if band_key == "correlation" else _offset_rgba(warped, offset_clip)
    Image.fromarray(rgba, mode="RGBA").save(destination, format="PNG", compress_level=9, optimize=False)
    west, south, east, north = rasterio.transform.array_bounds(height, width, destination_transform)
    lon_west, lat_south, lon_east, lat_north = transform_bounds(
        "EPSG:3857", "EPSG:4326", west, south, east, north, densify_pts=21
    )
    return {
        "width": width,
        "height": height,
        "bounds_wgs84": [lon_west, lat_south, lon_east, lat_north],
        "resampling": "nearest",
        "transparent_nodata": True,
    }


def _asset_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "url": path.relative_to(root).as_posix(),
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
    }


def _verified_run_summary(input_root: Path) -> dict[str, Any]:
    path = input_root / "aoi-run-summary.json"
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportContractError("Validated aoi-run-summary.json is missing or unreadable") from exc
    if not isinstance(summary, dict):
        raise ExportContractError("Validated aoi-run-summary.json has an invalid shape")
    diagnostics = summary.get("science_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ExportContractError("Langtang run-summary science diagnostics have an invalid shape")
    pixel_stack = diagnostics.get("pixel_stack")
    candidate_checks = diagnostics.get("candidate_checks")
    if not isinstance(pixel_stack, dict) or not isinstance(candidate_checks, dict):
        raise ExportContractError("Langtang run-summary nested diagnostics have an invalid shape")
    summary_aoi = _polygon_coordinates(summary.get("aoi_wkt"))
    checks = {
        "schema_version": summary.get("schema_version") == "sarguardian-aoi-dashboard-summary/1.0",
        "run_id": str(summary.get("run_id")) == f"{RUN_ID}-{RUN_ATTEMPT}",
        "status": summary.get("status") == "PASS",
        "date_start": summary.get("date_start") == "2026-07-20",
        "date_end": summary.get("date_end") == "2026-08-30",
        "date_filter_mode": summary.get("date_filter_mode") == "secondary-acquisition-in-window",
        "aoi_crs": summary.get("aoi_crs") == "EPSG:4326",
        "aoi_wkt": summary_aoi is not None and all(
            _close(actual, expected, tolerance=1e-9)
            for actual, expected in zip(summary_aoi, AOI_COORDINATES, strict=True)
        ),
        "products_expected": summary.get("products_expected") == 10,
        "products_validated": summary.get("products_validated") == 10,
        "products_with_science_pixels": summary.get("products_with_science_pixels") == 10,
        "gunw_count": summary.get("gunw_count") == 5,
        "goff_count": summary.get("goff_count") == 5,
        "science_status": diagnostics.get("status") == SCIENCE_STATUS,
        "pixel_stack_status": pixel_stack.get("status") == "COMPLETED_NO_DETECTION",
        "candidate_screening_status": candidate_checks.get("status") == "SKIPPED_UNSUPPORTED_CUSTOM_AOI",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ExportContractError("Langtang run-summary contract failed: " + ",".join(failed))
    return summary


def export_dataset(
    input_root: Path,
    output_root: Path,
    *,
    product_specs: tuple[ProductSpec, ...] = PRODUCT_SPECS,
) -> dict[str, Any]:
    if output_root.exists():
        raise ExportContractError(f"Output already exists: {output_root}")
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    run_summary = _verified_run_summary(input_root)
    source_hashes: dict[str, str] = {}
    source_arrays: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    source_metadata: dict[str, dict[str, Any]] = {}

    for spec in product_specs:
        path = (input_root / spec.source_relative_path).resolve()
        try:
            path.relative_to(input_root)
        except ValueError as exc:
            raise ExportContractError(f"{spec.product_id}: source path escapes the input root") from exc
        if not path.is_file():
            raise ExportContractError(f"{spec.product_id}: source TIFF is missing: {path}")
        source_hashes[spec.product_id] = sha256(path)
        with rasterio.open(path) as source:
            _verify_source(source, path, spec)
            arrays = tuple(source.read(index) for index in range(1, 4))
            source_arrays[spec.product_id] = arrays  # type: ignore[assignment]
            source_metadata[spec.product_id] = {
                "transform": source.transform,
                "crs": source.crs.to_string(),
                "dtype": np.dtype(source.dtypes[0]),
                "footprint": _source_footprint(source.transform, source.width, source.height, source.crs.to_string()),
            }

    finite_chunks = [
        np.abs(array[np.isfinite(array)]).astype(np.float64, copy=False)
        for arrays in source_arrays.values()
        for array in arrays[:2]
        if np.any(np.isfinite(array))
    ]
    if not finite_chunks:
        raise ExportContractError("The verified offset bands contain no finite measurements")
    finite_offsets = np.concatenate(finite_chunks)
    offset_clip = max(float(np.percentile(finite_offsets, 98.0)), 1e-6)

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}-", dir=output_root.parent))
    try:
        products: list[dict[str, Any]] = []
        for spec in product_specs:
            product_dir = temporary / spec.product_id
            product_dir.mkdir(parents=True)
            metadata = source_metadata[spec.product_id]
            dtype, dtype_name, extension = _numeric_dtype(metadata["dtype"])
            band_records: list[dict[str, Any]] = []
            for band, array in zip(BANDS, source_arrays[spec.product_id], strict=True):
                numeric_path = product_dir / f"{band['key']}.{extension}"
                numeric_path.write_bytes(array.astype(dtype, copy=False).tobytes(order="C"))
                preview_path = product_dir / f"{band['key']}.png"
                preview = _preview(
                    array.astype(np.float32, copy=False),
                    source_transform=metadata["transform"],
                    source_crs=metadata["crs"],
                    band_key=str(band["key"]),
                    offset_clip=offset_clip,
                    destination=preview_path,
                )
                band_records.append({
                    **band,
                    "numeric_dtype": dtype_name,
                    "numeric": _asset_record(numeric_path, temporary),
                    "preview": {**_asset_record(preview_path, temporary), **preview},
                    "display_range": [0.0, 1.0] if band["key"] == "correlation" else [-offset_clip, offset_clip],
                    "display_palette": "purple-yellow" if band["key"] == "correlation" else "blue-white-red",
                    "display_only": True,
                })
            products.append({
                "id": spec.product_id,
                "family": "GOFF",
                "maturity": spec.maturity,
                "track": spec.track,
                "reference_date": spec.reference_date,
                "secondary_date": spec.secondary_date,
                "pair_semantics": (
                    "pre-event acquisition pair"
                    if spec.product_id == "GOFF-005"
                    else "event-spanning acquisition pair; it does not timestamp change to a single day"
                ),
                "source_tiff": Path(spec.source_relative_path).name,
                "source_sha256": spec.source_sha256,
                "width": spec.width,
                "height": spec.height,
                "crs": spec.crs,
                "proj4": EXPECTED_PROJ4,
                "transform": list(spec.transform),
                "bounds_native": list(spec.bounds),
                "nodata": "NaN",
                "pixel_sampling": "nearest native pixel center after WGS84-to-EPSG:32645 transformation",
                "footprint_wgs84": metadata["footprint"],
                "bands": band_records,
            })

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": "langtang-40212069747-1",
            "source_run_id": RUN_ID,
            "source_run_attempt": RUN_ATTEMPT,
            "processing_status": run_summary["status"],
            "science_status": run_summary["science_diagnostics"]["status"],
            "maturity": "PROVISIONAL",
            "pixel_stack_status": run_summary["science_diagnostics"]["pixel_stack"]["status"],
            "candidate_screening_status": run_summary["science_diagnostics"]["candidate_checks"]["status"],
            "historical_observation": True,
            "hazard_warning": False,
            "aoi": {"crs": "EPSG:4326", "wkt": AOI_WKT, "coordinates": AOI_COORDINATES},
            "offset_display_range_mm": [-offset_clip, offset_clip],
            "offset_display_note": "Display clipping only; numeric inspection preserves the original native value.",
            "products": products,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for spec in product_specs:
            source_path = input_root / spec.source_relative_path
            after = sha256(source_path)
            if after != source_hashes[spec.product_id]:
                raise ExportContractError(f"{spec.product_id}: source TIFF changed during export")
        os.replace(temporary, output_root)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path, help="Extracted validated dashboard artifact root")
    parser.add_argument("--output-root", required=True, type=Path, help="New static web-data directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = export_dataset(args.input_root, args.output_root)
    except ExportContractError as error:
        print(f"LANGTANG_EXPORT_STATUS=FAIL detail={error}", flush=True)
        return 2
    print(
        f"LANGTANG_EXPORT_STATUS=PASS products={len(manifest['products'])} "
        f"output={args.output_root.resolve()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
