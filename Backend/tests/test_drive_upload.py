from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from science_adapter import upload_to_drive as drive


class Response:
    def __init__(self, payload: dict):
        self.payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class DriveSession:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.next_id = 0
        self.files: dict[str, tuple[str, bytes]] = {}

    def post(self, _url, **_kwargs):
        return Response({"access_token": "access-token"})

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET" and url.endswith("/files"):
            return Response({"files": []})
        if method == "GET" and url.endswith("/permissions"):
            return Response({"permissions": []})
        if method == "POST" and url == f"{drive.DRIVE_API}/files":
            self.next_id += 1
            return Response({"id": f"folder-{self.next_id}"})
        if method in {"POST", "PATCH"} and url.startswith(f"{drive.DRIVE_UPLOAD_API}/files"):
            self.next_id += 1
            identifier = f"file-{self.next_id}"
            upload = kwargs["files"]["file"]
            self.files[identifier] = (upload[0], upload[1].read())
            return Response({"id": identifier})
        if method == "GET" and "/files/file-" in url:
            identifier = url.rsplit("/", 1)[-1]
            name, content = self.files[identifier]
            return Response({"id": identifier, "name": name, "size": str(len(content)), "md5Checksum": hashlib.md5(content).hexdigest()})
        raise AssertionError(f"Unexpected Drive request: {method} {url}")


def write_validated_manifest(work_dir: Path) -> None:
    output = work_dir / "outputs" / "granule-one" / "screening.tif"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"validated-geotiff")
    (work_dir / "result-manifest.json").write_text(json.dumps({
        "status": "succeeded",
        "job_id": "123e4567-e89b-12d3-a456-426614174000",
        "validated_outputs": [{
            "path": "outputs/granule-one/screening.tif",
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")


def test_uploads_only_manifest_and_checksum_verified_outputs_to_a_private_job_folder(tmp_path: Path) -> None:
    write_validated_manifest(tmp_path)
    session = DriveSession()
    config = drive.DriveConfig("client", "secret", "https://oauth2.googleapis.com/token", "refresh", "13BIAepPGLlYYmscUlNPeMCdtepYshjT4")

    receipt = drive.upload_validated_results(tmp_path, config=config, session=session)

    assert receipt["status"] == "PASS"
    uploaded = [item for item in session.requests if item[1].startswith(f"{drive.DRIVE_UPLOAD_API}/files")]
    assert len(uploaded) >= 3  # manifest is refreshed after verified inventory is written
    assert not any("/permissions" in item[1] and item[0] == "POST" for item in session.requests)


def test_rejects_tampered_validated_outputs_before_google_drive_is_contacted(tmp_path: Path) -> None:
    write_validated_manifest(tmp_path)
    (tmp_path / "outputs" / "granule-one" / "screening.tif").write_bytes(b"tampered")

    with pytest.raises(drive.DriveUploadError, match="checksum"):
        drive.upload_validated_results(tmp_path, config=drive.DriveConfig("client", "secret", "https://oauth2.googleapis.com/token", "refresh", "13BIAepPGLlYYmscUlNPeMCdtepYshjT4"))


def test_rejects_non_web_or_untrusted_google_oauth_configuration() -> None:
    with pytest.raises(drive.DriveUploadError, match="web credentials"):
        drive.load_drive_config({
            "GOOGLE_DRIVE_OAUTH_CLIENT_JSON": json.dumps({"installed": {}}),
            "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh",
            "GOOGLE_DRIVE_FOLDER_ID": "13BIAepPGLlYYmscUlNPeMCdtepYshjT4",
        })
    with pytest.raises(drive.DriveUploadError, match="unsupported token endpoint"):
        drive.load_drive_config({
            "GOOGLE_DRIVE_OAUTH_CLIENT_JSON": json.dumps({"web": {
                "client_id": "client", "client_secret": "secret", "token_uri": "https://invalid.example/token",
            }}),
            "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh",
            "GOOGLE_DRIVE_FOLDER_ID": "13BIAepPGLlYYmscUlNPeMCdtepYshjT4",
        })


def test_drive_config_accepts_the_workflow_parent_folder_variable() -> None:
    config = drive.load_drive_config({
        "GOOGLE_DRIVE_OAUTH_CLIENT_JSON": json.dumps({"web": {
            "client_id": "client", "client_secret": "secret",
            "token_uri": "https://oauth2.googleapis.com/token",
        }}),
        "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh",
        "GOOGLE_DRIVE_PARENT_FOLDER_ID": "13BIAepPGLlYYmscUlNPeMCdtepYshjT4",
    })

    assert config.parent_folder_id == "13BIAepPGLlYYmscUlNPeMCdtepYshjT4"


def test_downloads_public_result_only_from_googleusercontent() -> None:
    content = b"verified public result"

    class PublicResponse:
        url = "https://drive.usercontent.google.com/download?id=abcdefghijk"
        content = b"verified public result"

        def raise_for_status(self) -> None:
            pass

    class PublicSession:
        def get(self, url, **kwargs):
            assert url == "https://drive.usercontent.google.com/download"
            assert kwargs["params"]["id"] == "abcdefghijk"
            assert kwargs["allow_redirects"] is True
            return PublicResponse()

    receipt, downloaded = drive.download_public_result("abcdefghijk", session=PublicSession())

    assert downloaded == content
    assert receipt["bytes"] == len(content)
    assert receipt["md5"] == hashlib.md5(content).hexdigest()


def test_rejects_public_result_redirect_to_untrusted_host() -> None:
    class RedirectedResponse:
        url = "https://attacker.example/result.zip"
        content = b"untrusted"

        def raise_for_status(self) -> None:
            pass

    class RedirectedSession:
        def get(self, _url, **_kwargs):
            return RedirectedResponse()

    with pytest.raises(drive.DriveUploadError, match="untrusted host"):
        drive.download_public_result("abcdefghijk", session=RedirectedSession())
