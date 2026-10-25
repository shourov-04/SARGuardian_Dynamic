#!/usr/bin/env python3
"""Download one exact NISAR granule through the official direct HTTPS path.

The pinned science module supplies the canonical AOI setup and authenticated
ASF session. The direct HTTPS object layout is documented by the NISAR guide,
so the known name/maturity from the committed baseline avoids catalog search.
This wrapper adds single-product, disk-space, filename, HDF5, checksum and
provenance safeguards. It never deletes the source file.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def normal_name(value: str) -> str:
    """Normalize a public granule ID or a source filename, preserving the ID."""
    return re.sub(r"\.h5$", "", Path(value.strip()).name, flags=re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_exact_product_with_timeout(
    session: Any,
    url: str,
    out_path: Path,
    expected_bytes: int | None,
    *,
    connect_timeout_seconds: int = 30,
    read_timeout_seconds: int = 120,
    progress_interval_bytes: int = 128 * 1024 * 1024,
) -> int:
    """Stream one authenticated ASF object with bounded idle waits and progress logs."""
    if not trusted_asf_url(url):
        raise ValueError("DIRECT_PRODUCT_URL_NOT_TRUSTED")
    response = session.get(
        url,
        allow_redirects=True,
        stream=True,
        timeout=(connect_timeout_seconds, read_timeout_seconds),
    )
    try:
        response.raise_for_status()
        header_length = response.headers.get("Content-Length")
        try:
            header_bytes = int(header_length) if header_length else None
        except (TypeError, ValueError, OverflowError):
            header_bytes = None
        if (
            expected_bytes and header_bytes
            and header_bytes != expected_bytes
        ):
            raise RuntimeError(
                f"DOWNLOAD_CONTENT_LENGTH_MISMATCH expected={expected_bytes} "
                f"response_header={header_bytes}"
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        next_progress = progress_interval_bytes
        try:
            with out_path.open("wb") as target:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    target.write(chunk)
                    downloaded += len(chunk)
                    if downloaded >= next_progress:
                        print(
                            f"DOWNLOAD_PROGRESS_BYTES={downloaded} "
                            f"EXPECTED_BYTES={expected_bytes or header_bytes or 'unknown'}",
                            flush=True,
                        )
                        while next_progress <= downloaded:
                            next_progress += progress_interval_bytes
        except requests.exceptions.RequestException:
            out_path.unlink(missing_ok=True)
            raise

        if downloaded <= 0:
            out_path.unlink(missing_ok=True)
            raise RuntimeError("DOWNLOAD_EMPTY_RESPONSE_BODY")
        expected = expected_bytes or header_bytes
        if expected and downloaded != expected:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"DOWNLOAD_CONTENT_LENGTH_MISMATCH expected={expected} actual={downloaded}"
            )
        print(f"DOWNLOAD_COMPLETE_BYTES={downloaded}", flush=True)
        return downloaded
    finally:
        response.close()


def load_source_acquisition(science_root: Path, aoi: str) -> tuple[Any, str]:
    """Load the exact acquisition module shipped by the pinned science repo."""
    source_file = (science_root / "src" / "nisar_acquisition.py").resolve()
    if not source_file.is_file():
        raise FileNotFoundError("PINNED_SCIENCE_ACQUISITION_MODULE_MISSING")
    source_dir = str(source_file.parent)
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    spec = importlib.util.spec_from_file_location(
        "sarguardian_pinned_nisar_acquisition", source_file
    )
    if spec is None or spec.loader is None:
        raise ImportError("PINNED_SCIENCE_ACQUISITION_MODULE_COULD_NOT_BE_LOADED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This imports AOIs from the same gunw_reader used by all science scripts.
    module.set_aoi(aoi)
    return module, sha256_file(source_file)


def trusted_asf_url(value: Any) -> bool:
    """Validate an HTTPS URL against the trusted ASF data hosts."""
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "asf.alaska.edu"
        or host.endswith(".asf.alaska.edu")
        or host.endswith(".asf.earthdatacloud.nasa.gov")
    )


def safe_remote_error(exc: BaseException) -> str:
    """Emit short network diagnostics without leaking URLs or credentials."""
    code = getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    if code is None:
        code = getattr(response, "status_code", None)
    detail = re.sub(r"https?://[^\s\"'<>]+", "[URL]", str(exc))
    detail = re.sub(
        r"(?i)\b(password|passwd|token|access[_-]?token|refresh[_-]?token|"
        r"authorization|client[_-]?secret|secret)\b\s*[:=]\s*[^,;\s<>]+",
        r"\1=[REDACTED]",
        detail,
    )
    detail = re.sub(r"\s+", " ", detail).strip()[:220]
    return f"status={code or 'unknown'} detail={detail or type(exc).__name__}"


def is_retryable_remote_failure(exc: BaseException) -> bool:
    """Classify only transient HTTP/network failures for automated whole-step retry."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return int(status) in {408, 425, 429, 500, 502, 503, 504}
    if re.search(r"\b(?:HTTP_STATUS_)(?:408|425|429|500|502|503|504)\b", str(exc)):
        return True
    if "DOWNLOAD_CONTENT_LENGTH_MISMATCH" in str(exc):
        return True
    return isinstance(exc, requests.exceptions.RequestException)


def direct_product_url(wanted: str, product_type: str, maturity: str) -> str:
    """Build the documented NISAR Earthdata HTTPS path for a production granule."""
    collection = f"NISAR_L2_{product_type}_{maturity.upper()}_V1"
    return (
        "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/"
        f"{collection}/{wanted}/{wanted}.h5"
    )


def is_urgent_response_product(wanted: str) -> bool:
    """UR products live in a separate bucket; do not use the production URL layout."""
    return bool(re.search(r"_UR_(?:GUNW|GOFF)_", wanted))


def _sleep_before_retry(stage: str, attempt: int, detail: str) -> None:
    delay = min(2 ** attempt, 30)
    print(
        f"NETWORK_RETRY stage={stage} attempt={attempt} "
        f"sleep_seconds={delay} {safe_remote_error(RuntimeError(detail))}",
        file=sys.stderr,
    )
    time.sleep(delay)


def build_authenticated_session_with_retry(
    acquisition: Any, attempts: int = 2, *, force_credentials: bool = False
) -> Any:
    """Build a bearer-authenticated ASF session without the unrelated cookie flow."""
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if token and not force_credentials:
        import asf_search

        print("EARTHDATA_AUTH_MODE=pre_generated_user_token", file=sys.stderr)
        # ASFSession applies the token as a Bearer credential. This avoids calling
        # /api/users/find_or_create_token for each newly constructed session.
        return asf_search.ASFSession().auth_with_token(token)

    username = os.environ.get("EARTHDATA_USERNAME", "").strip()
    password = os.environ.get("EARTHDATA_PASSWORD", "")
    if not username or not password:
        # Compatibility path for callers that provide a pre-authenticated
        # pinned acquisition module (including offline validation tests).
        print("EARTHDATA_AUTH_MODE=pinned_acquisition_session", file=sys.stderr)
        for attempt in range(1, attempts + 1):
            source_request = requests.sessions.Session.request

            def bounded_request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", (10, 20))
                return source_request(session, method, url, **kwargs)

            try:
                requests.sessions.Session.request = bounded_request
                return acquisition.build_session()
            except requests.exceptions.RequestException as exc:
                if attempt >= attempts or not is_retryable_remote_failure(exc):
                    raise
                _sleep_before_retry("earthdata_authentication", attempt, str(exc))
            finally:
                requests.sessions.Session.request = source_request
        raise RuntimeError("EARTHDATA_AUTHENTICATION_RETRIES_EXHAUSTED")

    import asf_search
    from requests.auth import HTTPBasicAuth

    print("EARTHDATA_AUTH_MODE=username_password_bearer_token_exchange", file=sys.stderr)
    for attempt in range(1, attempts + 1):
        session = asf_search.ASFSession()
        response = None
        try:
            # auth_with_creds() also requests an asf-urs cookie. That cookie is
            # required for some ASF products, but not for these NISAR L2 HTTPS
            # objects. The cookie exchange can return 401 even after EDL issued
            # a valid user token, so perform the documented token request and
            # install the resulting Bearer credential directly.
            response = session.post(
                f"https://{session.edl_host}/api/users/find_or_create_token",
                auth=HTTPBasicAuth(username, password),
                timeout=(10, 20),
            )
            response.raise_for_status()
            payload = response.json()
            access_token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise RuntimeError("EARTHDATA_TOKEN_RESPONSE_INVALID")
            session._update_edl_token(access_token)
            return session
        except (requests.exceptions.RequestException, ValueError) as exc:
            if attempt >= attempts or not is_retryable_remote_failure(exc):
                raise
            _sleep_before_retry("earthdata_authentication", attempt, str(exc))
        finally:
            if response is not None:
                response.close()
    raise RuntimeError("EARTHDATA_AUTHENTICATION_RETRIES_EXHAUSTED")


def probe_url_content_length(session: Any, url: str, attempts: int = 2) -> int | None:
    """Probe an exact HTTPS object with bounded retries for transient network/HTTP errors."""
    if not trusted_asf_url(url):
        raise ValueError("DIRECT_PRODUCT_URL_NOT_TRUSTED")

    for attempt in range(1, attempts + 1):
        response = None
        try:
            response = session.head(url, allow_redirects=True, timeout=(10, 30))
            if response.status_code in {405, 501}:
                response.close()
                response = session.get(
                    url,
                    headers={"Range": "bytes=0-0"},
                    allow_redirects=True,
                    stream=True,
                    timeout=(10, 30),
                )
            status = int(response.status_code)
            if status in {429, 500, 502, 503, 504} and attempt < attempts:
                reason = response.reason or f"HTTP_{status}"
                response.close()
                response = None
                _sleep_before_retry("https_product_probe", attempt, reason)
                continue
            if status >= 400:
                raise RuntimeError(
                    f"DIRECT_PRODUCT_HTTP_STATUS_{status} {response.reason or ''}".strip()
                )
            content_range = response.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                remote_bytes = int(match.group(1))
            else:
                raw_size = response.headers.get("Content-Length")
                try:
                    remote_bytes = int(raw_size) if raw_size else 0
                except (TypeError, ValueError, OverflowError):
                    remote_bytes = 0
            return remote_bytes if remote_bytes > 0 else None
        except requests.exceptions.RequestException as exc:
            if attempt >= attempts:
                raise
            _sleep_before_retry("https_product_probe", attempt, str(exc))
        finally:
            if response is not None:
                response.close()
    raise RuntimeError("DIRECT_PRODUCT_PROBE_RETRIES_EXHAUSTED")


def probe_direct_product(
    acquisition: Any, wanted: str, product_type: str, maturity: str, *, force_credentials: bool = False
) -> tuple[dict[str, Any], Any, int | None]:
    """Verify the exact production HTTPS object and read its size."""
    url = direct_product_url(wanted, product_type, maturity)
    if not trusted_asf_url(url):
        raise ValueError("DIRECT_PRODUCT_URL_NOT_TRUSTED")

    session = build_authenticated_session_with_retry(acquisition, force_credentials=force_credentials)
    remote_bytes = probe_url_content_length(session, url)
    record = {
        "granuleName": wanted,
        "productType": product_type,
        "maturity": maturity.upper(),
        "downloadUrl": url,
        "sizeBytes": remote_bytes,
        "_sarguardian_discovery_backend": "direct_https_known_granule",
    }
    return record, session, remote_bytes


def _catalog_record_for_exact_granule(
    wanted: str, product_type: str, maturity: str, attempts: int = 2
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the exact granule from ASF catalog metadata and its published access URL.

    This fallback is required for UR products (separate storage bucket) and for
    production URLs that no longer match the published object location.
    """
    import asf_search

    results = None
    for attempt in range(1, attempts + 1):
        try:
            results = asf_search.search(dataset="NISAR", granule_list=[wanted])
            break
        except requests.exceptions.RequestException as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    "ASF_CATALOG_SEARCH_FAILED " + safe_remote_error(exc)
                ) from None
            _sleep_before_retry("asf_catalog_search", attempt, str(exc))
    if results is None:
        raise RuntimeError("ASF_CATALOG_SEARCH_FAILED_NO_RESULT")

    def value_matches(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        text = value.strip()
        parsed = urlparse(text)
        if parsed.scheme:
            text = Path(parsed.path).name
        return normal_name(text) == wanted

    exact_match = None
    exact_url = None
    for product in results:
        props = getattr(product, "properties", {}) or {}
        if not isinstance(props, dict):
            props = {}
        urls: list[str] = []
        try:
            urls = [u for u in product.get_urls() if isinstance(u, str)]
        except Exception:
            urls = []
        props_url = props.get("url")
        if isinstance(props_url, str):
            urls.append(props_url)
        urls = list(dict.fromkeys(urls))
        exact_urls = [
            u for u in urls
            if value_matches(u) and trusted_asf_url(u)
        ]
        identity_values = [
            props.get(key)
            for key in (
                "fileName", "granuleName", "sceneName", "productName",
                "producerGranuleId", "producerGranuleID", "granule_ur",
                "fileID", "name",
            )
        ]
        identity_ok = any(value_matches(v) for v in identity_values)
        if not identity_ok and not exact_urls:
            continue
        if not exact_urls:
            continue
        exact_match = product
        exact_url = exact_urls[0]
        break

    if exact_match is None or exact_url is None:
        if is_urgent_response_product(wanted):
            raise RuntimeError(
                "UR_GRANULE_NOT_FOUND_IN_ASF_CATALOG; urgent-response products use "
                "a separate bucket and are normally retained for 30 days; an archived "
                "source copy is required if ASF has expired this granule"
            )
        raise RuntimeError("ASF_CATALOG_EXACT_GRANULE_NOT_FOUND")

    props = getattr(exact_match, "properties", {}) or {}
    if not isinstance(props, dict):
        props = {}
    discovered_maturity = str(
        props.get("dataMaturity") or props.get("maturity") or maturity
    ).upper()
    record = {
        "granuleName": wanted,
        "productType": product_type,
        "maturity": discovered_maturity,
        "downloadUrl": exact_url,
        "sizeBytes": props.get("bytes") or props.get("sizeBytes"),
        "_sarguardian_discovery_backend": "asf_catalog_exact_granule",
    }
    return record, props


def probe_catalog_product(
    acquisition: Any, wanted: str, product_type: str, maturity: str, *, force_credentials: bool = False
) -> tuple[dict[str, Any], Any, int | None]:
    """Resolve an exact ASF catalog URL (including UR bucket routing), then verify it."""
    record, _props = _catalog_record_for_exact_granule(
        wanted, product_type, maturity
    )
    session = build_authenticated_session_with_retry(acquisition, force_credentials=force_credentials)
    try:
        remote_bytes = probe_url_content_length(session, record["downloadUrl"])
    except RuntimeError as exc:
        if is_urgent_response_product(wanted) and "DIRECT_PRODUCT_HTTP_STATUS_404" in str(exc):
            raise RuntimeError(
                "UR_GRANULE_OBJECT_EXPIRED_OR_NOT_AVAILABLE_IN_UR_BUCKET; "
                "urgent-response products are normally retained for 30 days; "
                "restore the exact raw granule from an independent archive if available"
            ) from None
        raise
    record["sizeBytes"] = remote_bytes or record.get("sizeBytes")
    return record, session, remote_bytes


def probe_known_product(
    acquisition: Any, wanted: str, product_type: str, maturity: str, *, force_credentials: bool = False
) -> tuple[dict[str, Any], Any, int | None]:
    """Use exact product routing, falling back from rejected user tokens when credentials exist."""
    try:
        if is_urgent_response_product(wanted):
            print("DISCOVERY_FALLBACK=ASF_CATALOG_URGENT_RESPONSE", file=sys.stderr)
            return probe_catalog_product(
                acquisition, wanted, product_type, maturity, force_credentials=force_credentials
            )
        try:
            return probe_direct_product(
                acquisition, wanted, product_type, maturity, force_credentials=force_credentials
            )
        except RuntimeError as exc:
            if "DIRECT_PRODUCT_HTTP_STATUS_404" not in str(exc):
                raise
            print(
                "DIRECT_PRODUCT_HTTP_404=retrying exact ASF catalog URL resolution",
                file=sys.stderr,
            )
            return probe_catalog_product(
                acquisition, wanted, product_type, maturity, force_credentials=force_credentials
            )
    except RuntimeError as exc:
        token = os.environ.get("EARTHDATA_TOKEN", "").strip()
        has_credentials = bool(os.environ.get("EARTHDATA_USERNAME")) and bool(os.environ.get("EARTHDATA_PASSWORD"))
        auth_rejected = any(
            f"DIRECT_PRODUCT_HTTP_STATUS_{status}" in str(exc) for status in (401, 403)
        )
        if token and has_credentials and not force_credentials and auth_rejected:
            print(
                "EARTHDATA_USER_TOKEN_REJECTED=retrying_once_with_username_password_session",
                file=sys.stderr,
            )
            return probe_known_product(
                acquisition, wanted, product_type, maturity, force_credentials=True
            )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-type", required=True, choices=["GUNW", "GOFF"])
    parser.add_argument("--maturity", required=True, choices=["PROVISIONAL", "BETA"])
    parser.add_argument("--granule-name", required=True, help="Exact public NISAR granule identifier; no wildcard")
    parser.add_argument("--science-root", required=True, type=Path, help="Checkout of the pinned SARGuardian main source")
    parser.add_argument("--aoi", default="source", help="AOI name defined in the pinned gunw_reader.AOIS")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--expected-source-bytes", type=int, default=0)
    parser.add_argument("--disk-safety-factor", type=float, default=2.2)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Resolve the exact authenticated product URL and verify availability without downloading or writing source data",
    )
    args = parser.parse_args()

    supplied_name = args.granule_name.strip()
    wanted = normal_name(supplied_name)
    if Path(supplied_name).name != supplied_name or not re.fullmatch(
        r"NISAR_L2_(?:(?:PR|UR|OD)_)?(GUNW|GOFF)_[A-Za-z0-9_.-]{10,250}", wanted
    ):
        print("ERROR=EXACT_PUBLIC_GRANULE_NAME_REQUIRED", file=sys.stderr)
        return 2
    if f"_{args.product_type}_" not in wanted:
        print("ERROR=GRANULE_NAME_PRODUCT_TYPE_MISMATCH", file=sys.stderr)
        return 2
    has_user_token = bool(os.environ.get("EARTHDATA_TOKEN", "").strip())
    has_credentials = bool(os.environ.get("EARTHDATA_USERNAME")) and bool(os.environ.get("EARTHDATA_PASSWORD"))
    if not has_user_token and not has_credentials:
        print("ERROR=EARTHDATA_CREDENTIALS_MISSING_OR_EARTHDATA_TOKEN_MISSING", file=sys.stderr)
        return 2

    try:
        acquisition, acquisition_sha256 = load_source_acquisition(args.science_root, args.aoi)
        record, session, remote_content_length_bytes = probe_known_product(
            acquisition, wanted, args.product_type, args.maturity
        )
    except Exception as exc:
        print(f"ERROR=DIRECT_HTTPS_PRODUCT_PROBE_FAILED TYPE={type(exc).__name__}", file=sys.stderr)
        print("DOWNLOAD_FAILURE_RETRYABLE=" + ("yes" if is_retryable_remote_failure(exc) else "no"), file=sys.stderr)
        print("DIRECT_PRODUCT_DIAGNOSTIC=" + safe_remote_error(exc), file=sys.stderr)
        print("SOURCE_ACQUISITION_MODULE=src/nisar_acquisition.py", file=sys.stderr)
        print(f"AOI={args.aoi}", file=sys.stderr)
        return 2

    if args.probe_only:
        print(f"PRODUCT_TYPE={args.product_type}")
        print(f"MATURITY={args.maturity}")
        print(f"AOI={args.aoi}")
        print(f"DISCOVERY_BACKEND={record['_sarguardian_discovery_backend']}")
        print(f"REMOTE_CONTENT_LENGTH_BYTES={remote_content_length_bytes or 'unknown'}")
        print("PRODUCT_PREFLIGHT_STATUS=PASS")
        return 0

    # The pinned baseline run supplies a conservative family estimate; a real
    # Content-Length from the exact HTTPS object wins when available.
    family_estimate = 2_400_000_000 if args.product_type == "GUNW" else 1_000_000_000
    expected_bytes = max(
        args.expected_source_bytes or 0,
        remote_content_length_bytes or 0,
        family_estimate if remote_content_length_bytes is None and not args.expected_source_bytes else 0,
    )
    if expected_bytes <= 0:
        print("ERROR=SOURCE_SIZE_ESTIMATE_UNAVAILABLE", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(args.out_dir).free
    required_free = int(expected_bytes * args.disk_safety_factor + 512 * 1024 * 1024)
    collection = f"NISAR_L2_{args.product_type}_{args.maturity}_V1"
    print(f"PRODUCT_TYPE={args.product_type}")
    print(f"MATURITY={args.maturity}")
    print(f"COLLECTION={collection}")
    print(f"AOI={args.aoi}")
    print(f"DISCOVERY_BACKEND={record['_sarguardian_discovery_backend']}")
    print(f"REMOTE_CONTENT_LENGTH_BYTES={remote_content_length_bytes or 'unknown'}")
    print(f"EXPECTED_SOURCE_BYTES={expected_bytes}")
    print(f"RUNNER_FREE_BYTES_BEFORE_DOWNLOAD={free_bytes}")
    print(f"REQUIRED_FREE_BYTES_WITH_SAFETY_MARGIN={required_free}")
    if free_bytes < required_free:
        print("ERROR=INSUFFICIENT_RUNNER_DISK_SPACE", file=sys.stderr)
        return 2

    staging_dir = args.out_dir / ".download-staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(mode=0o700)
    try:
        staged_raw = staging_dir / f"{wanted}.h5"
        download_exact_product_with_timeout(
            session,
            record["downloadUrl"],
            staged_raw,
            remote_content_length_bytes,
        )
        files = sorted([*staging_dir.glob("*.h5"), *staging_dir.glob("*.H5")])
        if len(files) != 1:
            raise RuntimeError(f"EXPECTED_ONE_HDF5_AFTER_DOWNLOAD_COUNT_{len(files)}")
        staged_raw = files[0]
        if normal_name(staged_raw.name) != wanted:
            raise RuntimeError("DOWNLOADED_FILENAME_DOES_NOT_MATCH_REQUESTED_GRANULE")
        import h5py
        if not h5py.is_hdf5(staged_raw):
            raise RuntimeError("DOWNLOADED_FILE_IS_NOT_A_VALID_HDF5_CONTAINER")
        actual_size = staged_raw.stat().st_size
        if actual_size <= 0 or actual_size > free_bytes:
            raise RuntimeError("INVALID_DOWNLOADED_SOURCE_SIZE")
        if remote_content_length_bytes is not None and actual_size != remote_content_length_bytes:
            raise RuntimeError("DOWNLOADED_SIZE_DIFFERS_FROM_REMOTE_CONTENT_LENGTH")
        raw = args.out_dir / staged_raw.name
        if raw.exists():
            raise RuntimeError("REFUSING_TO_OVERWRITE_EXISTING_RAW_PRODUCT")
        shutil.move(str(staged_raw), str(raw))
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        print(f"ERROR=SOURCE_ASF_DOWNLOAD_FAILED TYPE={type(exc).__name__}", file=sys.stderr)
        print("DOWNLOAD_FAILURE_RETRYABLE=" + ("yes" if is_retryable_remote_failure(exc) else "no"), file=sys.stderr)
        print("DIRECT_PRODUCT_DIAGNOSTIC=" + safe_remote_error(exc), file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    actual_size = raw.stat().st_size
    free_after = shutil.disk_usage(args.out_dir).free

    receipt = {
        "schema_version": "sarguardian-single-granule-download/2.0",
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "product_type": args.product_type,
        "maturity_requested": args.maturity,
        "aoi": args.aoi,
        "collection_short_name": collection,
        "discovery_backend": record["_sarguardian_discovery_backend"],
        "science_acquisition_sha256": acquisition_sha256,
        "discovery_start": None,
        "discovery_end": None,
        "granule_name": wanted,
        "source_filename": raw.name,
        "source_bytes": actual_size,
        "source_sha256": sha256_file(raw),
        "downloader_script": Path(__file__).name,
        "downloader_script_sha256": sha256_file(Path(__file__).resolve()),
        "asf_search_version": importlib.metadata.version("asf_search"),
        "catalog_size_estimate_bytes": None,
        "remote_content_length_bytes": remote_content_length_bytes,
        "expected_size_bytes": expected_bytes,
        "runner_free_bytes_after_download": free_after,
        "source_deleted": False,
        "signed_download_urls_stored": False,
    }
    receipt_path = args.out_dir / "download-manifest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"RAW_PRODUCT_PATH={raw}")
    print(f"RAW_PRODUCT_BYTES={actual_size}")
    print(f"RAW_PRODUCT_SHA256={receipt['source_sha256']}")
    print(f"DOWNLOAD_MANIFEST_PATH={receipt_path}")
    print("RAW_SOURCE_DELETED=no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
