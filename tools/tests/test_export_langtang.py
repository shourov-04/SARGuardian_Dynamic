from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import Affine, array_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visualization.export_langtang import ExportContractError, ProductSpec, export_dataset


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_product(path: Path, *, shift: float) -> tuple[np.ndarray, ...]:
    transform = Affine(80, 0, 340720, 0, -80, 3138000)
    rows, columns = 6, 8
    range_offset = np.arange(rows * columns, dtype=np.float32).reshape(rows, columns) + shift
    along_track = -range_offset.copy()
    correlation = np.full((rows, columns), 0.82 + shift / 100, dtype=np.float32)
    range_offset[0, 0] = np.nan
    along_track[0, 0] = np.nan
    correlation[0, 0] = np.nan
    range_offset[1, 1] = 0.0
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", width=columns, height=rows, count=3,
        dtype="float32", crs="EPSG:32645", transform=transform, nodata=np.nan,
    ) as destination:
        destination.write(range_offset, 1)
        destination.write(along_track, 2)
        destination.write(correlation, 3)
    return range_offset, along_track, correlation


def write_summary(root: Path) -> None:
    (root / "aoi-run-summary.json").write_text(json.dumps({
        "schema_version": "sarguardian-aoi-dashboard-summary/1.0",
        "run_id": "40212069747-1", "status": "PASS",
        "date_start": "2026-07-20", "date_end": "2026-08-30",
        "date_filter_mode": "secondary-acquisition-in-window",
        "aoi_crs": "EPSG:4326",
        "aoi_wkt": (
            "POLYGON ((85.463902 28.357602, 85.378759 28.208853, "
            "85.610158 28.184949, 85.670584 28.305019, 85.463902 28.357602))"
        ),
        "products_expected": 10, "products_validated": 10,
        "products_with_science_pixels": 10, "gunw_count": 5, "goff_count": 5,
        "science_diagnostics": {
            "status": "PASS_WITH_LIMITATIONS",
            "pixel_stack": {"status": "COMPLETED_NO_DETECTION"},
            "candidate_checks": {"status": "SKIPPED_UNSUPPORTED_CUSTOM_AOI"},
        },
    }), encoding="utf-8")


def specs(root: Path) -> tuple[ProductSpec, ProductSpec]:
    transform = (80.0, 0.0, 340720.0, 0.0, -80.0, 3138000.0)
    bounds = tuple(float(value) for value in array_bounds(6, 8, Affine(*transform)))
    first = root / "first.tif"
    second = root / "second.tif"
    return (
        ProductSpec("GOFF-005", "first.tif", digest(first), "2026-07-23", "2026-08-16", width=8, height=6, transform=transform, bounds=bounds),
        ProductSpec("GOFF-009", "second.tif", digest(second), "2026-08-16", "2026-08-28", width=8, height=6, transform=transform, bounds=bounds),
    )


def test_export_is_deterministic_preserves_sources_and_keeps_numeric_values(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    first_arrays = write_product(input_root / "first.tif", shift=0)
    write_product(input_root / "second.tif", shift=4)
    write_summary(input_root)
    source_hashes = {path.name: digest(path) for path in input_root.glob("*.tif")}
    product_specs = specs(input_root)

    first_output = tmp_path / "web-one"
    second_output = tmp_path / "web-two"
    manifest = export_dataset(input_root, first_output, product_specs=product_specs)
    export_dataset(input_root, second_output, product_specs=product_specs)

    assert {path.name: digest(path) for path in input_root.glob("*.tif")} == source_hashes
    assert (first_output / "manifest.json").read_bytes() == (second_output / "manifest.json").read_bytes()
    assert manifest["offset_display_range_mm"][0] == -manifest["offset_display_range_mm"][1]
    ranges = {
        tuple(band["display_range"])
        for product in manifest["products"]
        for band in product["bands"][:2]
    }
    assert ranges == {tuple(manifest["offset_display_range_mm"])}

    numeric = np.fromfile(first_output / "GOFF-005" / "slant_range_offset.f32.bin", dtype="<f4").reshape(6, 8)
    np.testing.assert_array_equal(numeric, first_arrays[0])
    assert numeric[1, 1] == 0.0
    assert np.isnan(numeric[0, 0])

    preview = np.asarray(Image.open(first_output / "GOFF-005" / "slant_range_offset.png"))
    assert preview.shape[2] == 4
    assert preview[..., 3].min() == 0
    assert preview[..., 3].max() > 0
    parsed = json.loads((first_output / "manifest.json").read_text(encoding="utf-8"))
    assert parsed["products"][0]["pixel_sampling"].startswith("nearest native pixel center")


def test_export_rejects_an_unverified_source_hash_without_creating_output(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    write_product(input_root / "first.tif", shift=0)
    write_product(input_root / "second.tif", shift=4)
    write_summary(input_root)
    first, second = specs(input_root)
    tampered = ProductSpec(
        first.product_id, first.source_relative_path, "0" * 64,
        first.reference_date, first.secondary_date,
        width=first.width, height=first.height, transform=first.transform, bounds=first.bounds,
    )
    output = tmp_path / "web"

    with pytest.raises(ExportContractError, match="SHA-256 mismatch"):
        export_dataset(input_root, output, product_specs=(tampered, second))

    assert not output.exists()
