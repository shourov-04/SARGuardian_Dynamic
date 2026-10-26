from __future__ import annotations

import csv
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.run_export_science_diagnostics import (
    candidate_export_mask_policy,
    classify_pixel_stack,
    collect_pr_gunw_exports,
    export_identity,
    help_has_exact_option,
    read_candidate_points,
    run_diagnostics,
    write_target_log,
)
from tools.run_pixel_stack_inventory import run_inventory


def test_export_identity_extracts_dates_and_processing_type():
    assert export_identity(Path("GUNW_20251128_20251210_PR.tif")) == (
        "20251128", "20251210", "PR"
    )
    assert export_identity(Path("GUNW_20251128_20251210_UR.tif")) == (
        "20251128", "20251210", "UR"
    )
    assert export_identity(Path("not-a-science-export.tif")) is None


def test_duplicate_export_basenames_are_preserved_in_product_subdirectories(tmp_path: Path):
    exports = tmp_path / "product-data"
    filename = "GUNW_20260922_20261004_PR.tif"
    first = exports / "GUNW-001" / filename
    second = exports / "GUNW-002" / filename
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"ascending-geometry")
    second.write_bytes(b"descending-geometry")

    inventory = collect_pr_gunw_exports(exports, tmp_path / "staged")

    assert inventory["source_files_matched"] == 2
    assert inventory["files_copied"] == 2
    assert inventory["filename_collisions"] == []
    assert inventory["files"] == [
        f"GUNW-001/{filename}", f"GUNW-002/{filename}",
    ]
    assert inventory["duplicate_basenames_preserved"] == [{
        "name": filename,
        "paths": [f"GUNW-001/{filename}", f"GUNW-002/{filename}"],
    }]


def test_pixel_stack_adapter_passes_every_distinct_path_to_pinned_engine(tmp_path: Path):
    filename = "GUNW_20260922_20261004_PR.tif"
    files = [tmp_path / "GUNW-001" / filename, tmp_path / "GUNW-002" / filename]
    for index, path in enumerate(files):
        path.parent.mkdir(parents=True)
        path.write_bytes(str(index).encode())
    parsed: list[Path] = []
    cross_checks: list[dict] = []

    def parse_export(path: Path):
        parsed.append(path)
        return {"path": path, "proc": "PR", "ref": SimpleNamespace(month=9), "geom": path.parent.name}

    engine = SimpleNamespace(
        parse_export=parse_export,
        chain=lambda rasters: [rasters],
        stack=lambda run, remove_common_mode: {"run": run},
        report=lambda _result, _label: False,
        cluster_footprint=lambda _result: None,
        cross_geometry_check=lambda found: cross_checks.append(found),
    )

    assert run_inventory(engine, files, "all") == 0
    assert parsed == sorted(files)
    assert cross_checks == [{}]


def test_diagnostics_accept_duplicate_basenames_without_dropping_inputs(monkeypatch, tmp_path: Path):
    commit = "1" * 40
    science = tmp_path / "science"
    science_src = science / "src"
    science_src.mkdir(parents=True)
    (science_src / "pixel_stack.py").write_text(
        "from datetime import datetime\n"
        "def parse_export(path):\n"
        "    return {'path': path, 'ref': datetime(2026, 9, 22).date(), "
        "'sec': datetime(2026, 10, 4).date(), 'proc': 'PR', 'geom': path.parent.name}\n"
        "def chain(rasters): return [rasters]\n"
        "def stack(run, remove_common_mode=True): return {'run': run}\n"
        "def report(result, label): print('VERDICT: NO DETECTION'); return False\n"
        "def cluster_footprint(result): return None\n"
        "def cross_geometry_check(found): return None\n",
        encoding="utf-8",
    )
    exports = tmp_path / "product-data"
    filename = "GUNW_20260922_20261004_PR.tif"
    for product_id, content in (("GUNW-001", b"a"), ("GUNW-002", b"b")):
        path = exports / product_id / filename
        path.parent.mkdir(parents=True)
        path.write_bytes(content)

    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[:4] == ["git", "-C", str(science.resolve()), "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout=commit + "\n")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("tools.run_export_science_diagnostics.subprocess.run", run)
    report = run_diagnostics(
        science, exports, tmp_path / "diagnostics",
        expected_science_commit=commit,
    )

    assert report["status"] == "PASS"
    assert report["pixel_stack"]["status"] == "COMPLETED_NO_DETECTION"
    assert report["pixel_stack"]["input_count"] == 2
    assert report["export_inventory"]["files_copied"] == 2
    assert report["export_inventory"]["filename_collisions"] == []


def test_candidate_checker_option_detection_does_not_confuse_aoi_csv_with_aoi():
    help_text = """options:
      --target-log TARGET_LOG
      --aoi-csv AOI_CSV
      --lat LAT
      --lon LON
    """
    assert help_has_exact_option(help_text, "--aoi-csv")
    assert not help_has_exact_option(help_text, "--aoi")
    assert not help_has_exact_option(help_text, "--allow-event-affected-aoi")
    assert help_has_exact_option(
        help_text + "      --allow-event-affected-aoi\n",
        "--allow-event-affected-aoi",
    )


def test_pixel_stack_result_is_not_misreported_as_hazard_confirmation():
    assert classify_pixel_stack(0, "VERDICT: NO DETECTION. Cluster is within noise null.") == "COMPLETED_NO_DETECTION"
    assert classify_pixel_stack(0, "cluster exceeds the noise-only 95th percentile") == "COMPLETED_CANDIDATE_REQUIRES_CROSS_CHECK"
    assert classify_pixel_stack(0, "VERDICT: NO DETECTION in chain A; chain B cluster exceeds the noise-only 95th percentile") == "COMPLETED_CANDIDATE_REQUIRES_CROSS_CHECK"
    assert classify_pixel_stack(1, "No contiguous chain of 2+ pairs; nothing to stack.") == "INSUFFICIENT_TEMPORAL_COVERAGE"
    assert classify_pixel_stack(1, "Traceback: invalid raster grid") == "FAIL"


def test_legacy_source_mask_fallback_is_pin_and_provenance_scoped():
    old_pin = "ab547ecc82bcc4d6ccef545a59852536c2beacc6"
    source_tags = {"source": "NISAR_L2_PR_GUNW_example.h5"}

    assert candidate_export_mask_policy(
        source_tags, aoi="source", science_commit=old_pin
    ) == (True, "LEGACY_SOURCE_PIN_WITH_SOURCE_PROVENANCE_TAG")
    assert candidate_export_mask_policy(
        source_tags, aoi="custom", science_commit=old_pin
    ) == (False, "AOI_MASK_TAG_MISSING_FOR_THIS_ENGINE_OR_AOI")
    assert candidate_export_mask_policy(
        {"source": "NISAR_L2_PR_GUNW_example.h5", "aoi_mask_applied": "false"},
        aoi="source", science_commit=old_pin,
    ) == (False, "AOI_MASK_EXPLICITLY_NOT_TRUE")
    assert candidate_export_mask_policy(
        {"aoi_mask_applied": "true"}, aoi="source", science_commit=old_pin
    ) == (False, "SOURCE_PROVENANCE_TAG_MISSING")
    assert candidate_export_mask_policy(
        {"source": "NISAR_L2_PR_GUNW_example.h5"},
        aoi="source", science_commit="10639a916c9829850775410b3ed7de62e527770d",
    ) == (False, "AOI_MASK_TAG_MISSING_FOR_THIS_ENGINE_OR_AOI")


def test_candidate_parser_only_keeps_detachment_like_valid_coordinates(tmp_path: Path):
    from tools.run_export_science_diagnostics import read_candidate_points

    path = tmp_path / "candidates.csv"
    path.write_text(
        "id,lat,lon,reading\n"
        "4,28.27799,85.52983,DETACHMENT-like\n"
        "6,28.28537,85.51250,deposit / flat\n"
        "bad,999,200,DETACHMENT-like\n"
        "5,28.27020,85.51853,DETACHMENT-like\n",
        encoding="utf-8",
    )
    points = read_candidate_points(path)
    assert [x["id"] for x in points] == ["4", "5"]
    assert points[0]["lat"] == 28.27799
    assert points[0]["lon"] == 85.52983


def test_target_log_preserves_candidate_checker_line_contract(tmp_path: Path):
    target = tmp_path / "target.log"
    write_target_log(target, [{
        "reference": "20251125",
        "secondary": "20251207",
        "median_mm": 1.2,
        "valid_pixels": 25,
        "median_coherence": 0.8,
        "raster": "GUNW_20251125_20251207_PR.tif",
    }])
    text = target.read_text(encoding="utf-8")
    assert "2025-11-25 -> 2025-12-07 : 1.2000 mm (25 px, coh 0.8000)" in text
