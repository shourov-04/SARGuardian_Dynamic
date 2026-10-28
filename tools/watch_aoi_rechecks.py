#!/usr/bin/env python3
"""Consume live failed-product artifacts and recheck them on the 11th runner."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from plan_aoi_retries import plan
from run_aoi_streaming_batch import load_products


FAILURE_ARTIFACT_RE = re.compile(
    r"sarguardian-live-failure-(?P<run>[0-9]+)-(?P<attempt>[0-9]+)-(?P<product>(?:GUNW|GOFF)-[0-9]{3,5})"
)


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Artifact contains an unsafe path.")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Artifact contains a symbolic link.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def merge_checkpoints(primary: Path, rechecks: Path, consolidated: Path) -> None:
    if consolidated.exists():
        shutil.rmtree(consolidated)
    consolidated.mkdir(parents=True)
    if primary.is_dir():
        shutil.copytree(primary, consolidated, dirs_exist_ok=True)
    if rechecks.is_dir():
        shutil.copytree(rechecks, consolidated, dirs_exist_ok=True)


class GitHubRun:
    def __init__(self, repository: str, run_id: str, token: str, api_url: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository is invalid.")
        if not run_id.isdigit() or not token:
            raise ValueError("GitHub run identity is invalid.")
        self.base = f"{api_url.rstrip('/')}/repos/{repository}"
        self.run_id = run_id
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SARGuardian-live-recheck",
        })

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(1, 6):
            try:
                response = self.session.request(method, url, timeout=(15, 120), **kwargs)
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 20))
                continue
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 5:
                response.close()
                time.sleep(min(2 ** attempt, 20))
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("GitHub request retry loop exhausted.")

    def artifacts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.request(
                "GET", f"{self.base}/actions/runs/{self.run_id}/artifacts",
                params={"per_page": 100, "page": page},
            )
            payload = response.json()
            response.close()
            batch = payload.get("artifacts", [])
            if not isinstance(batch, list):
                raise ValueError("GitHub artifact response is invalid.")
            rows.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < 100:
                return rows
            page += 1

    def primary_jobs_complete(self) -> bool:
        response = self.request(
            "GET", f"{self.base}/actions/runs/{self.run_id}/jobs",
            params={"filter": "all", "per_page": 100},
        )
        payload = response.json()
        response.close()
        jobs = payload.get("jobs", [])
        primary = [
            job for job in jobs
            if isinstance(job, dict) and "Process AOI shard " in str(job.get("name", ""))
        ]
        return bool(primary) and all(job.get("status") == "completed" for job in primary)

    def download(self, artifact: dict[str, Any], destination: Path) -> None:
        url = artifact.get("archive_download_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Artifact download URL is invalid.")
        response = self.request("GET", url, stream=True)
        try:
            with destination.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    if block:
                        output.write(block)
        finally:
            response.close()


def product_command(args: argparse.Namespace, product: dict[str, Any], prior: Path) -> list[str]:
    return [
        sys.executable, "-u", "tools/process_aoi_product.py",
        "--product-id", str(product["id"]),
        "--granule-name", str(product["granule_name"]),
        "--product-type", str(product["product_type"]),
        "--maturity", str(product["maturity"]),
        "--expected-source-bytes", str(product["expected_source_bytes"]),
        "--retention-profile", args.retention_profile,
        "--job-id", args.job_id,
        "--drive-version-tag", args.drive_version_tag,
        "--orchestration-sha", args.orchestration_sha,
        "--science-root", str(args.science_root),
        "--work-root", str(args.work_root),
        "--checkpoint-root", str(args.recheck_root),
        "--aoi-name", "custom",
        "--execution-label", "retry-1",
        "--prior-checkpoint", str(prior),
    ]


def recheck_product(
    args: argparse.Namespace,
    product: dict[str, Any],
    prior_source: Path,
) -> bool:
    product_id = str(product["id"])
    prior = args.recheck_root / "prior" / f"sarguardian-granule-{product_id}"
    if prior.exists():
        shutil.rmtree(prior)
    if prior_source.is_dir():
        shutil.copytree(prior_source, prior)
    else:
        prior.mkdir(parents=True)
    shutil.rmtree(args.work_root / product_id, ignore_errors=True)
    print(f"LIVE_RECHECK_START product_id={product_id}", flush=True)
    result = subprocess.run(product_command(args, product, prior), check=False)
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"LIVE_RECHECK_COMPLETE product_id={product_id} status={status}", flush=True)
    return result.returncode == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--science-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--incoming-root", required=True, type=Path)
    parser.add_argument("--primary-root", required=True, type=Path)
    parser.add_argument("--recheck-root", required=True, type=Path)
    parser.add_argument("--consolidated-root", required=True, type=Path)
    parser.add_argument("--retention-profile", required=True, choices=("derived-only", "full-lossless"))
    parser.add_argument("--job-id", default="")
    parser.add_argument("--drive-version-tag", required=True)
    parser.add_argument("--orchestration-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=int, default=20_100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest, products = load_products(args.manifest)
    by_id = {str(product["id"]): product for product in products}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    github = GitHubRun(args.repository, args.run_id, token, args.api_url)
    for root in (
        args.work_root, args.incoming_root, args.primary_root,
        args.recheck_root, args.consolidated_root,
    ):
        root.mkdir(parents=True, exist_ok=True)

    prefix = f"sarguardian-live-failure-{args.run_id}-{args.run_attempt}-"
    seen: set[int] = set()
    attempted: set[str] = set()
    deadline = time.monotonic() + args.timeout_seconds
    completed_polls = 0

    while time.monotonic() < deadline:
        discovered = github.artifacts()
        new_failures = [
            row for row in discovered
            if str(row.get("name", "")).startswith(prefix)
            and isinstance(row.get("id"), int)
            and row["id"] not in seen
            and not row.get("expired")
        ]
        for artifact in sorted(new_failures, key=lambda row: int(row["id"])):
            match = FAILURE_ARTIFACT_RE.fullmatch(str(artifact["name"]))
            if not match or match.group("run") != args.run_id or match.group("attempt") != args.run_attempt:
                continue
            product_id = match.group("product")
            if product_id not in by_id:
                raise ValueError(f"Failure artifact names unknown product {product_id}.")
            artifact_id = int(artifact["id"])
            archive = args.incoming_root / f"failure-{artifact_id}.zip"
            extracted = args.incoming_root / f"failure-{artifact_id}"
            github.download(artifact, archive)
            safe_extract(archive, extracted)
            prior = extracted / "products" / f"sarguardian-granule-{product_id}"
            seen.add(artifact_id)
            attempted.add(product_id)
            recheck_product(args, by_id[product_id], prior)

        if github.primary_jobs_complete():
            completed_polls = completed_polls + 1 if not new_failures else 0
            if completed_polls >= 3:
                break
        else:
            completed_polls = 0
        time.sleep(max(0.1, args.poll_seconds))
    else:
        print("ERROR=LIVE_RECHECK_WATCHER_TIMEOUT", flush=True)
        return 2

    # After all primaries finish, collect their immutable shard artifacts as a
    # safety net for a runner crash or a live-signal upload failure.
    for artifact in github.artifacts():
        name = str(artifact.get("name", ""))
        artifact_id = artifact.get("id")
        if not name.startswith("sarguardian-stream-products-") or not isinstance(artifact_id, int):
            continue
        archive = args.incoming_root / f"primary-{artifact_id}.zip"
        github.download(artifact, archive)
        safe_extract(archive, args.primary_root)

    merge_checkpoints(args.primary_root, args.recheck_root, args.consolidated_root)
    initial = plan(
        manifest,
        args.consolidated_root / "products",
        args.consolidated_root / "cleanups",
    )
    for decision in initial["decisions"]:
        product_id = str(decision["product_id"])
        if decision["status"] != "RETRY" or product_id in attempted:
            continue
        attempted.add(product_id)
        prior = args.consolidated_root / "products" / f"sarguardian-granule-{product_id}"
        recheck_product(args, by_id[product_id], prior)
    merge_checkpoints(args.primary_root, args.recheck_root, args.consolidated_root)

    recovery_manifest = dict(manifest)
    recovery_manifest["recovery_source_run_id"] = manifest.get("run_id")
    final = plan(
        recovery_manifest,
        args.consolidated_root / "products",
        args.consolidated_root / "cleanups",
    )
    summary = {
        "schema_version": "sarguardian-live-recheck/1.0",
        "created_utc": datetime.now(UTC).isoformat(),
        "status": "PASS" if final["products_complete"] == len(products) else "FAIL",
        "products_expected": len(products),
        "products_complete": final["products_complete"],
        "live_failure_artifacts_consumed": len(seen),
        "rechecked_product_ids": sorted(attempted),
        "terminal_failure_count": final["terminal_failure_count"],
        "decisions": final["decisions"],
    }
    (args.consolidated_root / "live-recheck-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"LIVE_RECHECK_STATUS={summary['status']} complete={final['products_complete']}/{len(products)} "
        f"rechecked={len(attempted)}",
        flush=True,
    )
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
