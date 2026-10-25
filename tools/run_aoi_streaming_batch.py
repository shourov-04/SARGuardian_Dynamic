#!/usr/bin/env python3
"""Process a dynamic-AOI manifest sequentially in one prepared GitHub runner."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plan_aoi_retries import plan


PRODUCT_ID_RE = re.compile(r"(?:GUNW|GOFF)-[0-9]{3,5}")


def hdf5_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".h5")


def load_products(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("products"), list) or not value["products"]:
        raise ValueError("Manifest contains no products.")
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value["products"]:
        if not isinstance(raw, dict):
            raise ValueError("Manifest product has an invalid shape.")
        product_id = raw.get("id")
        family = raw.get("product_type")
        if not isinstance(product_id, str) or not PRODUCT_ID_RE.fullmatch(product_id) or product_id in seen:
            raise ValueError("Manifest product ID is invalid or duplicated.")
        if family not in {"GUNW", "GOFF"} or not isinstance(raw.get("granule_name"), str):
            raise ValueError("Manifest product identity is invalid.")
        if isinstance(raw.get("expected_source_bytes"), bool) or not isinstance(raw.get("expected_source_bytes"), int):
            raise ValueError("Manifest product size is invalid.")
        seen.add(product_id)
        products.append(raw)
    return value, products


def write_summary(path: Path, *, total: int, completed: list[str], failed: str | None, unprocessed: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "sarguardian-sequential-batch/1.0",
        "created_utc": datetime.now(UTC).isoformat(),
        "status": "PASS" if failed is None else "FAIL",
        "products_total": total,
        "products_completed": len(completed),
        "completed_product_ids": completed,
        "failed_product_id": failed,
        "unprocessed_product_ids": unprocessed,
        "dependency_setup_count": 1,
        "maximum_simultaneous_raw_products": 1,
    }, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--science-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--checkpoint-root", required=True, type=Path)
    parser.add_argument("--retention-profile", required=True, choices=("derived-only", "full-lossless"))
    parser.add_argument("--job-id", default="")
    parser.add_argument("--drive-version-tag", required=True)
    parser.add_argument("--orchestration-sha", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest, products = load_products(args.manifest)
    args.work_root.mkdir(parents=True, exist_ok=True)
    (args.checkpoint_root / "products").mkdir(parents=True, exist_ok=True)
    (args.checkpoint_root / "cleanups").mkdir(parents=True, exist_ok=True)
    completed: list[str] = []
    failed: str | None = None
    unprocessed: list[str] = []
    total = len(products)
    for ordinal, product in enumerate(products, 1):
        product_id = str(product["id"])
        family = str(product["product_type"])
        print(f"::group::Process {product_id} ({ordinal}/{total})", flush=True)
        print(f"BATCH_PRODUCT_START ordinal={ordinal} total={total} product_id={product_id} family={family}", flush=True)
        command = [
            sys.executable, "-u", "tools/process_aoi_product.py",
            "--product-id", product_id,
            "--granule-name", str(product["granule_name"]),
            "--product-type", family,
            "--maturity", str(product["maturity"]),
            "--expected-source-bytes", str(product["expected_source_bytes"]),
            "--retention-profile", args.retention_profile,
            "--job-id", args.job_id,
            "--drive-version-tag", args.drive_version_tag,
            "--orchestration-sha", args.orchestration_sha,
            "--science-root", str(args.science_root),
            "--work-root", str(args.work_root),
            "--checkpoint-root", str(args.checkpoint_root),
            "--aoi-name", "custom",
            "--execution-label", "initial",
        ]
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        result = subprocess.run(command, env=environment, check=False)
        print("::endgroup::", flush=True)
        if result.returncode:
            first_plan = plan(manifest, args.checkpoint_root / "products", args.checkpoint_root / "cleanups")
            decision = next(row for row in first_plan["decisions"] if row["product_id"] == product_id)
            if decision["status"] == "RETRY":
                prior = args.checkpoint_root / "prior" / f"sarguardian-granule-{product_id}"
                current = args.checkpoint_root / "products" / f"sarguardian-granule-{product_id}"
                if prior.exists():
                    shutil.rmtree(prior)
                if current.exists():
                    shutil.copytree(current, prior)
                retry_command = command[:-2] + ["--execution-label", "retry-1", "--prior-checkpoint", str(prior)]
                print(
                    f"BATCH_PRODUCT_RETRY product_id={product_id} attempt=2/2 "
                    f"reasons={','.join(decision['reasons'])}",
                    flush=True,
                )
                result = subprocess.run(retry_command, env=environment, check=False)
            if result.returncode:
                failed = product_id
                unprocessed = [str(item["id"]) for item in products[ordinal:]]
                print(f"BATCH_PRODUCT_FAILED ordinal={ordinal} total={total} product_id={product_id}", flush=True)
                break
        raw_files = hdf5_files(args.work_root / product_id / "raw")
        if raw_files:
            failed = product_id
            unprocessed = [str(item["id"]) for item in products[ordinal:]]
            print(f"ERROR=RAW_PRODUCT_REMAINS_AFTER_VERIFIED_CLEANUP product_id={product_id}", flush=True)
            break
        shutil.rmtree(args.work_root / product_id)
        completed.append(product_id)
        print(f"BATCH_PRODUCT_COMPLETE ordinal={ordinal} total={total} product_id={product_id}", flush=True)
        print(f"BATCH_PROGRESS completed={len(completed)} total={total}", flush=True)

    retry_report = plan(
        manifest,
        args.checkpoint_root / "products",
        args.checkpoint_root / "cleanups",
    )
    (args.checkpoint_root / "retry-plan.json").write_text(
        json.dumps(retry_report, indent=2) + "\n", encoding="utf-8"
    )
    if failed is None and (
        retry_report["products_complete"] != total
        or retry_report["retry_count"]
        or retry_report["terminal_failure_count"]
    ):
        rejected = next(row for row in retry_report["decisions"] if row["status"] != "PASS")
        failed = str(rejected["product_id"])
        print(
            "ERROR=FINAL_RECEIPT_GATE_FAILED "
            f"complete={retry_report['products_complete']}/{total} "
            f"retry_count={retry_report['retry_count']} "
            f"terminal_failure_count={retry_report['terminal_failure_count']}",
            flush=True,
        )
    write_summary(
        args.checkpoint_root / "streaming-summary.json",
        total=total,
        completed=completed,
        failed=failed,
        unprocessed=unprocessed,
    )
    if failed is not None:
        return 1
    print(f"STREAMING_BATCH_STATUS=PASS products={total} dependency_setup_count=1", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
