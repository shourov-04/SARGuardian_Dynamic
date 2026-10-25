from __future__ import annotations

import math
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

from .asf_catalog import ASFAdapterError, ASFAdapterNotConfigured, normalize_product_rows, search_asf_catalog
from .auth import get_current_user
from . import external_workflow
from .config import SettingsError, get_settings, validate_production_settings
from .jobs import CreateJobRequest, JobResponse, JobState
from .displacements import (
    PROCESSING_OPTION_DEFAULTS,
    CreateDisplacementJobRequest,
    DisplacementJobResponse,
    GitHubCancellationError,
    GitHubDispatchError,
    ResubmitDisplacementJobRequest,
    WorkerCallback,
    callback_signature_is_valid,
    cancel_workflow_run,
    dispatch_workflow,
    find_dispatched_workflow_run,
    is_dispatch_timed_out,
    request_fingerprint as displacement_request_fingerprint,
    workflow_run_status,
)
from .models import CatalogResponse, CatalogSearchRequest
from .persistence import (
    PersistenceConfigurationError,
    PersistenceConflictError,
    PersistenceError,
    get_job_store,
)
from science_adapter.upload_to_drive import DriveUploadError, download_private_result

logger = logging.getLogger(__name__)

EARTH_RADIUS_METERS = 6_371_000
MAX_AOI_AREA_KM2 = 1_000
MAX_DATE_WINDOW_DAYS = 90
MAX_REQUEST_BODY_BYTES = 1_000_000
ELIGIBILITY_UNDETERMINED_REASON = "Catalogue metadata alone does not establish scientific processing eligibility; the science workflow must inspect the selected products and AOI separately."


def _cors_origins() -> list[str]:
    settings = get_settings()
    configured = settings.cors_origins
    if settings.app_env == "production" and not configured:
        raise RuntimeError("CORS_ORIGINS must be explicitly configured in production.")
    if not configured.strip():
        configured = "http://localhost:5173,http://127.0.0.1:5173"
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        validate_production_settings()
    except SettingsError as exc:
        raise RuntimeError(str(exc)) from None
    yield


app = FastAPI(title="SARGuardian Catalogue API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    logger.error("Unhandled API error", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body is too large."})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})
    return await call_next(request)


def _validate_coordinates(coordinates: list[list[float]]) -> list[tuple[float, float]]:
    if len(coordinates) < 3:
        raise HTTPException(status_code=422, detail="AOI must contain at least 3 coordinates.")

    normalized: list[tuple[float, float]] = []
    for coordinate in coordinates:
        if len(coordinate) != 2:
            raise HTTPException(status_code=422, detail="Each AOI coordinate must contain longitude and latitude.")
        longitude, latitude = coordinate
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise HTTPException(status_code=422, detail="AOI coordinates must be finite numbers.")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise HTTPException(status_code=422, detail="AOI coordinates must use WGS84 longitude/latitude ranges.")
        normalized.append((longitude, latitude))

    if normalized[-1] != normalized[0]:
        raise HTTPException(status_code=422, detail="AOI polygon must be closed by repeating its first coordinate.")
    normalized.pop()
    if len(normalized) < 3:
        raise HTTPException(status_code=422, detail="AOI must contain at least 3 distinct polygon vertices.")
    return normalized


def _area_km2(coordinates: list[tuple[float, float]]) -> float:
    ring = [*coordinates, coordinates[0]]
    total = 0.0
    for (longitude_a, latitude_a), (longitude_b, latitude_b) in zip(ring, ring[1:]):
        delta_longitude = math.radians(longitude_b - longitude_a)
        if delta_longitude > math.pi:
            delta_longitude -= 2 * math.pi
        if delta_longitude < -math.pi:
            delta_longitude += 2 * math.pi
        total += delta_longitude * (
            math.sin(math.radians(latitude_a)) + math.sin(math.radians(latitude_b))
        )
    return abs(total) * EARTH_RADIUS_METERS**2 / 2 / 1_000_000


def _validate_request(request: CatalogSearchRequest) -> tuple[float, list[tuple[float, float]]]:
    coordinates = _validate_coordinates(request.coordinates)
    area = _area_km2(coordinates)
    if not math.isfinite(area) or area <= 0:
        raise HTTPException(status_code=422, detail="AOI must have positive area.")
    if area > MAX_AOI_AREA_KM2:
        raise HTTPException(status_code=422, detail=f"AOI exceeds the {MAX_AOI_AREA_KM2} km2 maximum.")

    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date.")
    if (request.end_date - request.start_date).days > MAX_DATE_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range cannot exceed {MAX_DATE_WINDOW_DAYS} days.")
    if request.end_date > date.today():
        raise HTTPException(status_code=422, detail="end_date cannot be in the future.")
    return area, coordinates


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sarguardian-catalogue-api"}


@app.post("/api/catalog/search", response_model=CatalogResponse)
def search_catalog(request: CatalogSearchRequest, _user: dict = Depends(get_current_user)) -> CatalogResponse:
    area, coordinates = _validate_request(request)
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    query = {
        "platform": "NISAR",
        "product_type": request.product_type,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "aoi_area_km2": area,
    }
    requested_product_types = "GUNW,GOFF" if request.product_type == "BOTH" else request.product_type
    try:
        result = search_asf_catalog(coordinates, request.start_date, request.end_date, requested_product_types)
    except ASFAdapterNotConfigured:
        return CatalogResponse(
            status="not_configured",
            message="Live ASF catalogue search is disabled by server configuration. No products were returned.",
            checked_at=checked_at,
            query=query,
            processing_eligibility={"status": "undetermined", "reason": ELIGIBILITY_UNDETERMINED_REASON},
            next_step="Set ASF_CATALOG_ENABLED=true to enable the read-only ASF metadata adapter.",
            warnings=["Metadata preflight only: no satellite files were downloaded and no scientific movement analysis was performed."],
        )
    except ASFAdapterError as exc:
        return JSONResponse(status_code=502, content={"status": "upstream_error", "message": str(exc)})

    products, unknown_size, metadata_warnings = normalize_product_rows(result.rows, requested_product_types)
    total_gib = sum(product["size_mb"] or 0 for product in products) / 1024
    warnings = [*metadata_warnings]
    if result.truncated:
        warnings.append("ASF returned the configured result cap; narrow the AOI or date range for a complete search.")
    if unknown_size:
        warnings.append(f"Size metadata is unavailable for {unknown_size} product(s); the volume estimate is incomplete.")
    if not products:
        status = "no_data"
        message = "ASF catalogue search completed with no eligible products matching this AOI and date range."
        next_step = "Try another date range, AOI, or supported product family."
    else:
        status = "ready"
        message = "ASF catalogue metadata search completed. No satellite files were downloaded."
        next_step = "Review the returned metadata before any separate scientific processing workflow."
    response = CatalogResponse(
        status=status,
        message=message,
        checked_at=checked_at,
        query=query,
        processing_eligibility={"status": "undetermined", "reason": ELIGIBILITY_UNDETERMINED_REASON},
        product_count=len(products),
        truncated=result.truncated,
        products_with_unknown_size=unknown_size,
        estimated_full_granule_volume_gib=total_gib,
        estimated_full_granule_volume_is_complete=unknown_size == 0,
        products=products,
        next_step=next_step,
        warnings=[*warnings, "Metadata preflight only: no satellite files were downloaded and no scientific movement analysis was performed."],
    )
    if status != "ready" or request.product_type == "BOTH":
        return response

    try:
        store = get_job_store()
        response.preflight_id = store.save_preflight(
            str(_user["uid"]),
            request.model_dump(mode="json"),
            response.model_dump(mode="json"),
        )
    except PersistenceConfigurationError as exc:
        return JSONResponse(status_code=503, content={"status": "persistence_not_configured", "message": str(exc)})
    except PersistenceError:
        return JSONResponse(status_code=503, content={"status": "persistence_error", "message": "The preflight could not be persisted for job confirmation."})
    return response


def _required_owner_uid(user: dict) -> str:
    owner_uid = user.get("uid")
    if not isinstance(owner_uid, str) or not owner_uid:
        raise HTTPException(status_code=401, detail="Authenticated Firebase user identity is unavailable.")
    return owner_uid


def _require_store():
    try:
        return get_job_store()
    except PersistenceConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _job_response(record: dict) -> JobResponse:
    return JobResponse.model_validate(record)


def _displacement_response(record: dict) -> DisplacementJobResponse:
    return DisplacementJobResponse.model_validate(record)


@app.post("/api/jobs", response_model=JobResponse)
def create_job(request: CreateJobRequest, user: dict = Depends(get_current_user)) -> JobResponse:
    owner_uid = _required_owner_uid(user)
    store = _require_store()
    try:
        preflight = store.get_preflight(owner_uid, request.preflight_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the preflight persistence service.") from None
    if not preflight:
        raise HTTPException(status_code=404, detail="Preflight manifest was not found for this user.")
    try:
        expires_at = datetime.fromisoformat(str(preflight["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Preflight manifest is invalid; run the catalogue search again.") from None
    if expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail="Preflight manifest has expired; run the catalogue search again.")
    manifest = preflight.get("manifest_snapshot")
    request_snapshot = preflight.get("request_snapshot")
    if not isinstance(manifest, dict) or manifest.get("status") != "ready" or not isinstance(request_snapshot, dict):
        raise HTTPException(status_code=409, detail="Only a current successful preflight can be confirmed as a job.")

    try:
        existing = store.get_job_by_idempotency(owner_uid, request.idempotency_key)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    if existing:
        return _job_response(existing)
    values = {
        "id": str(uuid4()),
        "owner_uid": owner_uid,
        "preflight_id": str(request.preflight_id),
        "idempotency_key": request.idempotency_key,
        "product_family": request_snapshot.get("product_type"),
        "state": JobState.QUEUED.value,
        "request_snapshot": request_snapshot,
        "manifest_snapshot": manifest,
    }
    try:
        return _job_response(store.create_job(values))
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/jobs", response_model=list[JobResponse])
def list_jobs(user: dict = Depends(get_current_user)) -> list[JobResponse]:
    owner_uid = _required_owner_uid(user)
    try:
        records = _require_store().list_jobs(owner_uid)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    return [_job_response(record) for record in records]


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: UUID, user: dict = Depends(get_current_user)) -> JobResponse:
    owner_uid = _required_owner_uid(user)
    try:
        record = _require_store().get_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Processing job was not found for this user.")
    return _job_response(record)


def _validate_displacement_request(request: CreateDisplacementJobRequest) -> None:
    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date.")
    if (request.end_date - request.start_date).days > MAX_DATE_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range cannot exceed {MAX_DATE_WINDOW_DAYS} days.")
    if request.end_date > date.today():
        raise HTTPException(status_code=422, detail="end_date cannot be in the future.")
    if external_workflow.is_external_mode(get_settings()) and request.product_type != "BOTH":
        raise HTTPException(
            status_code=422,
            detail="This deployment runs the upstream workflow, which always processes both GUNW and GOFF products. Choose BOTH.",
        )


def _dispatch_for_mode(settings, job_id: UUID, request: CreateDisplacementJobRequest) -> dict:
    """Start the workflow for this deployment mode and return run details worth persisting."""
    if external_workflow.is_external_mode(settings):
        if not getattr(settings, "displacement_actions_enabled", False):
            raise external_workflow.ExternalDispatchError("Displacement processing is not configured on this server.")
        run = external_workflow.dispatch(settings, external_workflow.build_dispatch_inputs(
            aoi_wkt=request.aoi_wkt, start_date=request.start_date.isoformat(), end_date=request.end_date.isoformat(),
            acquisition_window=request.acquisition_window, retention_mode=request.retention_mode,
            processing_profile=request.processing_profile, product_type=request.product_type,
        ))
        return {"github_run_id": run.run_id, "github_run_url": run.html_url}
    dispatch_workflow(settings, job_id, request)
    return {}


def _create_and_dispatch_displacement_job(owner_uid: str, request: CreateDisplacementJobRequest, store) -> DisplacementJobResponse:
    try:
        existing = store.get_displacement_job_by_idempotency(owner_uid, request.idempotency_key)
        active = store.find_active_displacement_job(owner_uid, request.aoi_wkt, request.start_date.isoformat(), request.end_date.isoformat())
        active_jobs = store.list_active_displacement_jobs(owner_uid)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not check displacement job persistence.") from None
    if existing:
        if displacement_request_fingerprint(existing) != displacement_request_fingerprint(request.model_dump()):
            raise HTTPException(status_code=409, detail="That idempotency key is already bound to a different displacement request.")
        return _displacement_response(existing)
    if active:
        # A durable active request already exists for this account and exact
        # immutable payload. Never use a page refresh or second browser tab to
        # dispatch a second science workflow.
        if displacement_request_fingerprint(active) == displacement_request_fingerprint(request.model_dump()):
            return _displacement_response(active)
        raise HTTPException(status_code=409, detail="A different processing configuration is already active for this AOI and date window.")
    if active_jobs:
        raise HTTPException(status_code=429, detail="This account already has an active scientific processing job. Wait for it to finish or cancel it before submitting another.")
    settings = get_settings()
    values = {
        "owner_uid": owner_uid,
        "idempotency_key": request.idempotency_key,
        "state": JobState.QUEUED.value,
        "aoi_wkt": request.aoi_wkt,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        **{name: getattr(request, name) for name in PROCESSING_OPTION_DEFAULTS},
        "request_fingerprint": displacement_request_fingerprint(request.model_dump()),
        # Callback-bound jobs use contract 2; jobs run by an unchanged external workflow have
        # no callback, so they stay NULL (the database check allows only NULL or 2).
        "worker_contract_version": None if external_workflow.is_external_mode(settings) else 2,
        "stage": "queued",
    }
    try:
        record = store.create_displacement_job(values)
    except PersistenceError:
        try:
            active = store.find_active_displacement_job(owner_uid, request.aoi_wkt, request.start_date.isoformat(), request.end_date.isoformat())
        except PersistenceError:
            active = None
        if active:
            return _displacement_response(active)
        raise HTTPException(status_code=503, detail="Could not persist the displacement job.") from None
    job_id = UUID(str(record["id"]))
    try:
        run_details = _dispatch_for_mode(settings, job_id, request)
        record = store.update_displacement_job(job_id, {"state": JobState.DISPATCHING.value, "stage": "dispatching", **run_details})
    except (GitHubDispatchError, external_workflow.ExternalWorkflowError) as exc:
        logger.warning("Displacement dispatch failed for job %s: %s", job_id, exc)
        try:
            record = store.update_displacement_job(job_id, {
                "state": JobState.FAILED.value,
                "stage": "dispatch_failed",
                "error_code": getattr(exc, "code", "GITHUB_DISPATCH_FAILED"),
                "error_message": str(exc)[:500],
            })
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Displacement dispatch failed and its status could not be persisted.") from None
    except PersistenceError:
        raise HTTPException(status_code=503, detail="GitHub Actions accepted the job but its dispatch status could not be persisted.") from None
    return _displacement_response(record)


@app.post("/api/displacement-jobs", response_model=DisplacementJobResponse)
def create_displacement_job(request: CreateDisplacementJobRequest, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    _validate_displacement_request(request)
    return _create_and_dispatch_displacement_job(_required_owner_uid(user), request, _require_store())


@app.get("/api/displacement-jobs", response_model=list[DisplacementJobResponse])
def list_displacement_jobs(user: dict = Depends(get_current_user)) -> list[DisplacementJobResponse]:
    try:
        records = _require_store().list_displacement_jobs(_required_owner_uid(user))
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not list displacement jobs.") from None
    return [_displacement_response(record) for record in records]


def _cancel_active_displacement_job(owner_uid: str, job_id: UUID, store) -> dict:
    """Cancel a GitHub run and persist cancellation before any replacement exists."""
    try:
        current = store.get_displacement_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the current displacement job.") from None
    if not current:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")
    if current.get("state") not in {JobState.DISPATCHING.value, JobState.RUNNING.value}:
        raise HTTPException(status_code=409, detail="Only a currently dispatched or running job can be replaced.")

    settings = get_settings()
    run_id = current.get("github_run_id")
    if not run_id:
        discovered = find_dispatched_workflow_run(settings, job_id)
        if not discovered:
            raise HTTPException(status_code=409, detail="The current workflow has not been allocated by GitHub yet. Wait a moment and try refresh again; no replacement was created.")
        run_id, run_url, run_status, _conclusion = discovered
        if run_status == "completed":
            raise HTTPException(status_code=409, detail="The current workflow already completed. Refresh its status before starting a new request.")
        try:
            current = store.update_displacement_job(job_id, {"github_run_id": run_id, "github_run_url": run_url})
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Could not record the current GitHub Actions run.") from None

    try:
        cancel_workflow_run(settings, str(run_id))
    except GitHubCancellationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        return store.update_displacement_job(job_id, {
            "state": JobState.CANCELLED.value,
            "stage": "cancelled_by_user",
            "error_code": "CANCELLED_BY_USER",
            "error_message": "Cancelled by the account owner.",
        })
    except PersistenceError:
        raise HTTPException(status_code=503, detail="GitHub Actions was cancelled but the account job state could not be updated.") from None


@app.post("/api/displacement-jobs/{job_id}/cancel", response_model=DisplacementJobResponse)
def cancel_displacement_job(job_id: UUID, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    """Explicit account-owner cancellation; it never creates a new workflow."""
    owner_uid = _required_owner_uid(user)
    return _displacement_response(_cancel_active_displacement_job(owner_uid, job_id, _require_store()))


@app.post("/api/displacement-jobs/{job_id}/resubmit", response_model=DisplacementJobResponse)
def resubmit_displacement_job(
    job_id: UUID, request: ResubmitDisplacementJobRequest, user: dict = Depends(get_current_user),
) -> DisplacementJobResponse:
    """Retry a failed job or replace an active workflow from immutable stored inputs."""
    owner_uid = _required_owner_uid(user)
    store = _require_store()
    try:
        current = store.get_displacement_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the current displacement job.") from None
    if not current:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")

    if current.get("state") == JobState.FAILED.value:
        # A failed job has no active worker to cancel. Its stored WKT and dates
        # remain the sole source for a retry, preventing payload substitution.
        source = current
    elif current.get("state") in {JobState.DISPATCHING.value, JobState.RUNNING.value}:
        source = _cancel_active_displacement_job(owner_uid, job_id, store)
    else:
        raise HTTPException(status_code=409, detail="Only a failed, dispatched, or running job can be retried or refreshed.")

    replacement = CreateDisplacementJobRequest(
        aoi_wkt=str(source["aoi_wkt"]), start_date=source["start_date"], end_date=source["end_date"],
        idempotency_key=request.idempotency_key,
        **{name: source.get(name, default) for name, default in PROCESSING_OPTION_DEFAULTS.items()},
    )
    return _create_and_dispatch_displacement_job(owner_uid, replacement, store)


@app.get("/api/displacement-jobs/{job_id}", response_model=DisplacementJobResponse)
def get_displacement_job(job_id: UUID, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    store = _require_store()
    try:
        record = store.get_displacement_job(_required_owner_uid(user), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the displacement job.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")
    settings = get_settings()
    if _is_external_job(settings, record):
        return _displacement_response(_refresh_external_job(store, record, settings))
    if settings.displacement_actions_enabled and record.get("state") == JobState.DISPATCHING.value:
        discovered_run = find_dispatched_workflow_run(settings, job_id)
        if discovered_run:
            run_id, run_url, status, conclusion = discovered_run
            updates = {"github_run_id": run_id, "github_run_url": run_url}
            should_persist = (
                run_id != record.get("github_run_id") or run_url != record.get("github_run_url")
            )
            if status == "completed":
                code = "WORKER_CALLBACK_MISSING" if conclusion == "success" else "GITHUB_ACTIONS_WORKER_FAILED"
                message = "GitHub Actions completed without a verified success callback." if conclusion == "success" else "GitHub Actions reported a terminal worker failure before its startup callback."
                updates |= {"state": JobState.FAILED.value, "stage": "terminal_validation_failed", "error_code": code, "error_message": message}
                should_persist = True
            if should_persist:
                try:
                    record = store.update_displacement_job(job_id, updates)
                except PersistenceError:
                    raise HTTPException(status_code=503, detail="Could not persist the discovered workflow state.") from None
    if is_dispatch_timed_out(record, settings.displacement_dispatch_timeout_seconds):
        try:
            record = store.update_displacement_job(job_id, {"state": JobState.FAILED.value, "stage": "start_timeout", "error_code": "WORKFLOW_START_TIMEOUT", "error_message": "The workflow did not report a start before the configured timeout."})
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Could not persist the workflow timeout.") from None
    elif settings.displacement_actions_enabled and record.get("github_run_id") and record.get("state") == JobState.RUNNING.value:
        status = workflow_run_status(settings, str(record["github_run_id"]))
        if status and status[0] == "completed":
            code = "WORKER_CALLBACK_MISSING" if status[1] == "success" else "GITHUB_ACTIONS_WORKER_FAILED"
            message = "GitHub Actions completed without a verified success callback." if status[1] == "success" else "GitHub Actions reported a terminal worker failure."
            try:
                record = store.update_displacement_job(job_id, {"state": JobState.FAILED.value, "stage": "terminal_validation_failed", "error_code": code, "error_message": message})
            except PersistenceError:
                raise HTTPException(status_code=503, detail="Could not persist the terminal worker state.") from None
    return _displacement_response(record)


def _is_external_job(settings, record: dict) -> bool:
    """Jobs started by an unchanged external workflow carry no callback contract version."""
    return (external_workflow.is_external_mode(settings) and bool(getattr(settings, "displacement_actions_enabled", False))
            and record.get("worker_contract_version") is None)


def _refresh_external_job(store, record: dict, settings) -> dict:
    """Bring an externally run job up to date with its GitHub run.

    GitHub is asked at most once every few seconds per job, and a GitHub or parsing problem
    never turns a status read into an error: the stored job is simply returned unchanged.
    """
    if record.get("state") not in {JobState.DISPATCHING.value, JobState.RUNNING.value}:
        return record
    job_id = UUID(str(record["id"]))
    if not external_workflow.should_poll(str(job_id)):
        return record
    try:
        steps = external_workflow.plan_refresh(settings, record)
    except Exception:  # noqa: BLE001 - status reads must survive GitHub and parsing problems
        logger.warning("Could not refresh the external workflow state for job %s.", job_id, exc_info=True)
        return record
    current = record
    for step in steps:
        try:
            current = store.update_displacement_job(job_id, step.values, expected_state=step.expected_state)
        except PersistenceConflictError:
            # Another status request already advanced this job; use the stored result.
            try:
                return store.get_displacement_job_unscoped(job_id) or current
            except PersistenceError:
                return current
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Could not persist the workflow state.") from None
    return current


@app.post("/api/displacement-jobs/{job_id}/callback")
async def update_displacement_job_from_worker(job_id: UUID, request: Request) -> dict[str, str]:
    body = await request.body()
    settings = get_settings()
    if not callback_signature_is_valid(settings.displacement_callback_secret, body, request.headers.get("X-SARGuardian-Signature")):
        raise HTTPException(status_code=401, detail="Invalid worker callback signature.")
    try:
        payload = WorkerCallback.model_validate_json(body)
        store = _require_store()
        record = store.get_displacement_job_unscoped(job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read worker job state.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Displacement job was not found.")
    if record.get("worker_contract_version") == 2:
        if payload.contract_version != 2 or payload.job_id != job_id:
            raise HTTPException(status_code=409, detail="Worker callback is not bound to this version-2 job.")
        expected_fingerprint = record.get("request_fingerprint") or displacement_request_fingerprint(record)
        if payload.request_fingerprint != expected_fingerprint:
            raise HTTPException(status_code=409, detail="Worker callback request fingerprint does not match this job.")
    current = JobState(record["state"])
    target = JobState(payload.state)
    stored_sequence = int(record.get("callback_sequence") or 0)
    stored_run_id = record.get("github_run_id")
    if payload.sequence < stored_sequence:
        raise HTTPException(status_code=409, detail="Stale worker callback sequence.")
    if payload.sequence == stored_sequence:
        if current == target and stored_run_id == payload.github_run_id and record.get("stage") == payload.stage:
            return {"status": "accepted", "job_id": str(job_id), "job_status": current.value}
        raise HTTPException(status_code=409, detail="Duplicate worker callback does not match the recorded event.")
    if payload.sequence != stored_sequence + 1:
        raise HTTPException(status_code=409, detail="Worker callback sequence is out of order.")
    if stored_run_id and stored_run_id != payload.github_run_id:
        raise HTTPException(status_code=409, detail="Worker callback run does not match the recorded workflow run.")
    if target == JobState.SUCCEEDED and current != JobState.RUNNING:
        raise HTTPException(status_code=409, detail="A result cannot be accepted before the worker reports running.")
    if target == JobState.SUCCEEDED and record.get("worker_contract_version") == 2:
        summary = payload.result_summary
        if (not isinstance(summary, dict) or summary.get("schema_version") != 2
                or not isinstance(summary.get("validation"), dict)
                or summary["validation"].get("status") != "PASS"
                or not isinstance(summary.get("storage_verification"), dict)
                or summary["storage_verification"].get("status") != "PASS"
                or not _valid_private_artifacts(summary.get("artifacts"))):
            raise HTTPException(status_code=409, detail="Version-2 success requires a validated scientific result contract.")
    if target == JobState.RUNNING and payload.result_summary is not None:
        summary = payload.result_summary
        counts = summary.get("catalog_granule_counts") if isinstance(summary, dict) else None
        selected_counts = summary.get("selected_granule_counts") if isinstance(summary, dict) else None
        valid_counts = lambda value: isinstance(value, dict) and set(value) == {"GUNW", "GOFF"} and all(
            isinstance(value[family], int) and not isinstance(value[family], bool) and 0 <= value[family] <= 10_000
            for family in ("GUNW", "GOFF")
        )
        if summary.get("schema_version") != 2 or not valid_counts(counts) or not valid_counts(selected_counts):
            raise HTTPException(status_code=409, detail="Worker progress summary contains invalid product counts.")
    allowed = {
        (JobState.DISPATCHING, JobState.RUNNING), (JobState.DISPATCHING, JobState.FAILED),
        (JobState.RUNNING, JobState.RUNNING), (JobState.RUNNING, JobState.SUCCEEDED), (JobState.RUNNING, JobState.FAILED),
    }
    if (current, target) not in allowed:
        raise HTTPException(status_code=409, detail="Worker callback requested an invalid state transition.")
    try:
        retained_summary = (payload.result_summary if target in {JobState.RUNNING, JobState.SUCCEEDED}
                            and payload.result_summary is not None else record.get("result_summary"))
        updated = store.update_displacement_job(job_id, {
            "state": target.value, "github_run_id": payload.github_run_id,
            "github_run_url": payload.github_run_url, "error_code": payload.error_code if target == JobState.FAILED else None,
            "error_message": payload.error_message if target == JobState.FAILED else None, "callback_sequence": payload.sequence,
            "stage": payload.stage, "result_summary": retained_summary,
            "artifact_url": payload.artifact_url if target == JobState.SUCCEEDED else None,
        }, expected_state=current.value, expected_sequence=stored_sequence)
    except PersistenceConflictError:
        raise HTTPException(status_code=409, detail="Job state changed while this callback was being accepted.") from None
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not persist worker job state.") from None
    return {"status": "accepted", "job_id": str(job_id), "job_status": updated["state"]}


def _valid_private_artifacts(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    import re
    for artifact in value:
        if not isinstance(artifact, dict):
            return False
        path, digest, size, drive_id = (artifact.get("path"), artifact.get("sha256"),
                                        artifact.get("bytes"), artifact.get("drive_file_id"))
        if (not isinstance(path, str) or path.startswith("/") or ".." in path.split("/")
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(size, int) or size <= 0
                or not isinstance(drive_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", drive_id)):
            return False
    return True


@app.get("/api/displacement-jobs/{job_id}/artifacts/{artifact_index}")
async def download_displacement_artifact(job_id: UUID, artifact_index: int, user: dict = Depends(get_current_user)) -> Response:
    """Stream one private, verified Drive artifact to its authenticated owner."""
    if artifact_index < 0 or artifact_index > 10_000:
        raise HTTPException(status_code=404, detail="Result artifact was not found.")
    try:
        record = _require_store().get_displacement_job(str(user["uid"]), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the requested result artifact.") from None
    summary = record.get("result_summary") if record else None
    artifacts = summary.get("artifacts") if isinstance(summary, dict) else None
    if not _valid_private_artifacts(artifacts) or artifact_index >= len(artifacts):
        raise HTTPException(status_code=404, detail="Result artifact was not found.")
    artifact = artifacts[artifact_index]
    try:
        received, data = download_private_result(artifact["drive_file_id"])
    except DriveUploadError:
        raise HTTPException(status_code=503, detail="Private result storage is unavailable. Retry the download.") from None
    import hashlib
    if received["bytes"] != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise HTTPException(status_code=409, detail="Stored result artifact no longer matches the validated inventory.")
    filename = artifact["path"].rsplit("/", 1)[-1]
    return Response(content=data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store",
    })


@app.get("/api/displacement-jobs/{job_id}/result-link")
def get_displacement_result_link(job_id: UUID, user: dict = Depends(get_current_user)) -> JSONResponse:
    """Give the job owner a short-lived signed GitHub storage link for the whole result ZIP.

    The ZIP can be hundreds of MB, so it is downloaded by the browser straight from GitHub
    instead of passing through this API; the link is only issued to the authenticated owner.
    """
    try:
        record = _require_store().get_displacement_job(_required_owner_uid(user), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the displacement job.") from None
    summary = record.get("result_summary") if record else None
    if (not record or record.get("state") != JobState.SUCCEEDED.value or not isinstance(summary, dict)
            or summary.get("source") != external_workflow.RESULT_SOURCE):
        raise HTTPException(status_code=404, detail="No downloadable result package was found for this job.")
    try:
        url, filename = external_workflow.result_download_link(get_settings(), summary)
    except external_workflow.ArtifactUnavailableError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    except external_workflow.GitHubApiError:
        raise HTTPException(status_code=503, detail="GitHub result storage is unavailable. Retry the download.") from None
    except external_workflow.ExternalWorkflowError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse(
        content={"url": url, "filename": filename, "expires_in_seconds": 60},
        headers={"Cache-Control": "private, no-store"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=get_settings().port)
