"""Run the *unchanged* upstream dynamic-AOI workflow from the website and read its output back.

``DISPLACEMENT_WORKFLOW_MODE=external`` makes the account API behave like this:

* the website submits an AOI/date request to the authenticated API exactly as before;
* the API dispatches the existing ``SARGuardian AOI WKT E2E + Dashboard`` workflow
  (``sarguardian-aoi-wkt-dashboard.yml``) in the repository named by
  ``GITHUB_ACTIONS_REPOSITORY``.  That repository can belong to a different GitHub
  account, and its workflow is not modified in any way;
* the API follows that run through the GitHub REST API (the upstream workflow has no
  callback) and, once it succeeds, reads the dashboard artifact the workflow already
  publishes.  Only the small ``aoi-processing-report.json`` member is read, using HTTP
  range requests, so a very large artifact never has to pass through the API;
* the browser downloads the full result ZIP straight from GitHub through a short-lived
  signed link that only the owner of the job can request.

Only the Python standard library is used so the module can be imported (and tested) in
any runtime.
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import math
import re
import threading
import time
import zipfile
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

logger = logging.getLogger(__name__)

RESULT_SOURCE = "github_artifact"
DASHBOARD_ARTIFACT_PREFIX = "sarguardian-aoi-dashboard"
MANIFEST_ARTIFACT_NAME = "sarguardian-aoi-run-manifest"
REPORT_MEMBER = "aoi-processing-report.json"
MANIFEST_MEMBER = "manifest.json"

API_VERSION = "2022-11-28"
USER_AGENT = "sarguardian-dynamic-api"
API_TIMEOUT_SECONDS = 15
BLOB_TIMEOUT_SECONDS = 25
MAX_JSON_MEMBER_BYTES = 16 * 1024 * 1024
MAX_REMOTE_BYTES = 96 * 1024 * 1024
FULL_DOWNLOAD_FALLBACK_BYTES = 64 * 1024 * 1024
BLOCK_BYTES = 1024 * 1024
MAX_CACHED_BLOCKS = 8
MAX_SUMMARY_PRODUCTS = 500
MAX_DIAGNOSTICS_JSON_BYTES = 20_000
MIN_POLL_INTERVAL_SECONDS = 8.0
DEFAULT_START_TIMEOUT_SECONDS = 3_600
# GitHub always signs artifact storage URLs over https; tests may widen this for a local server.
ALLOWED_STORAGE_SCHEMES: tuple[str, ...] = ("https",)

# The upstream workflow's own option names (see its ``workflow_dispatch`` inputs).
UPSTREAM_RUN_PROFILE = {"validation-pilot": "validation-pilot", "full-aoi": "full-aoi-baseline"}
SCIENTIFIC_SCOPE = "upstream_dynamic_aoi_structural_and_provenance_validation_not_independent_geophysical_validation"
DEFAULT_SCIENTIFIC_LIMIT = "Requested-AOI outputs are research products, not a verified hazard warning."

_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_RUN_ID_RE = re.compile(r"[0-9]{1,20}")
_SHA256_RE = re.compile(r"[a-f0-9]{64}")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")


class ExternalWorkflowError(RuntimeError):
    """A safe, user-presentable failure; never contains a credential."""

    code = "EXTERNAL_WORKFLOW_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ExternalDispatchError(ExternalWorkflowError):
    code = "GITHUB_DISPATCH_FAILED"


class GitHubApiError(ExternalWorkflowError):
    """A GitHub (or GitHub result-storage) request failed.

    ``transient`` errors (network, 5xx, rate limit) must never fail a job; the caller
    keeps the job as it is and tries again on the next status request.
    """

    code = "GITHUB_API_ERROR"

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False, detail: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.detail = detail


class ArtifactUnavailableError(ExternalWorkflowError):
    code = "RESULT_ARTIFACT_UNAVAILABLE"


class ResultContractError(ExternalWorkflowError):
    code = "RESULT_CONTRACT_VALIDATION_FAILED"

    def __init__(self, message: str, *, code: str | None = None, progress: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code)
        self.progress = progress


@dataclass(frozen=True)
class DispatchedRun:
    run_id: str
    html_url: str | None


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    status: str
    conclusion: str | None
    html_url: str | None
    run_attempt: int


@dataclass(frozen=True)
class ArtifactInfo:
    id: int
    name: str
    size_in_bytes: int
    expired: bool
    expires_at: str | None
    run_id: str | None


@dataclass(frozen=True)
class RefreshStep:
    """One database update, applied only if the job is still in ``expected_state``."""

    expected_state: str
    values: dict[str, Any]


# --------------------------------------------------------------------------- settings


def _repository(settings: Any) -> str:
    repository = str(getattr(settings, "github_actions_repository", "") or "").strip()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ExternalWorkflowError("GITHUB_ACTIONS_REPOSITORY must look like owner/repository.", code="EXTERNAL_WORKFLOW_NOT_CONFIGURED")
    return repository


def _api_url(settings: Any) -> str:
    return str(getattr(settings, "github_api_url", "") or "https://api.github.com").rstrip("/")


def _headers(settings: Any) -> dict[str, str]:
    token = str(getattr(settings, "github_actions_token", "") or "").strip()
    if not token:
        raise ExternalWorkflowError("GITHUB_ACTIONS_TOKEN is not configured on this server.", code="EXTERNAL_WORKFLOW_NOT_CONFIGURED")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


def _check_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(str(run_id)):
        raise ExternalWorkflowError("The recorded GitHub run id is invalid.", code="GITHUB_RUN_ID_INVALID")
    return str(run_id)


def is_external_mode(settings: Any) -> bool:
    """Tolerant of the lightweight settings stand-ins used by existing tests."""
    return getattr(settings, "displacement_workflow_mode", "callback") == "external"


# --------------------------------------------------------------------------- HTTP


def _api_error(exc: HTTPError) -> GitHubApiError:
    status = exc.code
    message: str | None = None
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        candidate = payload.get("message") if isinstance(payload, dict) else None
        message = candidate if isinstance(candidate, str) and candidate else None
    except Exception:  # noqa: BLE001 - an unreadable error body must never mask the status
        message = None
    headers = exc.headers if exc.headers is not None else {}
    rate_limited = status == 429 or (status == 403 and (headers.get("X-RateLimit-Remaining") == "0" or bool(headers.get("Retry-After"))))
    suffix = f": {message[:200]}" if message else ""
    return GitHubApiError(
        f"GitHub API request failed ({status}){suffix}.", status=status,
        transient=status >= 500 or rate_limited, detail=message[:200] if message else None,
    )


_NETWORK_ERRORS = (URLError, TimeoutError, OSError, http.client.HTTPException)


def _github_json(settings: Any, method: str, path: str, *, body: dict[str, Any] | None = None,
                 timeout: int = API_TIMEOUT_SECONDS) -> tuple[int, Any]:
    url = f"{_api_url(settings)}/repos/{_repository(settings)}/{path}"
    headers = _headers(settings)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except HTTPError as exc:
        raise _api_error(exc) from None
    except _NETWORK_ERRORS:
        raise GitHubApiError("Could not reach GitHub.", transient=True) from None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GitHubApiError("GitHub returned an unreadable response.", status=status, transient=True) from None


# --------------------------------------------------------------------------- dispatch


def build_dispatch_inputs(*, aoi_wkt: str, start_date: str, end_date: str, acquisition_window: str,
                          retention_mode: str, processing_profile: str, product_type: str) -> dict[str, str]:
    """Translate the website request into the upstream workflow's six ``workflow_dispatch`` inputs.

    GitHub rejects inputs a workflow does not declare, so nothing else may be sent.
    """
    if product_type != "BOTH":
        raise ExternalDispatchError(
            "The upstream workflow always processes both GUNW and GOFF products; choose BOTH for this deployment.",
            code="EXTERNAL_PRODUCT_FILTER_UNSUPPORTED",
        )
    if processing_profile not in UPSTREAM_RUN_PROFILE:
        raise ExternalDispatchError("Unsupported processing profile.", code="EXTERNAL_PROFILE_UNSUPPORTED")
    return {
        "aoi_wkt": aoi_wkt,
        "start_date": start_date,
        "end_date": end_date,
        "date_filter_mode": acquisition_window,
        "run_profile": UPSTREAM_RUN_PROFILE[processing_profile],
        "retention_profile": retention_mode,
    }


def dispatch(settings: Any, inputs: Mapping[str, str]) -> DispatchedRun:
    workflow = str(getattr(settings, "github_actions_workflow", "") or "").strip()
    ref = str(getattr(settings, "github_actions_ref", "") or "").strip()
    if not workflow or not ref:
        raise ExternalDispatchError("GITHUB_ACTIONS_WORKFLOW and GITHUB_ACTIONS_REF must be configured.", code="EXTERNAL_WORKFLOW_NOT_CONFIGURED")
    try:
        status, payload = _github_json(
            settings, "POST", f"actions/workflows/{quote(workflow, safe='')}/dispatches",
            body={"ref": ref, "inputs": dict(inputs), "return_run_details": True},
        )
    except GitHubApiError as exc:
        if exc.status is None:
            raise ExternalDispatchError("Could not reach GitHub Actions to dispatch the workflow.") from None
        suffix = f": {exc.detail}" if exc.detail else ""
        raise ExternalDispatchError(f"GitHub Actions dispatch was rejected ({exc.status}){suffix}.") from None
    except ExternalWorkflowError as exc:
        raise ExternalDispatchError(str(exc), code=exc.code) from None
    run_id = payload.get("workflow_run_id") if isinstance(payload, dict) else None
    if status != 200 or isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        # Binding a run to an account by guessing would risk showing someone else's result,
        # so an untrackable dispatch is reported instead of being guessed at.
        raise ExternalDispatchError(
            "GitHub accepted the dispatch but did not return the workflow run id, so it cannot be tracked safely. "
            "Check the repository's Actions tab for an unattached run.",
            code="EXTERNAL_RUN_ID_UNAVAILABLE",
        )
    html_url = payload.get("html_url")
    return DispatchedRun(str(run_id), html_url if isinstance(html_url, str) and html_url.startswith("https://") else None)


def cancel_run(settings: Any, run_id: str) -> None:
    _github_json(settings, "POST", f"actions/runs/{_check_run_id(run_id)}/cancel")


# --------------------------------------------------------------------------- runs & artifacts


def get_run(settings: Any, run_id: str) -> RunInfo:
    _status, payload = _github_json(settings, "GET", f"actions/runs/{_check_run_id(run_id)}")
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise GitHubApiError("GitHub returned an unreadable workflow run.", transient=True)
    conclusion = payload.get("conclusion")
    attempt = payload.get("run_attempt")
    html_url = payload.get("html_url")
    return RunInfo(
        run_id=str(run_id), status=payload["status"],
        conclusion=conclusion if isinstance(conclusion, str) else None,
        html_url=html_url if isinstance(html_url, str) and html_url.startswith("https://") else None,
        run_attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else 1,
    )


def _artifact_from(item: Any) -> ArtifactInfo | None:
    if not isinstance(item, dict):
        return None
    artifact_id, name, size = item.get("id"), item.get("name"), item.get("size_in_bytes")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or not isinstance(name, str):
        return None
    run = item.get("workflow_run")
    run_id = run.get("id") if isinstance(run, dict) else None
    expires_at = item.get("expires_at")
    return ArtifactInfo(
        id=artifact_id, name=name,
        size_in_bytes=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
        expired=bool(item.get("expired")), expires_at=expires_at if isinstance(expires_at, str) else None,
        run_id=str(run_id) if isinstance(run_id, int) and not isinstance(run_id, bool) else None,
    )


def find_run_artifact(settings: Any, run_id: str, name: str) -> ArtifactInfo | None:
    """Newest artifact with exactly this name in this run, or ``None`` if not uploaded (yet)."""
    _status, payload = _github_json(
        settings, "GET", f"actions/runs/{_check_run_id(run_id)}/artifacts?name={quote(name, safe='')}&per_page=100",
    )
    items = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise GitHubApiError("GitHub returned an unreadable artifact list.", transient=True)
    found = [artifact for artifact in map(_artifact_from, items) if artifact and artifact.name == name]
    return max(found, key=lambda artifact: artifact.id) if found else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def artifact_download_url(settings: Any, artifact_id: int) -> str:
    """Ask GitHub for the short-lived signed storage URL of one artifact ZIP.

    The redirect is *not* followed: the signed URL must be fetched without the GitHub
    credential, and it is also what the browser is handed for the user's download.
    """
    url = f"{_api_url(settings)}/repos/{_repository(settings)}/actions/artifacts/{int(artifact_id)}/zip"
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(Request(url, headers=_headers(settings)), timeout=API_TIMEOUT_SECONDS):
            pass
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            location = (exc.headers or {}).get("Location")
            target = urljoin(url, location) if location else ""
            if urlsplit(target).scheme not in ALLOWED_STORAGE_SCHEMES:
                raise GitHubApiError("GitHub returned an unsafe artifact location.", transient=True) from None
            return target
        if exc.code in {404, 410}:
            raise ArtifactUnavailableError(
                "The result artifact is no longer available on GitHub (artifacts expire after the workflow's retention period).",
            ) from None
        raise _api_error(exc) from None
    except _NETWORK_ERRORS:
        raise GitHubApiError("Could not reach GitHub.", transient=True) from None
    raise GitHubApiError("GitHub did not redirect the artifact download.", transient=True)


def _content_range_total(value: str | None) -> int | None:
    match = _CONTENT_RANGE_RE.fullmatch((value or "").strip())
    return int(match.group(1)) if match else None


class _RemoteFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests.

    ``zipfile`` needs the end of the archive (central directory) plus the few members we
    want, so only a few MiB are transferred even when the artifact is hundreds of MiB.
    If a server ignores ``Range`` the (bounded) body is kept in memory instead.
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self._pos = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._memory: bytes | None = None
        self.bytes_fetched = 0
        self._size = self._probe()

    def _open(self, start: int, end: int):  # type: ignore[no-untyped-def]
        request = Request(self._url, headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            return urlopen(request, timeout=BLOB_TIMEOUT_SECONDS)
        except HTTPError as exc:
            if exc.code in {403, 404, 410}:
                raise ArtifactUnavailableError("The result storage link was rejected or has expired.") from None
            raise GitHubApiError(f"GitHub result storage returned {exc.code}.", status=exc.code,
                                 transient=exc.code >= 500 or exc.code == 429) from None
        except _NETWORK_ERRORS:
            raise GitHubApiError("Could not reach the GitHub result storage.", transient=True) from None

    def _probe(self) -> int:
        with self._open(0, 0) as response:
            if response.status == 206:
                total = _content_range_total(response.headers.get("Content-Range"))
                if total is None or total <= 0:
                    raise GitHubApiError("GitHub result storage did not report the artifact size.", transient=True)
                response.read(1)
                self.bytes_fetched += 1
                return total
            if response.status == 200:
                length = response.headers.get("Content-Length")
                if not (length or "").isdigit() or int(length) > FULL_DOWNLOAD_FALLBACK_BYTES:
                    raise ExternalWorkflowError(
                        "The result storage does not support partial reads and the artifact is too large to inspect.",
                        code="RESULT_ARTIFACT_TOO_LARGE",
                    )
                body = response.read(FULL_DOWNLOAD_FALLBACK_BYTES + 1)
                if len(body) > FULL_DOWNLOAD_FALLBACK_BYTES:
                    raise ExternalWorkflowError("The result artifact is too large to inspect.", code="RESULT_ARTIFACT_TOO_LARGE")
                self._memory = body
                self.bytes_fetched += len(body)
                return len(body)
            raise GitHubApiError(f"GitHub result storage returned an unexpected status ({response.status}).", transient=True)

    def _block(self, index: int) -> bytes:
        cached = self._blocks.get(index)
        if cached is not None:
            self._blocks.move_to_end(index)
            return cached
        start = index * BLOCK_BYTES
        end = min(start + BLOCK_BYTES, self._size) - 1
        if self.bytes_fetched + (end - start + 1) > MAX_REMOTE_BYTES:
            raise ExternalWorkflowError("Inspecting the result artifact exceeded its transfer budget.", code="RESULT_ARTIFACT_TOO_LARGE")
        with self._open(start, end) as response:
            if response.status != 206:
                raise GitHubApiError("GitHub result storage stopped honouring range requests.", transient=True)
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise GitHubApiError("GitHub result storage returned a short read.", transient=True)
        self.bytes_fetched += len(data)
        self._blocks[index] = data
        while len(self._blocks) > MAX_CACHED_BLOCKS:
            self._blocks.popitem(last=False)
        return data

    # io.RawIOBase interface used by zipfile
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._pos + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._pos = position
        return position

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = self._size - self._pos
        size = min(size, max(0, self._size - self._pos))
        if size <= 0:
            return b""
        if self._memory is not None:
            chunk = self._memory[self._pos:self._pos + size]
            self._pos += len(chunk)
            return chunk
        out = bytearray()
        end = self._pos + size
        while self._pos < end:
            index = self._pos // BLOCK_BYTES
            block = self._block(index)
            offset = self._pos - index * BLOCK_BYTES
            part = block[offset:offset + (end - self._pos)]
            if not part:
                break
            out += part
            self._pos += len(part)
        return bytes(out)

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def read_zip_members(url: str, wanted: Mapping[str, int]) -> dict[str, bytes]:
    """Read the named members (matched by file name, any folder) of a remote ZIP.

    ``wanted`` maps each file name to its maximum accepted size.  Nothing is extracted to
    disk, so archive member paths can never be used to write files.
    """
    remote = _RemoteFile(url)
    try:
        try:
            archive = zipfile.ZipFile(remote)
        except (zipfile.BadZipFile, ValueError):
            raise ArtifactUnavailableError("The result artifact is not a readable ZIP archive.") from None
        with archive:
            matches: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                base = info.filename.rsplit("/", 1)[-1]
                if base in wanted:
                    if base in matches:
                        raise ResultContractError(f"Expected exactly one {base} in the result artifact.")
                    matches[base] = info
            missing = sorted(name for name in wanted if name not in matches)
            if missing:
                raise ResultContractError(f"The result artifact does not contain {', '.join(missing)}.")
            found: dict[str, bytes] = {}
            for base, info in matches.items():
                if info.file_size > wanted[base]:
                    raise ResultContractError(f"{base} is larger than the supported limit.")
                try:
                    found[base] = archive.read(info)
                except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError):
                    raise ResultContractError(f"{base} could not be read from the result artifact.") from None
            return found
    finally:
        remote.close()


# --------------------------------------------------------------------------- result contract


def _json_object(data: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResultContractError(f"{name} is not valid JSON.") from None
    if not isinstance(value, dict):
        raise ResultContractError(f"{name} has an unexpected shape.")
    return value


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000 else None


def _valid_counts(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"GUNW", "GOFF"} and all(_count(value[family]) is not None for family in ("GUNW", "GOFF"))


def _vertices(wkt: Any) -> frozenset[tuple[float, float]] | None:
    """Order- and closure-independent vertex set, so canonicalisation cannot cause a mismatch."""
    if not isinstance(wkt, str):
        return None
    numbers = [float(token) for token in _NUMBER_RE.findall(wkt)]
    if len(numbers) < 6 or len(numbers) % 2:
        return None
    return frozenset((round(numbers[i], 9), round(numbers[i + 1], 9)) for i in range(0, len(numbers), 2))


_PRODUCT_KEYS = (
    "id", "product_type", "processing_type", "reference_date", "secondary_date", "maturity", "granule_name",
    "status", "valid_pixels", "metric_name", "metric_value", "export_count", "grid_pixels_inside_aoi",
    "boundary_pair_included_by_secondary_date_policy", "reference_date_outside_window",
)


def _compact_product(row: Mapping[str, Any]) -> dict[str, Any]:
    product: dict[str, Any] = {}
    for key in _PRODUCT_KEYS:
        value = row.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            product[key] = value
    errors = row.get("errors")
    if isinstance(errors, list) and errors:
        product["error"] = str(errors[0])[:200]
    return product


def build_progress_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    baseline = manifest.get("baseline_product_counts")
    products = manifest.get("products")
    if not isinstance(products, list) or not _valid_counts(baseline):
        raise ResultContractError("The AOI discovery manifest does not contain product counts.")
    selected = {
        family: sum(isinstance(row, dict) and row.get("product_type") == family for row in products)
        for family in ("GUNW", "GOFF")
    }
    return {
        "schema_version": 2, "source": RESULT_SOURCE,
        "catalog_granule_counts": {family: baseline[family] for family in ("GUNW", "GOFF")},
        "selected_granule_counts": selected,
    }


def build_result_summary(report: Mapping[str, Any], *, request: Mapping[str, Any], run: RunInfo, artifact: ArtifactInfo,
                         repository: str, catalog_counts: Any = None) -> dict[str, Any]:
    """Validate the upstream dashboard report and shape it for the website.

    ``request`` carries the submitted ``aoi_wkt``, ``start_date`` and ``end_date``; the
    result is only accepted if it describes that same AOI and date window.
    """
    summary, processing, products = report.get("summary"), report.get("processing_report"), report.get("products")
    if not isinstance(summary, dict) or not isinstance(processing, dict) or not isinstance(products, list):
        raise ResultContractError("The dashboard report does not have the expected sections.")
    expected, validated = summary.get("products_expected"), summary.get("products_validated")
    status = summary.get("status")
    if status != "PASS" or processing.get("status") != "PASS":
        done = f"{validated}/{expected} products validated" if isinstance(validated, int) and isinstance(expected, int) else "products not fully validated"
        raise ResultContractError(
            f"The dashboard finished with status {status if isinstance(status, str) else 'unknown'} ({done}).",
            code="RESULT_NOT_COMPLETE",
        )
    if (isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0 or len(products) != expected
            or isinstance(validated, bool) or not isinstance(validated, int)):
        raise ResultContractError("The dashboard report product counts are inconsistent.")
    digest = summary.get("aoi_wkt_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or processing.get("aoi_wkt_sha256") != digest:
        raise ResultContractError("The dashboard report provenance is inconsistent.")
    submitted = _vertices(request.get("aoi_wkt"))
    if submitted is None or _vertices(summary.get("aoi_wkt")) != submitted:
        raise ResultContractError("The workflow result does not describe the submitted AOI.", code="RESULT_REQUEST_MISMATCH")
    if summary.get("date_start") != str(request.get("start_date")) or summary.get("date_end") != str(request.get("end_date")):
        raise ResultContractError("The workflow result does not describe the submitted date window.", code="RESULT_REQUEST_MISMATCH")

    selected = {
        family: sum(isinstance(row, dict) and row.get("product_type") == family for row in products)
        for family in ("GUNW", "GOFF")
    }
    diagnostics = summary.get("science_diagnostics")
    if not isinstance(diagnostics, dict) or len(json.dumps(diagnostics, default=str)) > MAX_DIAGNOSTICS_JSON_BYTES:
        diagnostics = {}
    limit = summary.get("science_claim_limit")
    return {
        "schema_version": 2,
        "source": RESULT_SOURCE,
        "scientific_status": "PASS",
        "validation": {"status": "PASS", "scope": SCIENTIFIC_SCOPE},
        "github_artifact": {
            "id": artifact.id, "name": artifact.name, "size_in_bytes": artifact.size_in_bytes,
            "expires_at": artifact.expires_at, "repository": repository,
            "run_id": run.run_id, "run_attempt": run.run_attempt,
        },
        "products": [_compact_product(row) for row in products[:MAX_SUMMARY_PRODUCTS] if isinstance(row, dict)],
        "catalog_granule_counts": dict(catalog_counts) if _valid_counts(catalog_counts) else selected,
        "selected_granule_counts": selected,
        "progress": {"completed": validated, "total": expected, "failed": expected - validated},
        "diagnostics": diagnostics,
        "warnings": [
            "Processing and structural validation do not independently validate geophysical accuracy.",
            "Pairwise GUNW/GOFF metrics are not a common-reference displacement time series.",
        ],
        "analysis_type": "upstream_streaming_batch_dynamic_aoi_processing",
        "scientific_limit": limit if isinstance(limit, str) and limit else DEFAULT_SCIENTIFIC_LIMIT,
        "run": {
            "date_filter_mode": summary.get("date_filter_mode"), "run_profile": summary.get("run_profile"),
            "retention_profile": summary.get("retention_profile"),
            "products_with_science_pixels": summary.get("products_with_science_pixels"),
        },
        "provenance": {
            "pipeline_version": "sarguardian-aoi-wkt-dashboard-external-v1",
            "workflow_repository": repository,
            "science_commit": summary.get("science_commit"),
            "orchestration_commit": summary.get("orchestration_sha"),
            "orchestration_branch": summary.get("orchestration_branch"),
            "aoi_sha256": digest,
            "github_run_id": run.run_id,
        },
    }


def _read_report(settings: Any, run: RunInfo) -> tuple[dict[str, Any], ArtifactInfo]:
    name = f"{DASHBOARD_ARTIFACT_PREFIX}-{run.run_id}-{run.run_attempt}"
    artifact = find_run_artifact(settings, run.run_id, name)
    if artifact is None:
        raise ArtifactUnavailableError("The workflow finished but did not publish its dashboard artifact.")
    if artifact.expired:
        raise ArtifactUnavailableError("The result artifact has expired on GitHub.")
    members = read_zip_members(artifact_download_url(settings, artifact.id), {REPORT_MEMBER: MAX_JSON_MEMBER_BYTES})
    return _json_object(members[REPORT_MEMBER], REPORT_MEMBER), artifact


def fetch_result_summary(settings: Any, record: Mapping[str, Any], run: RunInfo) -> dict[str, Any]:
    report, artifact = _read_report(settings, run)
    prior = record.get("result_summary")
    return build_result_summary(
        report, request=record, run=run, artifact=artifact, repository=_repository(settings),
        catalog_counts=prior.get("catalog_granule_counts") if isinstance(prior, dict) else None,
    )


def result_download_link(settings: Any, summary: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(signed_url, filename)`` for a succeeded job's stored result package."""
    package = summary.get("github_artifact") if isinstance(summary, Mapping) else None
    if not isinstance(package, dict):
        raise ArtifactUnavailableError("This job has no downloadable GitHub result package.")
    artifact_id, name, run_id = package.get("id"), package.get("name"), package.get("run_id")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or not isinstance(name, str) or not isinstance(run_id, str):
        raise ArtifactUnavailableError("The stored result package description is invalid.")
    try:
        _status, payload = _github_json(settings, "GET", f"actions/artifacts/{artifact_id}")
    except GitHubApiError as exc:
        if exc.status in {404, 410}:
            raise ArtifactUnavailableError("The result artifact is no longer available on GitHub.") from None
        raise
    artifact = _artifact_from(payload)
    if artifact is None or artifact.name != name or artifact.run_id != run_id:
        raise ArtifactUnavailableError("The stored result package no longer matches its GitHub artifact.")
    if artifact.expired:
        raise ArtifactUnavailableError("The result artifact has expired on GitHub.")
    return artifact_download_url(settings, artifact.id), f"{name}.zip"


# --------------------------------------------------------------------------- job refresh


def _failed(code: str, message: str, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {"state": "failed", "stage": "failed", "error_code": code, "error_message": message[:500]}
    if summary is not None:
        values["result_summary"] = summary
    return values


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _describe_dashboard(settings: Any, run: RunInfo) -> str:
    """Best effort: say how far a failed run got.  Never raises."""
    try:
        report, _artifact = _read_report(settings, run)
        summary = report.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("status"), str):
            return (f" Dashboard status {summary['status']}: {summary.get('products_validated')}/"
                    f"{summary.get('products_expected')} products validated.")
    except Exception:  # noqa: BLE001
        logger.debug("Could not describe the failed dashboard", exc_info=True)
    return ""


def _terminal_values(settings: Any, record: Mapping[str, Any], run: RunInfo) -> dict[str, Any] | None:
    """Final database values for a completed run, or ``None`` to retry later (transient error)."""
    if run.conclusion == "success":
        try:
            summary = fetch_result_summary(settings, record, run)
        except GitHubApiError as exc:
            if exc.transient:
                return None
            return _failed("RESULT_ARTIFACT_UNAVAILABLE", str(exc))
        except ResultContractError as exc:
            return _failed(exc.code, str(exc), exc.progress)
        except ExternalWorkflowError as exc:
            return _failed(exc.code, str(exc))
        return {"state": "succeeded", "stage": "completed", "result_summary": summary, "error_code": None, "error_message": None}
    if run.conclusion == "cancelled":
        return {
            "state": "cancelled", "stage": "cancelled_on_github", "error_code": "CANCELLED_ON_GITHUB",
            "error_message": "The GitHub Actions run was cancelled (by an operator, or superseded by a newer queued run).",
        }
    message = f"The upstream workflow finished with conclusion '{run.conclusion or 'unknown'}'."
    return _failed("GITHUB_ACTIONS_WORKER_FAILED", message + _describe_dashboard(settings, run))


_progress_failures: dict[str, int] = {}


def _discover_progress(settings: Any, record: Mapping[str, Any], run: RunInfo) -> dict[str, Any] | None:
    prior = record.get("result_summary")
    if isinstance(prior, dict) and "catalog_granule_counts" in prior:
        return None
    if _progress_failures.get(run.run_id, 0) >= 3:
        return None
    try:
        artifact = find_run_artifact(settings, run.run_id, MANIFEST_ARTIFACT_NAME)
        if artifact is None or artifact.expired:
            return None
        members = read_zip_members(artifact_download_url(settings, artifact.id), {MANIFEST_MEMBER: MAX_JSON_MEMBER_BYTES})
        return build_progress_summary(_json_object(members[MANIFEST_MEMBER], MANIFEST_MEMBER))
    except GitHubApiError:
        return None
    except ExternalWorkflowError:
        _progress_failures[run.run_id] = _progress_failures.get(run.run_id, 0) + 1
        return None


def plan_refresh(settings: Any, record: Mapping[str, Any], *, now: datetime | None = None) -> list[RefreshStep]:
    """Compare a stored job with its GitHub run and return the database updates to apply, in order.

    The state machine in the database only allows ``dispatching -> running ->
    succeeded|failed|cancelled``, so a run that is already finished when first observed
    yields two steps.  A transient GitHub error yields no steps and never fails the job.
    """
    state = str(record.get("state"))
    if state not in {"dispatching", "running"}:
        return []
    now = now or datetime.now(UTC)
    timeout = int(getattr(settings, "displacement_dispatch_timeout_seconds", DEFAULT_START_TIMEOUT_SECONDS) or DEFAULT_START_TIMEOUT_SECONDS)
    created = _parse_time(record.get("created_at"))
    age = (now - created).total_seconds() if created else 0.0
    run_id = record.get("github_run_id")
    if not run_id:
        if age > timeout:
            return [RefreshStep(state, _failed("EXTERNAL_RUN_UNBOUND", "No GitHub Actions run was recorded for this request."))]
        return []
    try:
        run = get_run(settings, str(run_id))
    except GitHubApiError as exc:
        if exc.status == 404:
            return [RefreshStep(state, _failed("GITHUB_RUN_NOT_FOUND", "The GitHub Actions run for this request could not be found."))]
        if not exc.transient:
            logger.warning("External workflow status check was refused (%s); check GITHUB_ACTIONS_TOKEN access.", exc.status)
        return []
    except ExternalWorkflowError:
        logger.warning("External workflow status check is not configured correctly.", exc_info=True)
        return []

    link = {"github_run_url": run.html_url} if run.html_url and run.html_url != record.get("github_run_url") else {}
    steps: list[RefreshStep] = []
    current = state
    if run.status != "completed":
        if run.status == "in_progress":
            if current == "dispatching":
                steps.append(RefreshStep("dispatching", {"state": "running", "stage": "github_running", **link}))
                current = "running"
            progress = _discover_progress(settings, record, run)
            if progress is not None:
                steps.append(RefreshStep(current, {"stage": "products_discovered", "result_summary": progress}))
        elif age > timeout:
            try:
                cancel_run(settings, run.run_id)
            except ExternalWorkflowError:
                logger.warning("Could not cancel the GitHub run that never started.", exc_info=True)
            steps.append(RefreshStep(current, _failed("WORKFLOW_START_TIMEOUT", "The workflow did not start before the configured timeout.")))
        elif record.get("stage") != "github_queued":
            steps.append(RefreshStep(current, {"stage": "github_queued", **link}))
        return steps

    if current == "dispatching":
        steps.append(RefreshStep("dispatching", {"state": "running", "stage": "github_finishing", **link}))
        current = "running"
    terminal = _terminal_values(settings, record, run)
    if terminal is not None:
        steps.append(RefreshStep(current, terminal))
    return steps


_poll_lock = threading.Lock()
_last_poll: dict[str, float] = {}


def should_poll(key: str, *, min_interval: float = MIN_POLL_INTERVAL_SECONDS, now: float | None = None) -> bool:
    """Best-effort per-process throttle so a 2-second UI poll cannot exhaust GitHub's rate limit."""
    current = time.monotonic() if now is None else now
    with _poll_lock:
        previous = _last_poll.get(key)
        if previous is not None and current - previous < min_interval:
            return False
        _last_poll[key] = current
        if len(_last_poll) > 2048:
            for stale in sorted(_last_poll, key=_last_poll.__getitem__)[:1024]:
                del _last_poll[stale]
        return True
