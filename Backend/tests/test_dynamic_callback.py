from __future__ import annotations

import hashlib
import json
from pathlib import Path

from science_adapter.dynamic_callback import callback_payload, discovery_summary, success_summary


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_discovery_summary_reports_eligible_and_selected_family_counts(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {
        "baseline_product_counts": {"GUNW": 8, "GOFF": 5},
        "products": [{"product_type": "GUNW"}, {"product_type": "GOFF"}],
    })
    assert discovery_summary(manifest) == {
        "schema_version": 2,
        "catalog_granule_counts": {"GUNW": 8, "GOFF": 5},
        "selected_granule_counts": {"GUNW": 1, "GOFF": 1},
    }


def test_upstream_dashboard_and_drive_receipt_build_version_two_result(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard"
    write_json(dashboard / "aoi-run-summary.json", {
        "status": "PASS", "products_expected": 2, "gunw_count": 1, "goff_count": 1,
        "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        "orchestration_sha": "a" * 40, "aoi_wkt_sha256": "b" * 64,
        "science_diagnostics": {"pixel_stack": {"status": "PASS"}},
    })
    products = [
        {"product_type": "GUNW", "granule_name": "NISAR_GUNW", "status": "PASS"},
        {"product_type": "GOFF", "granule_name": "NISAR_GOFF", "status": "PASS"},
    ]
    write_json(dashboard / "aoi-processing-report.json", {
        "products": products,
        "processing_report": {
            "status": "PASS", "aoi_wkt_sha256": "b" * 64,
            "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        },
    })
    write_json(tmp_path / "manifest" / "manifest.json", {
        "aoi_wkt_sha256": "b" * 64,
        "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        "orchestration_sha": "a" * 40,
        "baseline_product_counts": {"GUNW": 8, "GOFF": 5},
        "products": products,
    })
    content = b"verified bundle"
    write_json(tmp_path / "storage" / "drive-dashboard-upload-receipt.json", {
        "remote_verification": "PASS", "remote_folder_id": "public-folder-id",
        "public_read": True,
        "public_folder_url": "https://drive.google.com/drive/folders/public-folder-id",
        "files": [
            {"id": f"private-file-id-{index}", "name": name,
             "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for index, name in enumerate(("aoi-derived-results.zip", "aoi-run-summary.json", "products.csv"))
        ],
    })

    result = success_summary(tmp_path, "12345")

    assert result["schema_version"] == 2
    assert result["validation"]["status"] == "PASS"
    assert result["storage_verification"]["status"] == "PASS"
    assert result["products"] == products
    assert result["artifacts"][0]["drive_file_id"] == "private-file-id-0"
    assert result["storage_verification"]["access"] == "anyone_with_link_reader"
    assert result["catalog_granule_counts"] == {"GUNW": 8, "GOFF": 5}
    assert result["selected_granule_counts"] == {"GUNW": 1, "GOFF": 1}
    assert result["provenance"]["github_run_id"] == "12345"


def test_callback_payload_is_deterministic_and_bound_to_the_job() -> None:
    payload = callback_payload(
        state="running", stage="products_discovered", sequence=2, github_run_id="123",
        github_run_url="https://github.test/run/123", job_id="11111111-1111-1111-1111-111111111111",
        request_fingerprint="a" * 64, result_summary={"schema_version": 2},
    )
    decoded = json.loads(payload)
    assert decoded["contract_version"] == 2
    assert decoded["job_id"] == "11111111-1111-1111-1111-111111111111"
    assert decoded["result_summary"] == {"schema_version": 2}
