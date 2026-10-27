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


def test_recovery_batch_continues_after_a_product_still_fails(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest(manifest_path)
    calls: list[str] = []

    def run(command, **_kwargs):
        product_id = command[command.index("--product-id") + 1]
        calls.append(product_id)
        (tmp_path / "work" / product_id).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=1 if product_id == "GUNW-001" else 0)

    def recovery_plan(manifest_value, _product_root, _cleanup_root):
        decisions = [{
            "product_id": product["id"],
            "status": "RETRY" if product["id"] == "GUNW-001" else "PASS",
            "reasons": ["MISSING_PRODUCT_CHECKPOINT"] if product["id"] == "GUNW-001" else [],
        } for product in manifest_value["products"]]
        return {
            "products_complete": 1, "retry_count": 1, "terminal_failure_count": 0,
            "retry_matrix": [], "decisions": decisions,
        }

    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "plan", recovery_plan)
    monkeypatch.setattr(batch.sys, "argv", argv(tmp_path, manifest_path) + ["--continue-after-failure"])

    assert batch.main() == 1
    assert calls == ["GUNW-001", "GUNW-001", "GOFF-002"]
    summary = json.loads((tmp_path / "checkpoints" / "streaming-summary.json").read_text(encoding="utf-8"))
    assert summary["failed_product_id"] == "GUNW-001"
    assert summary["unprocessed_product_ids"] == []


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


def test_twenty_products_spread_the_remainder_across_runners() -> None:
    products = [{"id": f"GOFF-{index:03d}", "expected_source_bytes": 10} for index in range(1, 21)]

    shards = [batch.product_shard(products, index, 6) for index in range(6)]

    assert [len(shard) for shard in shards] == [4, 4, 3, 3, 3, 3]
    assert len({row["id"] for shard in shards for row in shard}) == 20


def test_product_shard_rejects_unbounded_runner_count() -> None:
    try:
        batch.product_shard([{"id": "GOFF-001"}], 0, 11)
    except ValueError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("an unbounded processing shard count was accepted")


def test_sharded_batch_processes_and_validates_only_its_assigned_products(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest(manifest_path)
    calls: list[list[str]] = []
    planned_ids: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        product_id = command[command.index("--product-id") + 1]
        (tmp_path / "work" / product_id / "raw").mkdir(parents=True)
        return SimpleNamespace(returncode=0)

    def shard_plan(manifest_value, _product_root, _cleanup_root):
        products = manifest_value["products"]
        planned_ids.append([row["id"] for row in products])
        return {
            "products_complete": len(products), "retry_count": 0, "terminal_failure_count": 0,
            "retry_matrix": [], "decisions": [
                {"product_id": row["id"], "status": "PASS"} for row in products
            ],
        }

    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "plan", shard_plan)
    monkeypatch.setattr(batch.sys, "argv", argv(tmp_path, manifest_path) + [
        "--shard-index", "1", "--shard-count", "2",
    ])

    assert batch.main() == 0
    assert [call[call.index("--product-id") + 1] for call in calls] == ["GOFF-002"]
    assert planned_ids == [["GOFF-002"]]
    summary = json.loads(
        (tmp_path / "checkpoints" / "streaming-summary-shard-2-of-2.json").read_text(encoding="utf-8")
    )
    assert summary["shard_index"] == 1
    assert summary["shard_count"] == 2


def test_thirteen_mixed_size_products_use_four_balanced_runners() -> None:
    products = [
        {"id": f"GOFF-{index:03d}", "expected_source_bytes": 1_100_000_000}
        for index in range(1, 6)
    ] + [
        {"id": f"GUNW-{index:03d}", "expected_source_bytes": 2_300_000_000}
        for index in range(6, 14)
    ]

    shards = [batch.product_shard(products, index, 4) for index in range(4)]

    assert [len(shard) for shard in shards] == [4, 3, 3, 3]
    assert len({row["id"] for shard in shards for row in shard}) == 13


def test_thirty_five_products_are_evenly_split_across_ten_runners() -> None:
    products = [
        {"id": f"GUNW-{index:03d}", "expected_source_bytes": 2_300_000_000}
        for index in range(1, 36)
    ]

    shards = [batch.product_shard(products, index, 10) for index in range(10)]

    assert [len(shard) for shard in shards] == [4, 4, 4, 4, 4, 3, 3, 3, 3, 3]
    assert len({row["id"] for shard in shards for row in shard}) == 35


def test_eighty_products_are_split_eight_per_ten_runners() -> None:
    products = [
        {"id": f"GUNW-{index:03d}", "expected_source_bytes": 2_300_000_000}
        for index in range(1, 81)
    ]

    shards = [batch.product_shard(products, index, 10) for index in range(10)]

    assert [len(shard) for shard in shards] == [8] * 10
    assert len({row["id"] for shard in shards for row in shard}) == 80
