from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import process_aoi_product as product_module


def write_zero_valid_goff_export(path: Path, source: str) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=3, width=4, count=3,
        dtype="float32", crs="EPSG:32645", transform=from_origin(1, 4, 1, 1),
        nodata=np.nan,
    ) as dataset:
        for index in range(1, 4):
            dataset.write(np.full((3, 4), np.nan, dtype="float32"), index)
        dataset.update_tags(
            source=source, layer="HH/layer2", reference="20260826", secondary="20260907",
            maturity="PROVISIONAL", crid="P05023", export_status="NO_VALID_PIXELS",
            valid_pixel_count="0",
        )


def args(tmp_path: Path, *, execution_label: str = "initial") -> SimpleNamespace:
    return SimpleNamespace(
        product_id="GUNW-001",
        granule_name="NISAR_L2_PR_GUNW_example_1234567890",
        product_type="GUNW",
        maturity="PROVISIONAL",
        expected_source_bytes=3,
        retention_profile="derived-only",
        execution_label=execution_label,
        science_root=tmp_path / "science",
        work_root=tmp_path / "work",
        checkpoint_root=tmp_path / "checkpoints",
        aoi_name="custom",
        drive_version_tag="run-1",
        job_id="job-1",
        orchestration_sha="a" * 40,
        prior_checkpoint=None,
    )


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def prepare_cleanup_contract(pipeline: product_module.ProductPipeline, *, remote_status: str) -> Path:
    pipeline.raw_dir.mkdir(parents=True, exist_ok=True)
    pipeline.curated.mkdir(parents=True, exist_ok=True)
    raw = pipeline.raw_dir / f"{pipeline.product.granule_name}.h5"
    raw.write_bytes(b"raw")
    pipeline.raw_path = raw
    required_names = [
        "source-inventory.json", "preflight-report.json", "derived-output-validation.json",
        "download-manifest.json", "stats.csv", "map.tif",
    ]
    write_json(pipeline.curated / "product-processing.json", {
        "derived_output_validation_status": "PASS",
        "drive_remote_verification": remote_status,
        "drive_remote_folder_id": "folder-1",
        "stats_csv": "derived/stats.csv",
        "export_files": ["derived/map.tif"],
        "retained_archive_sha256": None,
    })
    write_json(pipeline.curated / "drive-retention-receipt.json", {
        "remote_verification": remote_status,
        "remote_folder_id": "folder-1",
        "files": [{"name": name} for name in required_names],
    })
    write_json(pipeline.curated / "derived-output-validation.json", {"status": "PASS"})
    write_json(pipeline.curated / "download-manifest.json", {"source_sha256": "b" * 64})
    return raw


def test_cleanup_never_deletes_raw_without_remote_verification(tmp_path: Path) -> None:
    pipeline = product_module.ProductPipeline(args(tmp_path))
    raw = prepare_cleanup_contract(pipeline, remote_status="FAIL")

    with pytest.raises(product_module.ProductPipelineError, match="remote verification"):
        pipeline.cleanup()

    assert raw.is_file()


def test_cleanup_deletes_raw_then_uploads_a_verified_receipt(tmp_path: Path) -> None:
    pipeline = product_module.ProductPipeline(args(tmp_path))
    raw = prepare_cleanup_contract(pipeline, remote_status="PASS")

    def uploader(files, output):
        names = [path.name for path in files]
        write_json(output, {
            "remote_verification": "PASS",
            "remote_folder_id": "folder-1",
            "files": [{"name": name} for name in names],
        })

    pipeline._uploader = uploader
    pipeline.cleanup()

    assert not raw.exists()
    cleanup_root = tmp_path / "checkpoints" / "cleanups" / "sarguardian-cleanup-GUNW-001"
    assert (cleanup_root / "local-cleanup-receipt.json").is_file()
    assert (cleanup_root / "drive-cleanup-upload-receipt.json").is_file()


def test_retry_reuses_only_a_raw_file_matching_its_download_manifest(tmp_path: Path, monkeypatch) -> None:
    pipeline = product_module.ProductPipeline(args(tmp_path, execution_label="retry-1"))
    pipeline.raw_dir.mkdir(parents=True)
    raw = pipeline.raw_dir / f"{pipeline.product.granule_name}.h5"
    raw.write_bytes(b"raw")
    digest = hashlib.sha256(b"raw").hexdigest()
    write_json(pipeline.raw_dir / "download-manifest.json", {
        "source_sha256": digest,
        "source_bytes": 3,
    })
    monkeypatch.setattr(product_module, "_run", lambda *_args, **_kwargs: pytest.fail("retry redownloaded the product"))

    pipeline.prepare()
    pipeline.download()

    assert pipeline.raw_path == raw
    assert pipeline.same_source is True
    assert pipeline.outcomes["download"] == "success"


def test_zero_valid_goff_export_gets_an_explicit_non_measurement_stats_row(tmp_path: Path) -> None:
    source = "NISAR_L2_PR_GOFF_example_1234567890.h5"
    raw = tmp_path / source
    raw.write_bytes(b"hdf5-placeholder")
    export = tmp_path / "export" / "GOFF_20260826_20260907_PR_HH-layer2.tif"
    write_zero_valid_goff_export(export, source)
    preflight = tmp_path / "preflight.json"
    write_json(preflight, {
        "grid_checks": [{
            "group": "science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2",
            "grid_pixels_inside_aoi": 155853,
        }],
    })
    stats = tmp_path / "goff.csv"

    assert product_module._write_no_valid_goff_stats(
        raw_path=raw, export_dir=export.parent, stats_path=stats,
        preflight_path=preflight, maturity="PROVISIONAL",
    ) is True
    with stats.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["file"] == source
    assert rows[0]["layer"] == "HH/layer2"
    assert rows[0]["valid_px"] == "0"
    assert rows[0]["aoi_px"] == "155853"
    assert rows[0]["export_status"] == "NO_VALID_PIXELS"
    assert rows[0]["range_median_mm"] == ""
