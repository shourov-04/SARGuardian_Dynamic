from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_aoi_streaming_batch as batch


def manifest(path: Path) -> None:
    path.write_text(json.dumps({
        "products": [
            {"id": "GUNW-001", "granule_name": "NISAR_L2_PR_GUNW_example_1234567890", "product_type": "GUNW", "maturity": "PROVISIONAL", "expected_source_bytes": 10},
            {"id": "GOFF-002", "granule_name": "NISAR_L2_PR_GOFF_example_1234567890", "product_type": "GOFF", "maturity": "BETA", "expected_source_bytes": 11},
        ]
    }), encoding="utf-8")


def argv(tmp_path: Path, manifest_path: Path) -> list[str]:
    return [
        "run_aoi_streaming_batch.py", "--manifest", str(manifest_path),
        "--science-root", str(tmp_path / "science"), "--work-root", str(tmp_path / "work"),
        "--checkpoint-root", str(tmp_path / "checkpoints"), "--retention-profile", "derived-only",
        "--job-id", "job-1", "--drive-version-tag", "run-1", "--orchestration-sha", "a" * 40,
    ]


def complete_plan(manifest_value, product_root, cleanup_root):
    products = manifest_value["products"]
    return {
        "products_complete": len(products), "retry_count": 0, "terminal_failure_count": 0,
        "retry_matrix": [], "decisions": [{"product_id": row["id"], "status": "PASS"} for row in products],
    }


def test_batch_runs_products_in_manifest_order_and_reuses_one_prepared_process(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest(manifest_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        product_id = command[command.index("--product-id") + 1]
        (tmp_path / "work" / product_id / "raw").mkdir(parents=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "plan", complete_plan)
    monkeypatch.setattr(batch.sys, "argv", argv(tmp_path, manifest_path))

    assert batch.main() == 0
    assert [call[call.index("--product-id") + 1] for call in calls] == ["GUNW-001", "GOFF-002"]
    summary = json.loads((tmp_path / "checkpoints" / "streaming-summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["dependency_setup_count"] == 1
    assert summary["maximum_simultaneous_raw_products"] == 1


def test_batch_stops_before_the_next_download_when_a_product_fails(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest(manifest_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "plan", lambda *_args: {
        "products_complete": 0, "retry_count": 1, "terminal_failure_count": 1,
        "retry_matrix": [], "decisions": [
            {"product_id": "GUNW-001", "status": "TERMINAL_FAILURE", "reasons": ["INVALID_AOI"]},
            {"product_id": "GOFF-002", "status": "RETRY", "reasons": ["MISSING_PRODUCT_CHECKPOINT"]},
        ],
    })
    monkeypatch.setattr(batch.sys, "argv", argv(tmp_path, manifest_path))

    assert batch.main() == 1
    assert len(calls) == 1
    summary = json.loads((tmp_path / "checkpoints" / "streaming-summary.json").read_text(encoding="utf-8"))
    assert summary["failed_product_id"] == "GUNW-001"
    assert summary["unprocessed_product_ids"] == ["GOFF-002"]


def test_batch_retries_one_retryable_product_once_in_the_same_runner(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest(manifest_path)
    calls: list[list[str]] = []
    plans = 0

    def run(command, **_kwargs):
        calls.append(command)
        product_id = command[command.index("--product-id") + 1]
        (tmp_path / "work" / product_id / "raw").mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    def retry_then_complete(manifest_value, product_root, cleanup_root):
        nonlocal plans
        plans += 1
        if plans == 1:
            return {
                "products_complete": 0, "retry_count": 2, "terminal_failure_count": 0,
                "retry_matrix": [], "decisions": [
                    {"product_id": "GUNW-001", "status": "RETRY", "reasons": ["MISSING_CLEANUP_RECEIPT"]},
                    {"product_id": "GOFF-002", "status": "RETRY", "reasons": ["MISSING_PRODUCT_CHECKPOINT"]},
                ],
            }
        return complete_plan(manifest_value, product_root, cleanup_root)

    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "plan", retry_then_complete)
    monkeypatch.setattr(batch.sys, "argv", argv(tmp_path, manifest_path))

    assert batch.main() == 0
    assert [call[call.index("--product-id") + 1] for call in calls] == ["GUNW-001", "GUNW-001", "GOFF-002"]
    assert [call[call.index("--execution-label") + 1] for call in calls] == ["initial", "retry-1", "initial"]


def test_manifest_rejects_duplicate_product_ids(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["products"][1]["id"] = "GUNW-001"
    path.write_text(json.dumps(value), encoding="utf-8")

    try:
        batch.load_products(path)
    except ValueError as error:
        assert "duplicated" in str(error)
    else:
        raise AssertionError("duplicate product IDs were accepted")
