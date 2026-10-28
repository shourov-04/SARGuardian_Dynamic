from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive_live_failure_queue as queue_module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def close(self):
        return None


class FakeDrive:
    def __init__(self):
        self.uploads = []

    def ensure_folder(self, parent, name):
        return f"folder-{name}"

    @staticmethod
    def _q(value):
        return value

    def upload_resumable(self, folder, path, mime, md5, sha256, size):
        self.uploads.append((folder, path, mime, md5, sha256, size))
        return {"id": "failureSignal123", "name": path.name, "bytes": size}

    def request(self, *_args, **_kwargs):
        return FakeResponse({"files": [
            {
                "id": "failureSignal123", "name": "GOFF-027.json",
                "mimeType": "application/json", "size": "100",
            },
            {"id": "ignoreFolder123", "name": "nested", "mimeType": queue_module.FOLDER_MIME},
        ]})


def test_queue_publishes_deterministic_product_signal(monkeypatch, tmp_path: Path) -> None:
    drive = FakeDrive()
    monkeypatch.setattr(queue_module, "Drive", lambda: drive)
    monkeypatch.setattr(queue_module, "required_env", lambda _name: "parent-folder")
    queue = queue_module.LiveFailureQueue(job_id="job-1", version_tag="run-1")
    product = {
        "id": "GOFF-027", "granule_name": "NISAR_L2_PR_GOFF_TEST",
        "product_type": "GOFF", "maturity": "PROVISIONAL", "expected_source_bytes": 100,
    }

    result = queue.publish(product, tmp_path / "GOFF-027.json")

    stored = json.loads((tmp_path / "GOFF-027.json").read_text(encoding="utf-8"))
    assert stored["product"] == product
    assert result["remote"]["id"] == "failureSignal123"
    assert drive.uploads[0][0] == "folder-run-1"


def test_queue_lists_only_valid_failed_product_signals(monkeypatch) -> None:
    drive = FakeDrive()
    monkeypatch.setattr(queue_module, "Drive", lambda: drive)
    monkeypatch.setattr(queue_module, "required_env", lambda _name: "parent-folder")

    signals = queue_module.LiveFailureQueue(job_id="job-1", version_tag="run-1").signals()

    assert signals == [{
        "id": "failureSignal123", "product_id": "GOFF-027", "name": "GOFF-027.json",
    }]
