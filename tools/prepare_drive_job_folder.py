#!/usr/bin/env python3
"""Create the per-job Drive folder once before parallel product runners start."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from drive_resumable_upload import Drive, DriveError, drive_folder_names, required_env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--version-tag", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.job_id):
        raise DriveError("JOB_ID_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.version_tag):
        raise DriveError("VERSION_TAG_INVALID")
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriveError("MANIFEST_INVALID") from exc
    products = manifest.get("products") if isinstance(manifest, dict) else None
    if not isinstance(products, list) or not products:
        raise DriveError("MANIFEST_PRODUCTS_INVALID")
    drive = Drive()
    parent = required_env("GOOGLE_DRIVE_PARENT_FOLDER_ID")
    job_folder = drive.ensure_folder(parent, args.job_id)
    for product in products:
        if not isinstance(product, dict):
            raise DriveError("MANIFEST_PRODUCT_INVALID")
        try:
            names = drive_folder_names(
                job_id=args.job_id,
                product_type=product["product_type"],
                maturity=product["maturity"],
                granule_name=product["granule_name"],
                version_tag=args.version_tag,
            )
        except (KeyError, TypeError, ValueError):
            raise DriveError("MANIFEST_PRODUCT_INVALID") from None
        folder = parent
        for name in names:
            folder = drive.ensure_folder(folder, name)
    print(
        f"DRIVE_JOB_FOLDER_PREPARED=PASS folder_id={job_folder} product_paths={len(products)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriveError as exc:
        print(f"ERROR={exc}", flush=True)
        raise SystemExit(2)
