from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "drive_resumable_upload.py"
spec = importlib.util.spec_from_file_location("drive_resumable_upload_retry_tests", TOOL)
assert spec and spec.loader
drive_upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drive_upload)


class FakeResponse:
    def __init__(self, status_code, *, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload or {}
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, request_responses=None, post_responses=None, put_responses=None):
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.put_responses = list(put_responses or [])
        self.request_calls = []
        self.post_calls = []
        self.put_calls = []

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        result = self.request_responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self.post_responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        result = self.put_responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_drive(session):
    drive = drive_upload.Drive.__new__(drive_upload.Drive)
    drive.s = session
    drive.token = "test-access-token"
    drive.client_id = "test-client"
    drive.client_secret = "test-secret"
    drive.refresh = "test-refresh"
    return drive


def test_drive_api_retries_transient_503_and_then_succeeds(monkeypatch):
    monkeypatch.setattr(drive_upload.time, "sleep", lambda _seconds: None)
    session = FakeSession(request_responses=[
        FakeResponse(503),
        FakeResponse(200, payload={"files": []}),
    ])
    drive = make_drive(session)

    response = drive.request("GET", f"{drive_upload.API}/files")

    assert response.status_code == 200
    assert len(session.request_calls) == 2
    assert all(call[2]["headers"]["Authorization"] == "Bearer test-access-token"
               for call in session.request_calls)


def test_oauth_refresh_retries_transient_503(monkeypatch, capsys):
    monkeypatch.setattr(drive_upload.time, "sleep", lambda _seconds: None)
    session = FakeSession(post_responses=[
        FakeResponse(503),
        FakeResponse(200, payload={"access_token": "fresh-access-token"}),
    ])
    drive = make_drive(session)

    drive._refresh()

    assert drive.token == "fresh-access-token"
    assert len(session.post_calls) == 2
    assert "DRIVE_RETRY stage=token_refresh" in capsys.readouterr().err


def test_resumable_upload_recovers_acknowledged_offset_after_http_500(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(drive_upload.time, "sleep", lambda _seconds: None)
    path = tmp_path / "small-result.csv"
    data = b"abcdefgh"
    path.write_bytes(data)
    md5 = hashlib.md5(data).hexdigest()
    sha = hashlib.sha256(data).hexdigest()

    final_metadata = {
        "id": "remote-file-id",
        "name": path.name,
        "size": str(len(data)),
        "md5Checksum": md5,
        "webViewLink": "https://drive.google.com/file/d/remote-file-id/view",
    }
    session = FakeSession(
        request_responses=[
            FakeResponse(200, headers={"Location": "https://www.googleapis.com/upload/session-1"})
        ],
        put_responses=[
            # The first chunk request receives a transient server error.
            FakeResponse(500),
            # Status query confirms that the first four bytes were accepted.
            FakeResponse(308, headers={"Range": "bytes=0-3"}),
            # The uploader resumes with only the remaining four bytes.
            FakeResponse(201, payload=final_metadata),
        ],
    )
    drive = make_drive(session)
    drive.list_named_children = lambda _parent, _name: []

    result = drive.upload_resumable(
        "parent-folder", path, "text/csv", md5, sha, len(data)
    )

    assert result["id"] == "remote-file-id"
    assert result["bytes"] == len(data)
    assert result["md5"] == md5
    assert result["reused"] is False
    assert len(session.put_calls) == 3
    assert session.put_calls[0][1]["headers"]["Content-Range"] == "bytes 0-7/8"
    assert session.put_calls[1][1]["headers"]["Content-Range"] == "bytes */8"
    assert session.put_calls[2][1]["headers"]["Content-Range"] == "bytes 4-7/8"
    assert session.put_calls[2][1]["data"] == b"efgh"
    assert "DRIVE_RETRY stage=chunk_upload" in capsys.readouterr().err


def test_non_retryable_drive_http_400_fails_without_repeating(monkeypatch):
    monkeypatch.setattr(drive_upload.time, "sleep", lambda _seconds: None)
    session = FakeSession(request_responses=[FakeResponse(400)])
    drive = make_drive(session)

    with pytest.raises(drive_upload.DriveError, match="DRIVE_API_HTTP_400"):
        drive.request("GET", f"{drive_upload.API}/files")

    assert len(session.request_calls) == 1


def test_folder_creation_recovers_if_post_response_is_transiently_ambiguous(monkeypatch):
    monkeypatch.setattr(drive_upload.time, "sleep", lambda _seconds: None)
    session = FakeSession(request_responses=[
        # Initial lookup confirms the folder does not exist.
        FakeResponse(200, payload={"files": []}),
        # The server returns 500 during creation after possibly creating it.
        FakeResponse(500),
        # The retry iteration re-queries and discovers that it exists.
        FakeResponse(200, payload={"files": [{
            "id": "folder-created-before-500",
            "name": "SARGuardian-DataArchive",
            "mimeType": drive_upload.FOLDER_MIME,
        }]}),
    ])
    drive = make_drive(session)
    folder_id = drive.ensure_folder("parent-id", "SARGuardian-DataArchive")

    assert folder_id == "folder-created-before-500"
    assert len(session.request_calls) == 3


class FakeExistingFilesDrive:
    def __init__(self, existing_by_name):
        self.existing_by_name = existing_by_name

    def list_named_children(self, _folder, name):
        return self.existing_by_name.get(name, [])


def _existing_record(name: str, data: bytes, *, file_id: str = "remote-old-id"):
    return {
        "id": file_id,
        "name": name,
        "size": str(len(data)),
        "md5Checksum": hashlib.md5(data).hexdigest(),
        "mimeType": "application/octet-stream",
    }


def test_same_run_retry_reuse_counts_files_once_as_new_in_this_run():
    row = {
        "id": "remote-file-id",
        "name": "export_src.tif",
        "bytes": 4096,
        "reused": True,
        "webViewLink": "https://drive.google.com/file/d/remote-file-id/view",
    }

    normalized = drive_upload.normalise_run_scoped_receipt_row(
        row, existing_files_are_new=True
    )

    assert normalized["reused"] is False
    assert normalized["upload_action_reused"] is True
    assert normalized["id"] == row["id"]
    assert row["reused"] is True  # The source record isn't mutated.


def test_only_known_receipt_files_can_be_replaced_during_same_run_retry(tmp_path):
    path = tmp_path / "drive-retention-receipt.json"
    path.write_bytes(b"fresh receipt")
    md5, sha, size = drive_upload.hashes(path)
    old = _existing_record(path.name, b"old receipt")
    drive = FakeExistingFilesDrive({path.name: [old]})

    missing, replacements = drive_upload.plan_existing_uploads(
        drive,
        "run-folder-id",
        [(path, md5, sha, size, "application/json")],
        replace_existing_retry_sidecars=True,
    )

    assert missing == size
    assert replacements == [{
        "id": "remote-old-id",
        "name": "drive-retention-receipt.json",
        "bytes": size,
    }]


def test_retry_never_replaces_a_scientific_output_with_different_content(tmp_path):
    path = tmp_path / "export_src.tif"
    path.write_bytes(b"new raster bytes")
    md5, sha, size = drive_upload.hashes(path)
    old = _existing_record(path.name, b"old raster bytes")
    drive = FakeExistingFilesDrive({path.name: [old]})

    with pytest.raises(drive_upload.DriveError, match="DRIVE_EXISTING_FILE_CONFLICT"):
        drive_upload.plan_existing_uploads(
            drive,
            "run-folder-id",
            [(path, md5, sha, size, "image/tiff")],
            replace_existing_retry_sidecars=True,
        )


def test_identical_run_scoped_files_do_not_require_new_drive_bytes(tmp_path):
    path = tmp_path / "export_src.tif"
    data = b"same immutable output"
    path.write_bytes(data)
    md5, sha, size = drive_upload.hashes(path)
    old = _existing_record(path.name, data)
    drive = FakeExistingFilesDrive({path.name: [old]})

    missing, replacements = drive_upload.plan_existing_uploads(
        drive,
        "run-folder-id",
        [(path, md5, sha, size, "image/tiff")],
        replace_existing_retry_sidecars=True,
    )

    assert missing == 0
    assert replacements == []
