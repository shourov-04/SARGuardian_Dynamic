from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from science_adapter.geometry import processing_fingerprint, validate_wkt

from .config import RuntimeSettings
from .jobs import JobState


class CreateDisplacementJobRequest(BaseModel):
    aoi_wkt: str = Field(min_length=20, max_length=100_000)
    start_date: date
    end_date: date
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    product_type: Literal["BOTH", "GUNW", "GOFF"] = "BOTH"
    acquisition_window: Literal["secondary-acquisition-in-window", "both-acquisitions-in-window"] = "secondary-acquisition-in-window"
    retention_mode: Literal["derived-only", "full-lossless"] = "derived-only"
    processing_profile: Literal["validation-pilot", "full-aoi"] = "full-aoi"

    @field_validator("aoi_wkt")
    @classmethod
    def require_single_ring_wgs84_polygon(cls, value: str) -> str:
        validate_wkt(value)
        # Keep the original accepted text for backwards-compatible result records.
        return value.strip()


PROCESSING_OPTION_DEFAULTS = {
    "product_type": "BOTH", "acquisition_window": "secondary-acquisition-in-window",
    "retention_mode": "derived-only", "processing_profile": "validation-pilot",
}


def request_fingerprint(record: dict[str, Any]) -> str:
    return processing_fingerprint(
        aoi_wkt=record["aoi_wkt"], start_date=str(record["start_date"]), end_date=str(record["end_date"]),
        **{name: record.get(name, default) for name, default in PROCESSING_OPTION_DEFAULTS.items()},
    )


class DisplacementJobResponse(BaseModel):
    id: UUID
    state: JobState
    aoi_wkt: str
    start_date: date
    end_date: date
    product_type: Literal["BOTH", "GUNW", "GOFF"] = "BOTH"
    acquisition_window: Literal["secondary-acquisition-in-window", "both-acquisitions-in-window"] = "secondary-acquisition-in-window"
    retention_mode: Literal["derived-only", "full-lossless"] = "derived-only"
    processing_profile: Literal["validation-pilot", "full-aoi"] = "validation-pilot"
    stage: str
    github_run_id: str | None = None
    github_run_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_summary: dict[str, Any] | None = None
    artifact_url: str | None = None
    created_at: str
    updated_at: str


class WorkerCallback(BaseModel):
    state: Literal["running", "succeeded", "failed"]
    sequence: int = Field(ge=1, le=1_000)
    stage: str = Field(min_length=1, max_length=80)
    github_run_id: str = Field(min_length=1, max_length=64)
    github_run_url: str | None = Field(default=None, max_length=2_048)
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=500)
    result_summary: dict[str, Any] | None = None
    artifact_url: str | None = Field(default=None, max_length=2_048)
    job_id: UUID | None = None
    contract_version: Literal[1, 2] = 1
    request_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ResubmitDisplacementJobRequest(BaseModel):
    """An explicit, user-confirmed replacement of one active job."""

    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    confirm: Literal[True]


class GitHubDispatchError(RuntimeError):
    pass


class GitHubCancellationError(RuntimeError):
    pass


def dispatch_workflow(settings: RuntimeSettings, job_id: UUID, request: CreateDisplacementJobRequest) -> None:
    if not settings.displacement_actions_enabled:
        raise GitHubDispatchError("Displacement processing is not configured on this server.")
    endpoint = f"{settings.github_api_url}/repos/{settings.github_actions_repository}/actions/workflows/{settings.github_actions_workflow}/dispatches"
    body = json.dumps({"ref": settings.github_actions_ref, "inputs": {
        "job_id": str(job_id), "aoi_wkt": request.aoi_wkt,
        "start_date": request.start_date.isoformat(), "end_date": request.end_date.isoformat(),
        "callback_url": settings.public_api_url,
        **{name: getattr(request, name) for name in PROCESSING_OPTION_DEFAULTS},
    }}).encode("utf-8")
    request_obj = Request(endpoint, data=body, method="POST", headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_actions_token}",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
    })
    try:
        with urlopen(request_obj, timeout=15) as response:
            if response.status != 204:
                raise GitHubDispatchError("GitHub Actions did not accept the displacement workflow.")
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = payload.get("message") if isinstance(payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = None
        suffix = f": {message[:200]}" if isinstance(message, str) and message else ""
        raise GitHubDispatchError(f"GitHub Actions dispatch was rejected ({exc.code}){suffix}.") from None
    except (URLError, TimeoutError):
        raise GitHubDispatchError("Could not reach GitHub Actions to dispatch the workflow.") from None


def cancel_workflow_run(settings: RuntimeSettings, run_id: str) -> None:
    """Ask GitHub to cancel an already allocated run before replacing its job.

    The caller must not create a replacement if this cannot be confirmed.  That
    avoids silently leaving an expensive science runner behind.
    """
    if not settings.displacement_actions_enabled:
        raise GitHubCancellationError("Displacement processing is not configured on this server.")
    endpoint = f"{settings.github_api_url}/repos/{settings.github_actions_repository}/actions/runs/{quote(run_id, safe='')}/cancel"
    request = Request(endpoint, data=b"", method="POST", headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_actions_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 202:
                raise GitHubCancellationError("GitHub Actions did not accept cancellation of the current workflow.")
    except HTTPError as exc:
        if exc.code in {409, 422}:
            raise GitHubCancellationError("The current GitHub Actions run is no longer cancellable. Refresh its status before trying again.") from None
        raise GitHubCancellationError(f"GitHub Actions rejected workflow cancellation ({exc.code}).") from None
    except (URLError, TimeoutError):
        raise GitHubCancellationError("Could not reach GitHub Actions to cancel the current workflow.") from None


def callback_signature_is_valid(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def workflow_run_status(settings: RuntimeSettings, run_id: str) -> tuple[str, str | None] | None:
    """Read GitHub's terminal state; never promote a job to success here."""
    endpoint = f"{settings.github_api_url}/repos/{settings.github_actions_repository}/actions/runs/{run_id}"
    request = Request(endpoint, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_actions_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    status = data.get("status")
    conclusion = data.get("conclusion")
    return (status, conclusion) if isinstance(status, str) and (isinstance(conclusion, str) or conclusion is None) else None


def find_dispatched_workflow_run(
    settings: RuntimeSettings, job_id: UUID,
) -> tuple[str, str | None, str, str | None] | None:
    """Find this job's dispatched run before a worker callback has recorded its run id.

    GitHub accepts a workflow dispatch before allocating a runner.  Looking up the
    run by its deterministic display title lets the API report a terminal runner
    failure (including failures before the first callback) without ever treating
    a GitHub success as a scientifically validated result.
    """
    workflow = quote(settings.github_actions_workflow, safe="")
    endpoint = (
        f"{settings.github_api_url}/repos/{settings.github_actions_repository}"
        f"/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=100"
    )
    request = Request(endpoint, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_actions_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    expected_title = f"Displacement {job_id}"
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict) or run.get("display_title") != expected_title:
            continue
        run_id, run_url, status, conclusion = (
            run.get("id"), run.get("html_url"), run.get("status"), run.get("conclusion"),
        )
        if not isinstance(run_id, (str, int)) or not isinstance(status, str):
            return None
        return (
            str(run_id), run_url if isinstance(run_url, str) else None, status,
            conclusion if isinstance(conclusion, str) else None,
        )
    return None


def is_dispatch_timed_out(record: dict[str, Any], timeout_seconds: int) -> bool:
    if record.get("state") != JobState.DISPATCHING.value:
        return False
    try:
        updated_at = datetime.fromisoformat(str(record["updated_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return (datetime.now(UTC) - updated_at).total_seconds() > timeout_seconds
