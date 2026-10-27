#!/usr/bin/env python3
"""Build a retry-only matrix for the dynamic WKT AOI workflow from artifact receipts."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else None
    except (OSError,ValueError,UnicodeDecodeError):
        return None

def plan(manifest: dict[str, Any], product_root: Path, cleanup_root: Path) -> dict[str, Any]:
    retry=[]; decisions=[]
    for product in manifest.get("products",[]):
        pid=str(product.get("id",""))
        family=str(product.get("product_type",""))
        granule=str(product.get("granule_name",""))
        artifact=product_root/("sarguardian-granule-"+pid)
        cleanup=cleanup_root/("sarguardian-cleanup-"+pid)
        meta=read_json(artifact/"product-processing.json")
        validation=read_json(artifact/"derived-output-validation.json")
        download=read_json(artifact/"download-manifest.json")
        retention=read_json(artifact/"drive-retention-receipt.json")
        diagnostics=read_json(artifact/"product-run-diagnostics.json")
        outcomes=(diagnostics or {}).get("step_outcomes", {})
        local_cleanup=read_json(cleanup/"local-cleanup-receipt.json")
        cleanup_upload=read_json(cleanup/"drive-cleanup-upload-receipt.json")
        reasons=[]
        terminal_reason=None
        download_diagnostics=(diagnostics or {}).get("download_diagnostics", [])
        permanent_download_failure=(
            outcomes.get("download") == "failure"
            and isinstance(download_diagnostics, list)
            and "DOWNLOAD_FAILURE_RETRYABLE=no" in download_diagnostics
        )
        # Repeating a deterministic local failure with the same source bytes and
        # the same code revision cannot repair it. Retry only acquisition/retention
        # failures or missing checkpoints without a known terminal processing cause.
        if permanent_download_failure:
            terminal_reason="NON_RETRYABLE_DOWNLOAD_FAILURE"
        elif (outcomes.get("download") == "success"
                and outcomes.get("aoi_preflight") == "success"
                and outcomes.get("reader") == "failure"):
            terminal_reason="NON_RETRYABLE_READER_FAILURE_AFTER_SUCCESSFUL_DOWNLOAD_AND_AOI_PREFLIGHT"
        elif (outcomes.get("download") == "success"
                and outcomes.get("aoi_preflight") == "failure"):
            terminal_reason="NON_RETRYABLE_AOI_PREFLIGHT_FAILURE_AFTER_SUCCESSFUL_DOWNLOAD"
        elif (outcomes.get("reader") == "success" and validation and validation.get("status") == "FAIL"):
            terminal_reason="NON_RETRYABLE_STRUCTURAL_VALIDATION_FAILURE_AFTER_SUCCESSFUL_READER"
        want_aoi=manifest.get("aoi_name")
        want_sha=manifest.get("science_commit")
        want_profile=manifest.get("retention_profile")
        if not meta: reasons.append("PRODUCT_PROCESSING_RECEIPT_MISSING")
        else:
            for key,expected in (("id",pid),("product_type",family),("granule_name",granule),
                                 ("aoi",want_aoi),("science_commit",want_sha),
                                 ("orchestration_sha",manifest.get("orchestration_sha")),
                                 ("orchestration_branch",manifest.get("orchestration_branch")),
                                 ("retention_profile",want_profile),
                                 ("derived_output_validation_status","PASS"),("drive_remote_verification","PASS")):
                if meta.get(key)!=expected: reasons.append("META_"+key.upper()+"_INVALID")
        if not validation or validation.get("status")!="PASS": reasons.append("STRUCTURAL_VALIDATION_MISSING_OR_FAIL")
        sha=(download or {}).get("source_sha256","")
        if not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}",sha):
            reasons.append("SOURCE_SHA256_MISSING")
        if not retention or retention.get("remote_verification")!="PASS":
            reasons.append("REMOTE_RETENTION_NOT_VERIFIED")
        if want_profile=="full-lossless":
            if not meta or meta.get("reader_equivalence_status") not in {"PASS","PASS_NO_VALID_AOI_PIXELS"}:
                reasons.append("RAW_ARCHIVE_READER_EQUIVALENCE_NOT_PASS")
            if not meta or not meta.get("retained_archive"):
                reasons.append("RAW_ARCHIVE_NOT_RETAINED")
        if not local_cleanup or local_cleanup.get("remote_retention_verification")!="PASS":
            reasons.append("POST_CLEANUP_RECEIPT_MISSING_OR_FAIL")
        if local_cleanup and sha and local_cleanup.get("source_file_sha256")!=sha:
            reasons.append("CLEANUP_SOURCE_SHA256_MISMATCH")
        if not cleanup_upload or cleanup_upload.get("remote_verification")!="PASS":
            reasons.append("CLEANUP_RECEIPT_REMOTE_UPLOAD_NOT_VERIFIED")
        complete=not reasons and terminal_reason is None
        if complete:
            status="PASS"
        elif terminal_reason:
            status="TERMINAL_FAILURE"
            reasons.append(terminal_reason)
        else:
            status="RETRY"
        decisions.append({"product_id":pid,"granule_name":granule,"status":status,
                          "terminal_reason":terminal_reason if status=="TERMINAL_FAILURE" else None,
                          "reasons":[] if complete else sorted(set(reasons))})
        if status=="RETRY":
            retry.append({k:product[k] for k in ("id","granule_name","product_type","maturity","expected_source_bytes")})
    return {
        "schema_version":"sarguardian-aoi-retry-plan/1.0",
        "manifest_run_id":manifest.get("run_id"),
        "aoi_wkt_sha256":manifest.get("aoi_wkt_sha256"),
        "products_expected":len(manifest.get("products",[])),
        "products_complete":sum(1 for row in decisions if row["status"]=="PASS"),
        "retry_count":len(retry),
        "terminal_failure_count":sum(1 for row in decisions if row["status"]=="TERMINAL_FAILURE"),
        "retry_matrix":retry,
        "decisions":decisions,
    }

def build_retry_manifest(manifest: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    recovery=dict(manifest)
    recovery["products"]=list(result["retry_matrix"])
    recovery["recovery_product_count"]=len(recovery["products"])
    recovery["recovery_source_run_id"]=manifest.get("run_id")
    return recovery

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest",required=True,type=Path)
    p.add_argument("--product-root",required=True,type=Path)
    p.add_argument("--cleanup-root",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    p.add_argument("--retry-manifest-out",type=Path)
    a=p.parse_args()
    manifest=read_json(a.manifest)
    if manifest is None:
        print("ERROR=AOI_RUN_MANIFEST_INVALID")
        return 2
    result=plan(manifest,a.product_root,a.cleanup_root)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    if a.retry_manifest_out is not None:
        a.retry_manifest_out.parent.mkdir(parents=True,exist_ok=True)
        a.retry_manifest_out.write_text(
            json.dumps(build_retry_manifest(manifest,result),indent=2,ensure_ascii=False)+"\n",
            encoding="utf-8",
        )
    with open(os.environ["GITHUB_OUTPUT"],"a",encoding="utf-8") as out:
        out.write("retry_matrix="+json.dumps(result["retry_matrix"],separators=(",",":"))+"\n")
        out.write("retry_count="+str(result["retry_count"])+"\n")
        out.write("terminal_failure_count="+str(result["terminal_failure_count"])+"\n")
    print("AOI_RETRY_PRODUCTS={}/{}".format(result["retry_count"],result["products_expected"]))
    print("AOI_TERMINAL_FAILURES={}".format(result["terminal_failure_count"]))
    for d in result["decisions"]:
        print("AOI_PRODUCT_CHECKPOINT={} status={} reasons={}".format(d["product_id"],d["status"],",".join(d["reasons"]) or "none"))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
