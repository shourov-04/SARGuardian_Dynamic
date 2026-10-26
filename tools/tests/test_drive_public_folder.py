from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drive_resumable_upload import Drive


class Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload

    def close(self) -> None:
        pass


def test_public_reader_permission_is_created_and_verified() -> None:
    drive = Drive.__new__(Drive)
    calls: list[tuple[str, str, dict]] = []
    reads = 0

    def request(method: str, url: str, **kwargs):
        nonlocal reads
        calls.append((method, url, kwargs))
        if method == "GET":
            reads += 1
            return Response({"permissions": [] if reads == 1 else [
                {"id": "permission-1", "type": "anyone", "role": "reader", "allowFileDiscovery": False}
            ]})
        assert method == "POST"
        assert kwargs["json"] == {"type": "anyone", "role": "reader", "allowFileDiscovery": False}
        return Response({"id": "permission-1", "type": "anyone", "role": "reader"})

    drive.request = request
    drive.ensure_public_reader("public-folder-id-123")

    assert [method for method, _url, _kwargs in calls] == ["GET", "POST", "GET"]


def test_existing_public_reader_permission_is_reused() -> None:
    drive = Drive.__new__(Drive)
    calls: list[str] = []

    def request(method: str, _url: str, **_kwargs):
        calls.append(method)
        return Response({"permissions": [{"type": "anyone", "role": "reader"}]})

    drive.request = request
    drive.ensure_public_reader("public-folder-id-123")

    assert calls == ["GET"]
