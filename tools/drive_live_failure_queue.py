#!/usr/bin/env python3
"""Publish and consume tiny failed-granule signals through the shared Drive job folder."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from drive_resumable_upload import API, Drive, DriveError, FOLDER_MIME, hashes, required_env


PRODUCT_ID_RE = re.compile(r"(?:GUNW|GOFF)-[0-9]{3,5}")
SAFE_SCOPE_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class LiveFailureQueue:
    def __init__(self, *, job_id: str, version_tag: str) -> None:
        if not SAFE_SCOPE_RE.fullmatch(job_id) or not SAFE_SCOPE_RE.fullmatch(version_tag):
            raise DriveError("LIVE_FAILURE_QUEUE_SCOPE_INVALID")
        self.drive = Drive()
        parent = required_env("GOOGLE_DRIVE_PARENT_FOLDER_ID")
        job_folder = self.drive.ensure_folder(parent, job_id)
        queue_folder = self.drive.ensure_folder(job_folder, "LiveFailures")
        self.folder = self.drive.ensure_folder(queue_folder, version_tag)

    def publish(self, product: dict[str, Any], destination: Path) -> dict[str, Any]:
        product_id = str(product.get("id", ""))
        if not PRODUCT_ID_RE.fullmatch(product_id):
            raise DriveError("LIVE_FAILURE_PRODUCT_ID_INVALID")
        record = {
            "schema_version": "sarguardian-live-failure/1.0",
            "product": {
                key: product[key]
                for key in ("id", "granule_name", "product_type", "maturity", "expected_source_bytes")
            },
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        md5, sha256, size = hashes(destination)
        uploaded = self.drive.upload_resumable(
            self.folder, destination, "application/json", md5, sha256, size
        )
        return {"record": record, "remote": uploaded}

    def signals(self) -> list[dict[str, Any]]:
        query = f"'{self.drive._q(self.folder)}' in parents and trashed = false"
        response = self.drive.request(
            "GET", f"{API}/files",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": 100,
                "fields": "files(id,name,mimeType,size,md5Checksum,createdTime)",
                "orderBy": "createdTime",
            },
        )
        try:
            payload = response.json()
        finally:
            response.close()
        rows = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DriveError("LIVE_FAILURE_QUEUE_LIST_INVALID")
        signals: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("mimeType") == FOLDER_MIME:
                continue
            match = re.fullmatch(r"((?:GUNW|GOFF)-[0-9]{3,5})\.json", str(row.get("name", "")))
            file_id = row.get("id")
            if not match or not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
                continue
            signals.append({"id": file_id, "product_id": match.group(1), "name": row["name"]})
        return signals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "list"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--version-tag", required=True)
    parser.add_argument("--product-json", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    queue = LiveFailureQueue(job_id=args.job_id, version_tag=args.version_tag)
    if args.command == "list":
        print(json.dumps(queue.signals(), separators=(",", ":")))
        return 0
    if args.product_json is None or args.out is None:
        raise DriveError("LIVE_FAILURE_PUBLISH_ARGUMENTS_MISSING")
    product = json.loads(args.product_json.read_text(encoding="utf-8"))
    result = queue.publish(product, args.out)
    print(
        f"LIVE_FAILURE_SIGNAL_PUBLISHED=PASS product_id={product['id']} "
        f"file_id={result['remote']['id']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DriveError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR={exc}", flush=True)
        raise SystemExit(2)
