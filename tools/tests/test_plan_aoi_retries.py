from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_aoi_retries as planner


def _inputs(tmp_path: Path, step_outcomes: dict[str, str], validation_status: str | None = None,
            download_diagnostics: list[str] | None = None):
    product = {
        "id": "GOFF-001",
        "granule_name": "NISAR_L2_PR_GOFF_TEST_PRODUCT",
        "product_type": "GOFF",
        "maturity": "PROVISIONAL",
        "expected_source_bytes": 100,
    }
    manifest = {
        "run_id": "test-run",
        "aoi_wkt_sha256": "test-hash",
        "aoi_name": "custom",
        "science_commit": "science-sha",
        "orchestration_sha": "orchestration-sha",
        "orchestration_branch": "feat/e2e-aoi-wkt-date-dashboard",
        "retention_profile": "derived-only",
        "products": [product],
    }
    product_root = tmp_path / "products"
    cleanup_root = tmp_path / "cleanups"
    artifact = product_root / "sarguardian-granule-GOFF-001"
    artifact.mkdir(parents=True)
    diagnostics = {"product_id": "GOFF-001", "step_outcomes": step_outcomes,
                   "download_diagnostics": download_diagnostics or []}
    (artifact / "product-run-diagnostics.json").write_text(json.dumps(diagnostics), encoding="utf-8")
    if validation_status is not None:
        (artifact / "derived-output-validation.json").write_text(
            json.dumps({"status": validation_status}), encoding="utf-8"
        )
    return manifest, product_root, cleanup_root


def test_reader_failure_after_successful_download_and_aoi_preflight_is_terminal_not_retried(tmp_path):
    manifest, product_root, cleanup_root = _inputs(
        tmp_path,
        {"download": "success", "aoi_preflight": "success", "reader": "failure"},
    )

    result = planner.plan(manifest, product_root, cleanup_root)

    assert result["retry_count"] == 0
    assert result["terminal_failure_count"] == 1
    decision = result["decisions"][0]
    assert decision["status"] == "TERMINAL_FAILURE"
    assert decision["terminal_reason"] == (
        "NON_RETRYABLE_READER_FAILURE_AFTER_SUCCESSFUL_DOWNLOAD_AND_AOI_PREFLIGHT"
    )


def test_download_failure_remains_eligible_for_retry(tmp_path):
    manifest, product_root, cleanup_root = _inputs(
        tmp_path,
        {"download": "failure", "aoi_preflight": "skipped", "reader": "skipped"},
    )

    result = planner.plan(manifest, product_root, cleanup_root)

    assert result["retry_count"] == 1
    assert result["terminal_failure_count"] == 0
    assert result["decisions"][0]["status"] == "RETRY"


def test_non_retryable_download_failure_is_terminal(tmp_path):
    manifest, product_root, cleanup_root = _inputs(
        tmp_path,
        {"download": "failure", "aoi_preflight": "skipped", "reader": "skipped"},
        download_diagnostics=["DOWNLOAD_FAILURE_RETRYABLE=no"],
    )

    result = planner.plan(manifest, product_root, cleanup_root)

    assert result["retry_count"] == 0
    assert result["terminal_failure_count"] == 1
    assert result["decisions"][0]["terminal_reason"] == "NON_RETRYABLE_DOWNLOAD_FAILURE"


def test_structural_validation_failure_after_successful_reader_is_terminal(tmp_path):
    manifest, product_root, cleanup_root = _inputs(
        tmp_path,
        {"download": "success", "aoi_preflight": "success", "reader": "success"},
        validation_status="FAIL",
    )

    result = planner.plan(manifest, product_root, cleanup_root)

    assert result["retry_count"] == 0
    assert result["terminal_failure_count"] == 1
    assert result["decisions"][0]["status"] == "TERMINAL_FAILURE"
    assert result["decisions"][0]["terminal_reason"] == (
        "NON_RETRYABLE_STRUCTURAL_VALIDATION_FAILURE_AFTER_SUCCESSFUL_READER"
    )


def test_retry_manifest_contains_only_retryable_products(tmp_path):
    manifest, product_root, cleanup_root = _inputs(
        tmp_path,
        {"download": "failure", "aoi_preflight": "skipped", "reader": "skipped"},
    )
    manifest["products"].append({
        "id": "GUNW-002",
        "granule_name": "NISAR_L2_PR_GUNW_TEST_PRODUCT",
        "product_type": "GUNW",
        "maturity": "PROVISIONAL",
        "expected_source_bytes": 200,
    })
    terminal = product_root / "sarguardian-granule-GUNW-002"
    terminal.mkdir(parents=True)
    (terminal / "product-run-diagnostics.json").write_text(json.dumps({
        "step_outcomes": {"download": "failure"},
        "download_diagnostics": ["DOWNLOAD_FAILURE_RETRYABLE=no"],
    }), encoding="utf-8")

    result = planner.plan(manifest, product_root, cleanup_root)
    recovery = planner.build_retry_manifest(manifest, result)

    assert [product["id"] for product in recovery["products"]] == ["GOFF-001"]
    assert recovery["recovery_product_count"] == 1
    assert recovery["recovery_source_run_id"] == "test-run"
