"""API behaviour of DISPLACEMENT_WORKFLOW_MODE=external.

GitHub itself is replaced by monkeypatching the ``external_workflow`` functions (they are
covered against a real local HTTP server in ``test_external_workflow.py``); these tests
check how the routes use them: dispatch inputs, run binding, polling, persistence,
ownership and the closed callback endpoint.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app import external_workflow
from app import main as main_module
from app.auth import get_current_user
from app.main import app
from app.persistence import PersistenceConflictError

ACTIVE = {"queued", "dispatching", "running"}
RUN_URL = "https://github.com/owner/repo/actions/runs/777"


class ExternalStore:
    """In-memory store that, like the database trigger, refuses writes from a stale state."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.lose_next_update_to_another_request = False

    def get_displacement_job_by_idempotency(self, owner, key):
        return next((row for row in self.rows if row["owner_uid"] == owner and row["idempotency_key"] == key), None)

    def create_displacement_job(self, values):
        now = datetime.now(UTC).isoformat()
        row = values | {
            "id": str(uuid4()), "created_at": now, "updated_at": now, "github_run_id": None, "github_run_url": None,
            "callback_sequence": 0, "error_code": None, "error_message": None, "result_summary": None, "artifact_url": None,
        }
        self.rows.append(row)
        return row

    def list_displacement_jobs(self, owner, limit=20):
        return [row for row in reversed(self.rows) if row["owner_uid"] == owner][:limit]

    def find_active_displacement_job(self, owner, aoi_wkt, start_date, end_date):
        return next((row for row in reversed(self.rows) if row["owner_uid"] == owner and row["aoi_wkt"] == aoi_wkt
                     and row["start_date"] == start_date and row["end_date"] == end_date and row["state"] in ACTIVE), None)

    def list_active_displacement_jobs(self, owner):
        return [row for row in self.rows if row["owner_uid"] == owner and row["state"] in ACTIVE]

    def update_displacement_job(self, job_id, values, **expected):
        row = next(row for row in self.rows if row["id"] == str(job_id))
        if self.lose_next_update_to_another_request:
            self.lose_next_update_to_another_request = False
            row |= {"state": "succeeded", "stage": "completed"}
            raise PersistenceConflictError("another request advanced this job")
        wanted = expected.get("expected_state")
        if wanted is not None and row["state"] != wanted:
            raise PersistenceConflictError("job state changed")
        row |= values
        return row

    def get_displacement_job(self, owner, job_id):
        return next((row for row in self.rows if row["owner_uid"] == owner and row["id"] == str(job_id)), None)

    def get_displacement_job_unscoped(self, job_id):
        return next((row for row in self.rows if row["id"] == str(job_id)), None)


def external_settings(**overrides) -> SimpleNamespace:
    values = {
        "displacement_actions_enabled": True, "displacement_workflow_mode": "external", "displacement_dispatch_timeout_seconds": 3600,
        "github_api_url": "https://api.github.test", "github_actions_repository": "owner/repo",
        "github_actions_workflow": "sarguardian-aoi-wkt-dashboard.yml", "github_actions_ref": "main",
        "github_actions_token": "token", "displacement_callback_secret": "", "public_api_url": "",
    }
    return SimpleNamespace(**(values | overrides))


def api(monkeypatch, store: ExternalStore, *, uid: str = "firebase-user", settings: SimpleNamespace | None = None) -> TestClient:
    monkeypatch.setattr(main_module, "get_job_store", lambda: store)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings or external_settings())
    app.dependency_overrides[get_current_user] = lambda: {"uid": uid}
    return TestClient(app)


def setup_function() -> None:
    app.dependency_overrides.clear()


def teardown_function() -> None:
    app.dependency_overrides.clear()


def payload(**overrides) -> dict:
    return {
        "aoi_wkt": "POLYGON((85 27, 85.01 27, 85 27.01, 85 27))", "start_date": "2026-10-01", "end_date": "2026-10-22",
        "idempotency_key": "external-mode-test-001",
    } | overrides


def accept_dispatch(monkeypatch) -> dict:
    captured: dict = {}

    def fake_dispatch(settings, inputs):
        captured["settings"], captured["inputs"] = settings, dict(inputs)
        return external_workflow.DispatchedRun("777", RUN_URL)

    monkeypatch.setattr(external_workflow, "dispatch", fake_dispatch)
    return captured


def submit(monkeypatch, store: ExternalStore) -> tuple[TestClient, dict]:
    accept_dispatch(monkeypatch)
    client = api(monkeypatch, store)
    response = client.post("/api/displacement-jobs", json=payload())
    assert response.status_code == 200, response.text
    return client, response.json()


def test_dispatch_sends_exactly_the_six_upstream_inputs_and_binds_the_run_to_the_job(monkeypatch) -> None:
    store = ExternalStore()
    captured = accept_dispatch(monkeypatch)
    response = api(monkeypatch, store).post("/api/displacement-jobs", json=payload(
        acquisition_window="both-acquisitions-in-window", retention_mode="full-lossless", processing_profile="validation-pilot",
    ))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "dispatching" and body["github_run_id"] == "777" and body["github_run_url"] == RUN_URL
    assert captured["inputs"] == {
        "aoi_wkt": "POLYGON((85 27, 85.01 27, 85 27.01, 85 27))", "start_date": "2026-10-01", "end_date": "2026-10-22",
        "date_filter_mode": "both-acquisitions-in-window", "run_profile": "validation-pilot", "retention_profile": "full-lossless",
    }
    assert captured["settings"].github_actions_repository == "owner/repo"
    # No callback contract: the column only allows NULL or 2, and the immutable fingerprint is still stored.
    assert store.rows[0]["worker_contract_version"] is None and store.rows[0]["request_fingerprint"]


def test_a_single_product_family_is_refused_before_anything_is_created(monkeypatch) -> None:
    store = ExternalStore()
    accept_dispatch(monkeypatch)
    response = api(monkeypatch, store).post("/api/displacement-jobs", json=payload(product_type="GUNW"))
    assert response.status_code == 422 and "both GUNW and GOFF" in response.json()["detail"]
    assert store.rows == []


def test_a_rejected_dispatch_is_stored_as_a_failed_job_with_the_reason(monkeypatch) -> None:
    store = ExternalStore()

    def reject(_settings, _inputs):
        raise external_workflow.ExternalDispatchError("GitHub Actions dispatch was rejected (422): Unexpected inputs provided.")

    monkeypatch.setattr(external_workflow, "dispatch", reject)
    response = api(monkeypatch, store).post("/api/displacement-jobs", json=payload())
    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "failed" and body["stage"] == "dispatch_failed"
    assert body["error_code"] == "GITHUB_DISPATCH_FAILED" and "Unexpected inputs" in body["error_message"]


def test_status_reads_follow_the_github_run_through_the_database_state_machine(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    summary = {"schema_version": 2, "source": "github_artifact", "products": [{"id": "GUNW-001", "product_type": "GUNW", "status": "PASS"}]}
    steps = [
        external_workflow.RefreshStep("dispatching", {"state": "running", "stage": "github_finishing"}),
        external_workflow.RefreshStep("running", {"state": "succeeded", "stage": "completed", "result_summary": summary,
                                                  "error_code": None, "error_message": None}),
    ]
    monkeypatch.setattr(external_workflow, "should_poll", lambda _key: True)
    monkeypatch.setattr(external_workflow, "plan_refresh", lambda _settings, record: steps)
    body = client.get(f"/api/displacement-jobs/{created['id']}").json()
    assert body["state"] == "succeeded" and body["stage"] == "completed"
    assert body["result_summary"]["products"][0]["id"] == "GUNW-001"
    # Terminal jobs are never polled again.
    monkeypatch.setattr(external_workflow, "plan_refresh", lambda *_args: (_ for _ in ()).throw(AssertionError("polled a finished job")))
    assert client.get(f"/api/displacement-jobs/{created['id']}").json()["state"] == "succeeded"


def test_a_github_or_parsing_problem_never_breaks_a_status_read(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    monkeypatch.setattr(external_workflow, "should_poll", lambda _key: True)

    def explode(*_args):
        raise RuntimeError("GitHub exploded")

    monkeypatch.setattr(external_workflow, "plan_refresh", explode)
    response = client.get(f"/api/displacement-jobs/{created['id']}")
    assert response.status_code == 200 and response.json()["state"] == "dispatching"


def test_polling_is_throttled_so_a_two_second_ui_poll_cannot_exhaust_the_github_rate_limit(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    monkeypatch.setattr(external_workflow, "should_poll", lambda _key: False)
    monkeypatch.setattr(external_workflow, "plan_refresh", lambda *_args: (_ for _ in ()).throw(AssertionError("not throttled")))
    assert client.get(f"/api/displacement-jobs/{created['id']}").json()["state"] == "dispatching"


def test_a_lost_race_between_two_status_reads_returns_the_stored_result(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    monkeypatch.setattr(external_workflow, "should_poll", lambda _key: True)
    monkeypatch.setattr(external_workflow, "plan_refresh", lambda _settings, _record: [
        external_workflow.RefreshStep("dispatching", {"state": "running", "stage": "github_running"}),
    ])
    store.lose_next_update_to_another_request = True
    body = client.get(f"/api/displacement-jobs/{created['id']}").json()
    assert body["state"] == "succeeded" and body["stage"] == "completed"


def test_callback_jobs_are_not_polled_even_when_the_server_is_switched_to_external(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    row = store.rows[0]
    row |= {"state": "running", "worker_contract_version": 2, "github_run_id": None}
    monkeypatch.setattr(external_workflow, "should_poll", lambda _key: True)
    monkeypatch.setattr(external_workflow, "plan_refresh", lambda *_args: (_ for _ in ()).throw(AssertionError("polled a callback job")))
    assert client.get(f"/api/displacement-jobs/{created['id']}").json()["state"] == "running"


def test_worker_callbacks_are_rejected_because_no_callback_secret_exists(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    response = client.post(
        f"/api/displacement-jobs/{created['id']}/callback", content=b"{}",
        headers={"X-SARGuardian-Signature": "sha256=" + "0" * 64},
    )
    assert response.status_code == 401


def test_result_link_goes_only_to_the_owner_of_a_succeeded_github_result(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    path = f"/api/displacement-jobs/{created['id']}/result-link"
    issued: list[dict] = []

    def link(_settings, summary):
        issued.append(summary)
        return "https://storage.example.test/blob?sig=1", "sarguardian-aoi-dashboard-777-1.zip"

    monkeypatch.setattr(external_workflow, "result_download_link", link)
    assert client.get(path).status_code == 404  # still running

    store.rows[0] |= {"state": "succeeded", "result_summary": {
        "source": "github_artifact", "github_artifact": {"id": 7, "name": "sarguardian-aoi-dashboard-777-1", "run_id": "777"},
    }}
    ok = api(monkeypatch, store).get(path)
    assert ok.status_code == 200
    assert ok.json() == {"url": "https://storage.example.test/blob?sig=1", "filename": "sarguardian-aoi-dashboard-777-1.zip", "expires_in_seconds": 60}
    assert ok.headers["cache-control"] == "private, no-store" and len(issued) == 1

    assert api(monkeypatch, store, uid="someone-else").get(path).status_code == 404
    assert len(issued) == 1  # nothing was signed for the other account

    store.rows[0]["result_summary"] = {"products": []}  # a Drive/callback style result has no GitHub package
    assert api(monkeypatch, store).get(path).status_code == 404


def test_result_link_reports_expired_and_unreachable_storage_distinctly(monkeypatch) -> None:
    store = ExternalStore()
    client, created = submit(monkeypatch, store)
    store.rows[0] |= {"state": "succeeded", "result_summary": {"source": "github_artifact", "github_artifact": {"id": 7, "name": "n", "run_id": "777"}}}
    path = f"/api/displacement-jobs/{created['id']}/result-link"

    def expired(_settings, _summary):
        raise external_workflow.ArtifactUnavailableError("The result artifact has expired on GitHub.")

    monkeypatch.setattr(external_workflow, "result_download_link", expired)
    gone = client.get(path)
    assert gone.status_code == 410 and "expired" in gone.json()["detail"]

    def unreachable(_settings, _summary):
        raise external_workflow.GitHubApiError("Could not reach GitHub.", transient=True)

    monkeypatch.setattr(external_workflow, "result_download_link", unreachable)
    busy = client.get(path)
    assert busy.status_code == 503 and "Retry" in busy.json()["detail"] and "token" not in busy.text.lower()
