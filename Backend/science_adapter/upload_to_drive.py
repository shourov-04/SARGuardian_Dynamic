"""Publish checksum-verified results to private Google Drive storage.

The application authorizes downloads through its existing job-owner API.  This
module deliberately never creates a public Drive permission or stores raw NISAR
HDF5 in the derived-only output inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_TOKEN_URIS = frozenset({
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",
})
FOLDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024


class DriveUploadError(RuntimeError):
    """A safe, user-facing result-publication failure."""


@dataclass(frozen=True)
class DriveConfig:
    client_id: str
    client_secret: str
    token_uri: str
    refresh_token: str
    parent_folder_id: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_drive_config(environ: dict[str, str] | None = None) -> DriveConfig:
    values = environ if environ is not None else os.environ
    raw_client = values.get("GOOGLE_DRIVE_OAUTH_CLIENT_JSON", "").strip()
    refresh_token = values.get("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()
    parent_folder_id = (
        values.get("GOOGLE_DRIVE_PARENT_FOLDER_ID", "")
        or values.get("GOOGLE_DRIVE_FOLDER_ID", "")
    ).strip()
    if not raw_client or not refresh_token or not parent_folder_id:
        raise DriveUploadError("Google Drive result storage is not fully configured.")
    if not FOLDER_ID_PATTERN.fullmatch(parent_folder_id):
        raise DriveUploadError("Configured Google Drive folder identifier is invalid.")
    try:
        parsed = json.loads(raw_client)
    except json.JSONDecodeError as exc:
        raise DriveUploadError("Google Drive OAuth client JSON is invalid.") from exc
    client = parsed.get("web") if isinstance(parsed, dict) else None
    if not isinstance(client, dict):
        raise DriveUploadError("Google Drive OAuth client JSON must contain web credentials.")
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    token_uri = client.get("token_uri")
    if not all(isinstance(value, str) and value.strip() for value in (client_id, client_secret, token_uri)):
        raise DriveUploadError("Google Drive OAuth client JSON is incomplete.")
    if token_uri not in GOOGLE_TOKEN_URIS:
        raise DriveUploadError("Google Drive OAuth client uses an unsupported token endpoint.")
    return DriveConfig(client_id, client_secret, token_uri, refresh_token, parent_folder_id)


class DriveClient:
    def __init__(self, config: DriveConfig, session: requests.Session | Any | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.access_token = self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        try:
            response = self.session.post(
                self.config.token_uri,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": self.config.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DriveUploadError("Could not authenticate to Google Drive result storage.") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DriveUploadError("Google Drive did not return an access token.")
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, url, headers=self._headers(), timeout=60, **kwargs)
            response.raise_for_status()
            payload = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as exc:
            raise DriveUploadError("Google Drive result storage request failed.") from exc
        if not isinstance(payload, dict):
            raise DriveUploadError("Google Drive returned an invalid result-storage response.")
        return payload

    @staticmethod
    def _query_text(value: str) -> str:
        return value.replace("'", "\\'")

    def _find_named_file(self, name: str, parent_id: str, mime_type: str | None = None) -> str | None:
        clauses = ["trashed = false", f"'{self._query_text(parent_id)}' in parents", f"name = '{self._query_text(name)}'"]
        if mime_type:
            clauses.append(f"mimeType = '{self._query_text(mime_type)}'")
        payload = self._json(
            "GET",
            f"{DRIVE_API}/files",
            params={"q": " and ".join(clauses), "fields": "files(id)", "pageSize": 2},
        )
        files = payload.get("files")
        if not isinstance(files, list):
            raise DriveUploadError("Google Drive returned an invalid file-search response.")
        identifiers = [item.get("id") for item in files if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if len(identifiers) > 1:
            raise DriveUploadError("Google Drive contains ambiguous prior result files for this job.")
        return identifiers[0] if identifiers else None

    def find_or_create_folder(self, name: str, parent_id: str) -> str:
        existing = self._find_named_file(name, parent_id, FOLDER_MIME_TYPE)
        if existing:
            return existing
        payload = self._json(
            "POST",
            f"{DRIVE_API}/files",
            json={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            params={"fields": "id"},
        )
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise DriveUploadError("Google Drive did not create a result folder.")
        return identifier

    def require_private_folder(self, folder_id: str) -> None:
        permissions = self._json("GET", f"{DRIVE_API}/files/{quote(folder_id, safe='')}/permissions",
                                 params={"fields": "permissions(type,role,allowFileDiscovery)"}).get("permissions")
        if not isinstance(permissions, list):
            raise DriveUploadError("Google Drive returned invalid folder permissions.")
        if any(isinstance(item, dict) and item.get("type") == "anyone" for item in permissions):
            raise DriveUploadError("Configured Google Drive result storage is public; private result retention is blocked.")

    def upload_file(self, path: Path, parent_id: str) -> dict[str, Any]:
        existing = self._find_named_file(path.name, parent_id)
        method = "PATCH" if existing else "POST"
        url = f"{DRIVE_UPLOAD_API}/files/{quote(existing, safe='')}" if existing else f"{DRIVE_UPLOAD_API}/files"
        metadata: dict[str, Any] = {"name": path.name}
        if not existing:
            metadata["parents"] = [parent_id]
        if path.stat().st_size > RESUMABLE_THRESHOLD_BYTES:
            payload = self._upload_resumable(path, metadata, method, url)
        else:
            payload = self._upload_multipart(path, metadata, method, url)
        identifier = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise DriveUploadError("Google Drive did not confirm a result-file upload.")
        # A returned upload id is not retention evidence. Read authoritative
        # metadata and compare byte count plus MD5 before allowing cleanup.
        remote = self._json("GET", f"{DRIVE_API}/files/{quote(identifier, safe='')}",
                            params={"fields": "id,name,size,md5Checksum,mimeType"})
        expected_md5 = hashlib.md5(path.read_bytes()).hexdigest()  # nosec: Drive's published integrity field
        if (remote.get("id") != identifier or remote.get("name") != path.name
                or str(remote.get("size")) != str(path.stat().st_size)
                or remote.get("md5Checksum") != expected_md5):
            raise DriveUploadError("Google Drive did not verify the uploaded result bytes.")
        return {"drive_file_id": identifier, "name": path.name, "bytes": path.stat().st_size,
                "md5": expected_md5, "remote_md5": remote["md5Checksum"], "status": "PASS"}

    def _upload_multipart(self, path: Path, metadata: dict[str, Any], method: str, url: str) -> dict[str, Any]:
        try:
            with path.open("rb") as stream:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params={"uploadType": "multipart", "fields": "id,name,size,md5Checksum,mimeType"},
                    files={
                        "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
                        "file": (path.name, stream, "application/octet-stream"),
                    },
                    timeout=300,
                )
                response.raise_for_status()
                payload = response.json()
        except (OSError, requests.RequestException, ValueError) as exc:
            raise DriveUploadError("Could not upload a validated result file to Google Drive.") from exc
        if not isinstance(payload, dict):
            raise DriveUploadError("Google Drive did not return a valid upload response.")
        return payload

    def _upload_resumable(self, path: Path, metadata: dict[str, Any], method: str, url: str) -> dict[str, Any]:
        """Bound memory use for lossless archives and large GeoTIFFs."""
        try:
            response = self.session.request(
                method, url, headers=self._headers() | {"X-Upload-Content-Type": "application/octet-stream"},
                params={"uploadType": "resumable", "fields": "id"}, json=metadata, timeout=60,
            )
            response.raise_for_status()
            upload_url = response.headers.get("Location")
            if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
                raise DriveUploadError("Google Drive did not create a resumable upload session.")
            total = path.stat().st_size
            offset = 0
            with path.open("rb") as stream:
                while offset < total:
                    chunk = stream.read(min(RESUMABLE_CHUNK_BYTES, total - offset))
                    if not chunk:
                        raise DriveUploadError("Local result file changed during resumable upload.")
                    end = offset + len(chunk) - 1
                    response = self.session.put(upload_url, headers=self._headers() | {
                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"},
                        data=chunk, timeout=300)
                    if end + 1 < total:
                        if response.status_code not in {200, 201, 308}:
                            response.raise_for_status()
                    else:
                        response.raise_for_status()
                        payload = response.json()
                    offset = end + 1
        except (OSError, requests.RequestException, ValueError) as exc:
            raise DriveUploadError("Could not resumably upload a validated result file to Google Drive.") from exc
        if not isinstance(payload, dict):
            raise DriveUploadError("Google Drive did not return a valid resumable upload response.")
        return payload

    def download_file(self, file_id: str) -> tuple[dict[str, Any], bytes]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
            raise DriveUploadError("Result storage file identifier is invalid.")
        try:
            response = self.session.get(f"{DRIVE_API}/files/{quote(file_id, safe='')}", headers=self._headers(),
                                        params={"alt": "media"}, timeout=300)
            response.raise_for_status()
            data = response.content
        except requests.RequestException as exc:
            raise DriveUploadError("Could not retrieve the private result artifact.") from exc
        return {"bytes": len(data), "md5": hashlib.md5(data).hexdigest()}, data  # nosec: Drive integrity field


def _validated_result_files(work_dir: Path) -> tuple[dict[str, Any], list[Path]]:
    manifest_path = work_dir / "result-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriveUploadError("Validated result manifest is unavailable for Google Drive upload.") from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "succeeded":
        raise DriveUploadError("Result manifest does not prove a successful scientific run.")
    outputs = manifest.get("validated_outputs")
    if not isinstance(outputs, list) or not outputs:
        raise DriveUploadError("Result manifest has no validated files to upload.")
    root = work_dir.resolve()
    files = [manifest_path]
    for output in outputs:
        if not isinstance(output, dict):
            raise DriveUploadError("Result manifest contains an invalid validated-file entry.")
        relative = output.get("path")
        digest = output.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise DriveUploadError("Result manifest contains an invalid validated-file entry.")
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file() or _sha256_file(candidate) != digest:
            raise DriveUploadError("A validated result file is missing or failed its checksum before Google Drive upload.")
        files.append(candidate)
    return manifest, files


def upload_validated_results(work_dir: Path, *, config: DriveConfig | None = None, session: Any | None = None) -> dict[str, Any]:
    manifest, files = _validated_result_files(work_dir)
    job_id = manifest.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        raise DriveUploadError("Result manifest has an invalid job identifier.")
    client = DriveClient(config or load_drive_config(), session=session)
    client.require_private_folder(client.config.parent_folder_id)
    job_folder = client.find_or_create_folder(job_id, client.config.parent_folder_id)
    client.require_private_folder(job_folder)
    outputs_folder = client.find_or_create_folder("outputs", job_folder)
    root = work_dir.resolve()
    receipts = []
    output_by_path = {str(item.get("path")): item for item in manifest.get("validated_outputs", []) if isinstance(item, dict)}
    for path in files:
        parent = job_folder if path.name == "result-manifest.json" else outputs_folder
        if path.name != "result-manifest.json":
            relative_parent = path.relative_to(root / "outputs").parent
            for part in relative_parent.parts:
                parent = client.find_or_create_folder(part, parent)
        receipt = client.upload_file(path, parent)
        relative = str(path.relative_to(root)).replace("\\", "/")
        receipt["path"] = relative
        receipts.append(receipt)
        if relative in output_by_path:
            output_by_path[relative]["drive_file_id"] = receipt["drive_file_id"]
            output_by_path[relative]["remote_verification"] = receipt
    manifest["storage_verification"] = {"status": "PASS", "storage": "google-drive-private",
                                        "job_folder_id": job_folder, "receipts": receipts,
                                        "retained_bytes": sum(item["bytes"] for item in receipts)}
    manifest_path = root / "result-manifest.json"
    # The manifest stored remotely is refreshed once more below. Its own hash is
    # intentionally not a recursive part of the inventory.
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_receipt = client.upload_file(manifest_path, job_folder)
    manifest["storage_verification"]["manifest_receipt"] = manifest_receipt
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    client.upload_file(manifest_path, job_folder)
    return manifest["storage_verification"]


def download_private_result(file_id: str, *, config: DriveConfig | None = None, session: Any | None = None) -> tuple[dict[str, Any], bytes]:
    """Used only after API job-owner authorization; never exposed to browsers."""
    return DriveClient(config or load_drive_config(), session=session).download_file(file_id)


def download_public_result(file_id: str, *, session: Any | None = None) -> tuple[dict[str, Any], bytes]:
    """Download a checksum-verified workflow artifact from an anyone-with-link folder."""
    if not FOLDER_ID_PATTERN.fullmatch(file_id):
        raise DriveUploadError("Result storage file identifier is invalid.")
    client = session or requests.Session()
    try:
        response = client.get(
            "https://drive.usercontent.google.com/download",
            params={"id": file_id, "export": "download", "confirm": "t"},
            timeout=300,
            allow_redirects=True,
        )
        response.raise_for_status()
        final = urlparse(str(getattr(response, "url", "https://drive.usercontent.google.com/")))
        if final.scheme != "https" or not (
            final.hostname == "drive.usercontent.google.com"
            or (final.hostname or "").endswith(".googleusercontent.com")
        ):
            raise DriveUploadError("Public result storage redirected to an untrusted host.")
        data = response.content
    except requests.RequestException as exc:
        raise DriveUploadError("Could not retrieve the public result artifact.") from exc
    return {"bytes": len(data), "md5": hashlib.md5(data).hexdigest()}, data  # nosec: integrity metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = upload_validated_results(args.work_dir.resolve())
    except DriveUploadError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    print("DRIVE_STORAGE_RECEIPT=" + json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
