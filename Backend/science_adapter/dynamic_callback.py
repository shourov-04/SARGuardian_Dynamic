"""Bridge the upstream dynamic-AOI batch artifacts to the account job API."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCIENCE_REPOSITORY = "moshiour0/SARGuardian"
SCIENCE_COMMIT = "10639a916c9829850775410b3ed7de62e527770d"


class CallbackError(RuntimeError):
    """A safe workflow integration failure."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CallbackError(f"Required workflow result is unreadable: {path.name}.") from exc
    if not isinstance(value, dict):
        raise CallbackError(f"Required workflow result has an invalid shape: {path.name}.")
    return value


def find_unique(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file()]
    if len(matches) != 1:
        raise CallbackError(f"Expected exactly one {name} result, found {len(matches)}.")
    return matches[0]


def discovery_summary(manifest_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    products = manifest.get("products")
    baseline_counts = manifest.get("baseline_product_counts")
    if not isinstance(products, list) or not isinstance(baseline_counts, dict):
        raise CallbackError("AOI discovery manifest does not contain product counts.")
    eligible = {family: baseline_counts.get(family) for family in ("GUNW", "GOFF")}
    selected = {
        family: sum(isinstance(row, dict) and row.get("product_type") == family for row in products)
        for family in ("GUNW", "GOFF")
    }
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in eligible.values()):
        raise CallbackError("AOI discovery manifest contains invalid eligible counts.")
    return {
        "schema_version": 2,
        "catalog_granule_counts": eligible,
        "selected_granule_counts": selected,
    }


def _artifact_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if name == "aoi-derived-results.zip":
        return "aoi_derived_result_bundle"
    if name == "aoi-run-summary.json":
        return "aoi_run_summary"
    if name == "products.csv":
        return "product_table_csv"
    return {".json": "machine_readable_report", ".csv": "result_table_csv", ".zip": "result_bundle"}.get(suffix, "validated_output")


def success_summary(dashboard_root: Path, github_run_id: str) -> dict[str, Any]:
    summary = _load_json(find_unique(dashboard_root, "aoi-run-summary.json"))
    report = _load_json(find_unique(dashboard_root, "aoi-processing-report.json"))
    receipt = _load_json(find_unique(dashboard_root, "drive-dashboard-upload-receipt.json"))
    manifest = _load_json(find_unique(dashboard_root, "manifest.json"))
    products = report.get("products")
    files = receipt.get("files")
    processing_report = report.get("processing_report")
    manifest_products = manifest.get("products")
    baseline_counts = manifest.get("baseline_product_counts")
    expected = summary.get("products_expected")
    if (summary.get("status") != "PASS" or not isinstance(processing_report, dict)
            or processing_report.get("status") != "PASS" or not isinstance(products, list)
            or not isinstance(manifest_products, list) or not isinstance(expected, int)
            or expected <= 0 or len(products) != expected or len(manifest_products) != expected):
        raise CallbackError("Dynamic-AOI aggregation did not produce a complete PASS result.")
    if (summary.get("aoi_wkt_sha256") != manifest.get("aoi_wkt_sha256")
            or summary.get("science_commit") != manifest.get("science_commit")
            or summary.get("orchestration_sha") != manifest.get("orchestration_sha")
            or processing_report.get("aoi_wkt_sha256") != manifest.get("aoi_wkt_sha256")
            or processing_report.get("science_commit") != manifest.get("science_commit")):
        raise CallbackError("Dynamic-AOI result provenance does not match its selected-product manifest.")
    if (not isinstance(baseline_counts, dict)
            or any(not isinstance(baseline_counts.get(family), int)
                   or isinstance(baseline_counts.get(family), bool)
                   or baseline_counts[family] < 0 for family in ("GUNW", "GOFF"))):
        raise CallbackError("Dynamic-AOI manifest contains invalid catalog product counts.")
    if (receipt.get("remote_verification") != "PASS" or receipt.get("public_read") is not True
            or not isinstance(files, list) or not files):
        raise CallbackError("Dynamic-AOI dashboard files were not verified in public result storage.")
    remote_folder_id = receipt.get("remote_folder_id")
    if (not isinstance(remote_folder_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", remote_folder_id)):
        raise CallbackError("Dynamic-AOI Drive receipt does not identify a valid result folder.")
    if receipt.get("public_folder_url") != f"https://drive.google.com/drive/folders/{remote_folder_id}":
        raise CallbackError("Dynamic-AOI Drive receipt does not verify the public result folder URL.")
    expected_names = {"aoi-derived-results.zip", "aoi-run-summary.json", "products.csv"}
    if {item.get("name") for item in files if isinstance(item, dict)} != expected_names:
        raise CallbackError("Drive receipt does not contain the complete account result package.")
    artifacts: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise CallbackError("Drive receipt contains an invalid file entry.")
        name, digest, size, drive_id = item.get("name"), item.get("sha256"), item.get("bytes"), item.get("id")
        if (not isinstance(name, str) or name not in expected_names
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or not isinstance(drive_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", drive_id)):
            raise CallbackError("Drive receipt contains incomplete verified file metadata.")
        artifacts.append({
            "path": name, "kind": _artifact_kind(name), "sha256": digest,
            "bytes": size, "drive_file_id": drive_id,
        })
    diagnostics = summary.get("science_diagnostics")
    return {
        "schema_version": 2,
        "scientific_status": "PASS",
        "validation": {
            "status": "PASS",
            "scope": "upstream_dynamic_aoi_structural_and_provenance_validation_not_independent_geophysical_validation",
        },
        "storage_verification": {
            "status": "PASS", "provider": "google_drive",
            "remote_folder_id": remote_folder_id,
            "access": "anyone_with_link_reader",
            "public_folder_url": receipt["public_folder_url"],
        },
        "artifacts": artifacts,
        "products": products,
        "catalog_granule_counts": {family: baseline_counts[family] for family in ("GUNW", "GOFF")},
        "selected_granule_counts": {
            family: sum(isinstance(row, dict) and row.get("product_type") == family for row in manifest_products)
            for family in ("GUNW", "GOFF")
        },
        "progress": {"completed": len(products), "total": expected, "failed": 0},
        "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
        "warnings": [
            "Processing and structural validation do not independently validate geophysical accuracy.",
            "Pairwise GUNW/GOFF metrics are not a common-reference displacement time series.",
        ],
        "analysis_type": "upstream_streaming_batch_dynamic_aoi_processing",
        "scientific_limit": summary.get("science_claim_limit") or "Requested-AOI outputs are research products, not a verified hazard warning.",
        "provenance": {
            "science_repository": SCIENCE_REPOSITORY,
            "science_commit": summary.get("science_commit") or SCIENCE_COMMIT,
            "pipeline_version": "moshiour-streaming-dynamic-aoi-v1",
            "orchestration_commit": summary.get("orchestration_sha"),
            "aoi_sha256": summary.get("aoi_wkt_sha256"),
            "github_run_id": github_run_id,
        },
    }


def callback_payload(*, state: str, stage: str, sequence: int, github_run_id: str,
                     github_run_url: str, job_id: str, request_fingerprint: str,
                     result_summary: dict[str, Any] | None = None,
                     error_code: str | None = None, error_message: str | None = None) -> bytes:
    value: dict[str, Any] = {
        "state": state, "stage": stage, "sequence": sequence,
        "github_run_id": github_run_id, "github_run_url": github_run_url,
        "job_id": job_id, "contract_version": 2,
        "request_fingerprint": request_fingerprint,
    }
    if result_summary is not None:
        value["result_summary"] = result_summary
    if error_code:
        value["error_code"] = error_code
    if error_message:
        value["error_message"] = error_message
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def send_callback(callback_url: str, callback_secret: str, job_id: str, body: bytes) -> None:
    signature = "sha256=" + hmac.new(callback_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = Request(
        f"{callback_url.rstrip('/')}/api/displacement-jobs/{job_id}/callback",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-SARGuardian-Signature": signature},
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status < 200 or response.status >= 300:
                raise CallbackError("Job status service rejected the worker callback.")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CallbackError("Could not deliver the signed worker callback.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", choices=("running", "succeeded", "failed"))
    parser.add_argument("--stage")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--callback-url")
    parser.add_argument("--callback-secret")
    parser.add_argument("--job-id")
    parser.add_argument("--request-fingerprint")
    parser.add_argument("--github-run-id")
    parser.add_argument("--github-run-url")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dashboard-root", type=Path)
    parser.add_argument("--result-summary", type=Path)
    parser.add_argument("--build-summary-out", type=Path)
    parser.add_argument("--error-code")
    parser.add_argument("--error-message")
    args = parser.parse_args()
    try:
        if args.build_summary_out:
            if not args.dashboard_root or not args.github_run_id:
                raise CallbackError("Dashboard root and GitHub run ID are required to build a result summary.")
            built = success_summary(args.dashboard_root, args.github_run_id)
            args.build_summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.build_summary_out.write_text(json.dumps(built, separators=(",", ":")), encoding="utf-8")
            return 0
        required = (args.state, args.stage, args.sequence, args.callback_url, args.callback_secret,
                    args.job_id, args.request_fingerprint, args.github_run_id, args.github_run_url)
        if any(value in (None, "") for value in required):
            raise CallbackError("Signed callback arguments are incomplete.")
        result = None
        if args.manifest:
            result = discovery_summary(args.manifest)
        if args.dashboard_root:
            result = success_summary(args.dashboard_root, args.github_run_id)
        if args.result_summary:
            result = _load_json(args.result_summary)
        body = callback_payload(
            state=args.state, stage=args.stage, sequence=args.sequence,
            github_run_id=args.github_run_id, github_run_url=args.github_run_url,
            job_id=args.job_id, request_fingerprint=args.request_fingerprint,
            result_summary=result, error_code=args.error_code, error_message=args.error_message,
        )
        send_callback(args.callback_url, args.callback_secret, args.job_id, body)
    except CallbackError as exc:
        print(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
