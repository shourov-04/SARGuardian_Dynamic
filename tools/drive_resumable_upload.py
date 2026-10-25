#!/usr/bin/env python3
"""Upload curated SARGuardian files to Google Drive with resumable streaming.

Never uploads the original raw product. Idempotent by exact filename+parent:
identical size+MD5 reuses the remote file; conflicting content fails closed.
"""
from __future__ import annotations
import argparse, hashlib, json, mimetypes, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse
import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK_BYTES = 8 * 1024 * 1024  # 8 MiB; multiple of 256 KiB.
MAX_RETRY_ATTEMPTS = 5
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
RETRY_BASE_SECONDS = 2
RETRY_MAX_SECONDS = 30

def _retryable_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUSES

def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Bounded exponential backoff; honour a numeric Retry-After when supplied."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 120.0)
        except (TypeError, ValueError, OverflowError):
            pass
    return min(RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)), RETRY_MAX_SECONDS)

def _log_retry(stage: str, attempt: int, delay: float, reason: str) -> None:
    """Keep retry logs useful without printing URLs, credentials, or request bodies."""
    print(
        f"DRIVE_RETRY stage={stage} attempt={attempt}/{MAX_RETRY_ATTEMPTS} "
        f"sleep_seconds={delay:g} reason={reason}",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(delay)

def _transient_drive_error(error: BaseException) -> bool:
    """Classify only explicit network and retryable HTTP failures."""
    message = str(error)
    if "DRIVE_API_NETWORK_ERROR" in message or "DRIVE_TOKEN_REFRESH_NETWORK_ERROR" in message:
        return True
    return any(f"HTTP_{status}" in message for status in RETRYABLE_HTTP_STATUSES)

class DriveError(RuntimeError): pass

def hashes(path: Path) -> tuple[str, str, int]:
    md5, sha, size = hashlib.md5(), hashlib.sha256(), 0
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            md5.update(block); sha.update(block); size += len(block)
    return md5.hexdigest(), sha.hexdigest(), size

def required_env(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value: raise DriveError(f"MISSING_ENV_{name}")
    return value

class Drive:
    def __init__(self):
        try:
            raw = json.loads(required_env('GOOGLE_DRIVE_OAUTH_CLIENT_JSON'))
            client = raw.get('installed') or raw.get('web') or raw
            raw_refresh = required_env('GOOGLE_DRIVE_REFRESH_TOKEN')
            try:
                parsed = json.loads(raw_refresh)
                refresh = parsed.get('refresh_token') if isinstance(parsed, dict) else parsed
            except json.JSONDecodeError:
                refresh = raw_refresh
            self.client_id, self.client_secret, self.refresh = client['client_id'], client['client_secret'], refresh
            if not all(isinstance(v, str) and v for v in (self.client_id, self.client_secret, self.refresh)):
                raise ValueError()
        except Exception:
            raise DriveError('DRIVE_CONFIGURATION_INVALID') from None
        self.s = requests.Session()
        self.token = None
        self._refresh()

    def _refresh(self):
        """Refresh the OAuth access token with bounded retries for transient failures."""
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                r = self.s.post(
                    TOKEN,
                    data={
                        'client_id': self.client_id,
                        'client_secret': self.client_secret,
                        'refresh_token': self.refresh,
                        'grant_type': 'refresh_token',
                    },
                    timeout=(15, 45),
                )
            except requests.RequestException as exc:
                if attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry("token_refresh", attempt, _retry_delay(attempt), type(exc).__name__)
                    continue
                raise DriveError("DRIVE_TOKEN_REFRESH_NETWORK_ERROR attempts_exhausted") from None

            if _retryable_status(r.status_code):
                status = r.status_code
                delay = _retry_delay(attempt, r.headers.get("Retry-After"))
                r.close()
                if attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry("token_refresh", attempt, delay, f"HTTP_{status}")
                    continue
                raise DriveError(f"DRIVE_TOKEN_REFRESH_HTTP_{status}_RETRIES_EXHAUSTED")

            if r.status_code != 200:
                status = r.status_code
                r.close()
                raise DriveError(f"DRIVE_TOKEN_REFRESH_HTTP_{status}")
            try:
                token = r.json().get("access_token")
            except (ValueError, AttributeError):
                token = None
            finally:
                r.close()
            if not token:
                raise DriveError("DRIVE_TOKEN_REFRESH_INVALID")
            self.token = token
            return

    def request(self, method, url, **kw):
        """Retry safe Drive API requests on transient network/HTTP errors.

        POST is not automatically retried because resource-creation requests can
        be non-idempotent. Callers may explicitly set retry_post=True for
        resumable-session creation, where an abandoned session creates no file.
        Folder creation uses its own lookup-before-retry flow.
        """
        headers = dict(kw.pop('headers', {}))
        timeout = kw.pop('timeout', (20, 120))
        retry_post = bool(kw.pop("retry_post", False))
        can_retry = method.upper() in {"GET", "HEAD", "PUT", "DELETE"} or retry_post
        refreshed = False
        attempt = 1

        while True:
            request_headers = dict(headers)
            request_headers['Authorization'] = f'Bearer {self.token}'
            try:
                r = self.s.request(
                    method, url, headers=request_headers, timeout=timeout, **kw
                )
            except requests.RequestException as exc:
                if can_retry and attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry(f"api_{method.lower()}", attempt, _retry_delay(attempt), type(exc).__name__)
                    attempt += 1
                    continue
                raise DriveError("DRIVE_API_NETWORK_ERROR") from None

            if r.status_code == 401 and not refreshed:
                r.close()
                self._refresh()
                refreshed = True
                continue

            if _retryable_status(r.status_code) and can_retry:
                status = r.status_code
                delay = _retry_delay(attempt, r.headers.get("Retry-After"))
                r.close()
                if attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry(f"api_{method.lower()}", attempt, delay, f"HTTP_{status}")
                    attempt += 1
                    continue
                raise DriveError(f"DRIVE_API_HTTP_{status}_RETRIES_EXHAUSTED")

            if r.status_code >= 400:
                status = r.status_code
                r.close()
                raise DriveError(f"DRIVE_API_HTTP_{status}")
            return r

    @staticmethod
    def _q(s: str) -> str: return s.replace('\\', '\\\\').replace("'", "\\'")

    def list_named_children(self, parent, name):
        q = f"'{self._q(parent)}' in parents and name = '{self._q(name)}' and trashed = false"
        data = self.request('GET', f'{API}/files', params={'q': q, 'spaces': 'drive', 'pageSize': 100,
            'fields': 'files(id,name,mimeType,size,md5Checksum,parents,webViewLink)'}).json()
        return data.get('files', [])

    def ensure_folder(self, parent, name):
        """Create a folder idempotently, resolving ambiguous network failures by lookup."""
        q = f"'{self._q(parent)}' in parents and name = '{self._q(name)}' and trashed = false"
        data = {'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent]}

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            rows = self.list_named_children(parent, name)
            folders = [x for x in rows if x.get('mimeType') == FOLDER_MIME]
            if any(x.get('mimeType') != FOLDER_MIME for x in rows):
                raise DriveError('DRIVE_FOLDER_NAME_CONFLICT')
            if len(folders) > 1:
                raise DriveError('DRIVE_DUPLICATE_FOLDER_CONFLICT')
            if folders:
                return folders[0]['id']

            try:
                out = self.request(
                    'POST', f'{API}/files',
                    params={'fields': 'id,name,mimeType,parents'},
                    json=data,
                ).json()
            except DriveError as exc:
                if not _transient_drive_error(exc) or attempt >= MAX_RETRY_ATTEMPTS:
                    raise
                # The server may have created the folder before the response was
                # lost. The next iteration re-queries by exact parent/name before
                # issuing another POST, preventing duplicate folders.
                delay = _retry_delay(attempt)
                _log_retry("create_folder", attempt, delay, str(exc).split()[0])
                continue

            if not out.get('id') or out.get('mimeType') != FOLDER_MIME:
                raise DriveError('DRIVE_FOLDER_CREATE_INVALID')
            return out['id']

        raise DriveError("DRIVE_FOLDER_CREATE_RETRIES_EXHAUSTED")

    def quota(self):
        doc = self.request('GET', f'{API}/about', params={'fields': 'storageQuota(limit,usage,usageInDrive)'}).json()
        q = doc.get('storageQuota')
        if not isinstance(q, dict) or not q.get('limit') or q.get('usage') is None:
            raise DriveError('DRIVE_QUOTA_UNKNOWN_FAIL_CLOSED')
        return int(q['limit']), int(q['usage'])

    def _query_resumable_status(self, session_url: str, size: int) -> dict:
        """Ask Google which bytes it has accepted after an ambiguous chunk failure."""
        headers = {
            'Content-Length': '0',
            'Content-Range': f'bytes */{size}',
        }
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                response = self.s.put(
                    session_url, data=b'', headers=headers, timeout=(20, 60)
                )
            except requests.RequestException as exc:
                if attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry("resumable_status", attempt, _retry_delay(attempt), type(exc).__name__)
                    continue
                raise DriveError("DRIVE_RESUMABLE_STATUS_NETWORK_ERROR attempts_exhausted") from None

            if response.status_code in (200, 201):
                try:
                    info = response.json()
                except (ValueError, AttributeError):
                    response.close()
                    raise DriveError("DRIVE_RESUMABLE_STATUS_METADATA_INVALID") from None
                response.close()
                return {"complete": True, "info": info, "offset": size}

            if response.status_code == 308:
                value = response.headers.get('Range', '')
                response.close()
                if not value:
                    return {"complete": False, "offset": 0}
                match = re.fullmatch(r"bytes=0-(\d+)", value.strip())
                if not match:
                    raise DriveError("DRIVE_RESUMABLE_RANGE_INVALID")
                offset = int(match.group(1)) + 1
                if not 0 <= offset <= size:
                    raise DriveError("DRIVE_RESUMABLE_RANGE_INVALID")
                return {"complete": False, "offset": offset}

            if _retryable_status(response.status_code):
                status = response.status_code
                delay = _retry_delay(attempt, response.headers.get("Retry-After"))
                response.close()
                if attempt < MAX_RETRY_ATTEMPTS:
                    _log_retry("resumable_status", attempt, delay, f"HTTP_{status}")
                    continue
                raise DriveError(f"DRIVE_RESUMABLE_STATUS_HTTP_{status}_RETRIES_EXHAUSTED")

            status = response.status_code
            response.close()
            if status in (404, 410):
                raise DriveError("DRIVE_RESUMABLE_SESSION_EXPIRED")
            raise DriveError(f"DRIVE_RESUMABLE_STATUS_HTTP_{status}")

        raise DriveError("DRIVE_RESUMABLE_STATUS_RETRIES_EXHAUSTED")

    def upload_resumable(self, parent, path: Path, mime, md5, sha, size):
        existing = self.list_named_children(parent, path.name)
        files = [x for x in existing if x.get('mimeType') != FOLDER_MIME]
        if len(files) > 1:
            raise DriveError('DRIVE_DUPLICATE_FILE_CONFLICT')
        if files:
            item = files[0]
            try:
                remote_size = int(item.get('size', -1))
            except (TypeError, ValueError):
                remote_size = -1
            if remote_size == size and item.get('md5Checksum', '').lower() == md5.lower():
                return {
                    'id': item['id'], 'name': path.name, 'bytes': size, 'md5': md5,
                    'sha256': sha, 'reused': True, 'webViewLink': item.get('webViewLink'),
                }
            raise DriveError(f'DRIVE_EXISTING_FILE_CONFLICT name={path.name}')

        meta = {
            'name': path.name,
            'parents': [parent],
            'description': f'SARGuardian curated archive; sha256={sha}',
        }
        headers = {
            'X-Upload-Content-Type': mime,
            'X-Upload-Content-Length': str(size),
            'Content-Type': 'application/json; charset=UTF-8',
        }
        r = self.request(
            'POST', UPLOAD,
            params={
                'uploadType': 'resumable', 'supportsAllDrives': 'true',
                'fields': 'id,name,size,md5Checksum,parents,webViewLink',
            },
            json=meta,
            headers=headers,
            retry_post=True,
        )
        session_url = r.headers.get('Location')
        r.close()
        parsed_session = urlparse(session_url or '')
        if parsed_session.scheme != 'https' or parsed_session.hostname not in {
            'www.googleapis.com', 'upload.googleapis.com'
        }:
            raise DriveError('DRIVE_RESUMABLE_SESSION_INVALID')

        offset = 0
        info = None
        with path.open('rb') as f:
            while offset < size and info is None:
                f.seek(offset)
                block = f.read(min(CHUNK_BYTES, size - offset))
                if not block:
                    raise DriveError('LOCAL_FILE_READ_SHORT')
                end = offset + len(block) - 1
                chunk_headers = {
                    'Content-Type': mime,
                    'Content-Length': str(len(block)),
                    'Content-Range': f'bytes {offset}-{end}/{size}',
                }
                attempt = 1

                while True:
                    try:
                        part = self.s.put(
                            session_url, data=block, headers=chunk_headers,
                            timeout=(20, 240),
                        )
                    except requests.RequestException as exc:
                        if attempt >= MAX_RETRY_ATTEMPTS:
                            raise DriveError(
                                "DRIVE_CHUNK_UPLOAD_NETWORK_ERROR_RETRIES_EXHAUSTED"
                            ) from None
                        _log_retry("chunk_upload", attempt, _retry_delay(attempt), type(exc).__name__)
                        state = self._query_resumable_status(session_url, size)
                        if state["complete"]:
                            info = state["info"]
                            offset = size
                            break
                        server_offset = state["offset"]
                        if server_offset < offset:
                            raise DriveError("DRIVE_RESUMABLE_OFFSET_MOVED_BACKWARDS")
                        if server_offset > offset:
                            offset = server_offset
                            break
                        attempt += 1
                        continue

                    status = part.status_code
                    if status in (200, 201):
                        try:
                            info = part.json()
                        except (ValueError, AttributeError):
                            part.close()
                            raise DriveError("DRIVE_UPLOAD_FINAL_METADATA_INVALID") from None
                        part.close()
                        offset = size
                        break

                    if status == 308:
                        value = part.headers.get('Range', '')
                        part.close()
                        if not value:
                            next_offset = 0
                        else:
                            match = re.fullmatch(r"bytes=0-(\d+)", value.strip())
                            if not match:
                                raise DriveError('DRIVE_RESUMABLE_RANGE_INVALID')
                            next_offset = int(match.group(1)) + 1
                        if next_offset < offset or next_offset > size:
                            raise DriveError('DRIVE_RESUMABLE_RANGE_INVALID')
                        if next_offset == offset:
                            if attempt >= MAX_RETRY_ATTEMPTS:
                                raise DriveError('DRIVE_RESUMABLE_NO_PROGRESS')
                            delay = _retry_delay(attempt)
                            _log_retry("chunk_no_progress", attempt, delay, "HTTP_308_NO_PROGRESS")
                            attempt += 1
                            continue
                        offset = next_offset
                        break

                    if _retryable_status(status):
                        delay = _retry_delay(attempt, part.headers.get("Retry-After"))
                        part.close()
                        if attempt >= MAX_RETRY_ATTEMPTS:
                            raise DriveError(
                                f"DRIVE_CHUNK_UPLOAD_HTTP_{status}_RETRIES_EXHAUSTED"
                            )
                        _log_retry("chunk_upload", attempt, delay, f"HTTP_{status}")
                        # Never blindly resend an ambiguous chunk: query the
                        # resumable session first and continue at Google's
                        # acknowledged byte offset.
                        state = self._query_resumable_status(session_url, size)
                        if state["complete"]:
                            info = state["info"]
                            offset = size
                            break
                        server_offset = state["offset"]
                        if server_offset < offset:
                            raise DriveError("DRIVE_RESUMABLE_OFFSET_MOVED_BACKWARDS")
                        if server_offset > offset:
                            offset = server_offset
                            break
                        attempt += 1
                        continue

                    part.close()
                    raise DriveError(f'DRIVE_CHUNK_UPLOAD_HTTP_{status}')

                if info is not None:
                    break

        if info is None:
            raise DriveError('DRIVE_UPLOAD_FINISHED_WITHOUT_METADATA')
        try:
            remote_size = int(info.get('size', -1))
        except (TypeError, ValueError):
            remote_size = -1
        if (
            not info.get('id')
            or remote_size != size
            or info.get('md5Checksum', '').lower() != md5.lower()
        ):
            raise DriveError('DRIVE_REMOTE_SIZE_OR_MD5_MISMATCH')
        return {
            'id': info['id'], 'name': path.name, 'bytes': size, 'md5': md5,
            'sha256': sha, 'reused': False, 'webViewLink': info.get('webViewLink'),
        }

REPLACEABLE_RETRY_SIDECAR_NAMES = frozenset({
    # These files describe a processing attempt and may legitimately include a
    # new timestamp or upload receipt IDs when the same product is retried.
    # Source identity must be proven equal before the worker enables this mode.
    "download-manifest.json",
    "preflight-report.json",
    "product-processing.json",
    "drive-retention-receipt.json",
    "local-cleanup-receipt.json",
})


def plan_existing_uploads(drive, folder, records, *, replace_existing_retry_sidecars=False):
    """Preflight existing files without silently replacing scientific outputs."""
    missing = 0
    replacements = []
    for path, md5, _sha, size, _mime in records:
        existing = drive.list_named_children(folder, path.name)
        ordinary = [item for item in existing if item.get("mimeType") != FOLDER_MIME]
        if len(ordinary) > 1:
            raise DriveError("DRIVE_DUPLICATE_FILE_CONFLICT")
        if not ordinary:
            missing += size
            continue

        item = ordinary[0]
        try:
            remote_size = int(item.get("size", -1))
        except (TypeError, ValueError):
            remote_size = -1
        matches = remote_size == size and str(item.get("md5Checksum", "")).lower() == md5.lower()
        if matches:
            continue

        if not replace_existing_retry_sidecars or path.name not in REPLACEABLE_RETRY_SIDECAR_NAMES:
            raise DriveError(f"DRIVE_EXISTING_FILE_CONFLICT name={path.name}")
        file_id = item.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise DriveError(f"DRIVE_EXISTING_RECEIPT_ID_MISSING name={path.name}")
        replacements.append({"id": file_id, "name": path.name, "bytes": size})
        # Conservative quota accounting: require room for the replacement before
        # deleting the stale same-run receipt, even though deletion may free space.
        missing += size
    return missing, replacements


def normalise_run_scoped_receipt_row(row, *, existing_files_are_new):
    """Distinguish same-workflow-run reuse from content retained by a prior run."""
    result = dict(row)
    if existing_files_are_new:
        result["upload_action_reused"] = bool(result.get("reused", False))
        # The folder is scoped to this unique Actions run/attempt. A matching file
        # already present there was created during this same run's earlier try,
        # so retention accounting must count its bytes once for this run.
        result["reused"] = False
    return result


def granule_folder_names(product_type, maturity, granule_name, version_tag=None):
    """Return a deterministic folder path; optional version tags preserve prior runs."""
    if product_type not in {"GUNW", "GOFF"} or maturity not in {"PROVISIONAL", "BETA"}:
        raise DriveError("GRANULE_UPLOAD_REQUIRES_PRODUCT_MATURITY_AND_NAME")
    supplied = str(granule_name or "").strip()
    granule_folder = Path(supplied).name
    if granule_folder.lower().endswith(".h5"):
        granule_folder = granule_folder[:-3]
    if supplied != Path(supplied).name or "\\" in supplied:
        raise DriveError("GRANULE_NAME_INVALID")
    if not granule_folder.startswith("NISAR_L2_"):
        raise DriveError("GRANULE_NAME_INVALID")
    names = ["SARGuardian-DataArchive", product_type, maturity, granule_folder]
    if version_tag:
        if not __import__("re").fullmatch(r"[A-Za-z0-9_.-]{1,100}", version_tag):
            raise DriveError("VERSION_TAG_INVALID")
        names.extend(["Runs", version_tag])
    return names


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path, help='folder containing curated archive/sidecars; raw product is excluded')
    p.add_argument('--product-type', choices=['GUNW', 'GOFF'])
    p.add_argument('--maturity', choices=['PROVISIONAL', 'BETA'])
    p.add_argument('--granule-name')
    p.add_argument('--run-name', help='upload an aggregate run bundle under SARGuardian-DataArchive/Runs/<run-name>')
    p.add_argument('--version-tag', help='isolate a granule upload under <granule>/Runs/<version-tag>; never overwrites prior runs')
    p.add_argument(
        '--existing-files-are-new', action='store_true',
        help='treat matching files in this unique run-scoped folder as retained by this same workflow run (retry-only accounting)',
    )
    p.add_argument(
        '--replace-existing-retry-sidecars', action='store_true',
        help='replace only allowlisted metadata/cleanup receipts in this run-scoped folder after checksum conflict; scientific outputs still fail closed',
    )
    p.add_argument('--files', nargs='+', required=True, help='explicit local files to upload; raw source must NOT be included')
    p.add_argument('--out', required=True, type=Path)
    args = p.parse_args()
    if args.run_name:
        if args.version_tag:
            raise DriveError('RUN_UPLOAD_CANNOT_MIX_RUN_NAME_AND_VERSION_TAG')
        if args.product_type or args.maturity or args.granule_name:
            raise DriveError('RUN_UPLOAD_CANNOT_MIX_GRANULE_AND_RUN_IDENTIFIERS')
        if not __import__('re').fullmatch(r'[A-Za-z0-9_.-]{1,100}', args.run_name):
            raise DriveError('RUN_NAME_INVALID')
    elif not (args.product_type and args.maturity and args.granule_name):
        raise DriveError('GRANULE_UPLOAD_REQUIRES_PRODUCT_MATURITY_AND_NAME')
    if args.existing_files_are_new and (not args.version_tag or args.run_name):
        raise DriveError('SAME_RUN_REUSE_ACCOUNTING_REQUIRES_VERSION_TAG')
    if args.replace_existing_retry_sidecars and not args.existing_files_are_new:
        raise DriveError('RETRY_SIDECAR_REPLACEMENT_REQUIRES_SAME_RUN_RETRY_MODE')
    root = args.root.resolve()
    files = [Path(x).resolve() for x in args.files]
    for path in files:
        if not path.is_file() or path == root: print('ERROR=UPLOAD_FILE_NOT_FOUND', file=sys.stderr); return 2
        if path.suffix.lower() in {'.h5', '.hdf5'} and not (path.name.endswith('.science.h5') or path.name.endswith('.curated.h5')):
            print('ERROR=REFUSING_TO_UPLOAD_RAW_HDF5', file=sys.stderr); return 2
        try: path.relative_to(root)
        except ValueError: print('ERROR=UPLOAD_FILE_OUTSIDE_ROOT', file=sys.stderr); return 2
    # The raw file may be in root itself. Explicit upload allowlist above only lets curated naming patterns pass.
    if len({x.name for x in files}) != len(files): print('ERROR=DUPLICATE_UPLOAD_BASENAMES', file=sys.stderr); return 2
    records = []
    local_total = 0
    for path in files:
        md5, sha, size = hashes(path); local_total += size
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        records.append((path, md5, sha, size, mime))
    drive = Drive()
    limit, usage = drive.quota()
    parent = required_env('GOOGLE_DRIVE_PARENT_FOLDER_ID')
    if args.run_name:
        folder_names = ['SARGuardian-DataArchive', 'Runs', args.run_name]
        granule_folder = None
    else:
        folder_names = granule_folder_names(
            args.product_type, args.maturity, args.granule_name, args.version_tag
        )
        granule_folder = Path(args.granule_name.strip()).name
        if granule_folder.lower().endswith('.h5'): granule_folder = granule_folder[:-3]
    folder = parent
    for name in folder_names: folder = drive.ensure_folder(folder, name)
    # Query exact existing names before calculating projected new bytes.
    # Only allowlisted retry sidecars may be replaced on a same-run retry; science
    # products/exports remain immutable and mismatched content fails closed.
    missing, replacements = plan_existing_uploads(
        drive, folder, records,
        replace_existing_retry_sidecars=args.replace_existing_retry_sidecars,
    )
    # Quota is based on the exact post-processing files in this upload allowlist,
    # not source-file estimates from the acquisition manifest. This check runs
    # after the retained archive and derived outputs have actually been created.
    print(f"CURATED_UPLOAD_FILE_COUNT={len(records)}")
    print(f"CURATED_UPLOAD_ACTUAL_BYTES={local_total}")
    print(f"DRIVE_NEW_BYTES_REQUIRED_ACTUAL={missing}")
    if usage + missing > limit:
        raise DriveError(
            "DRIVE_QUOTA_INSUFFICIENT "
            f"curated_files={len(records)} actual_curated_bytes={local_total} "
            f"required_new_bytes={missing} remaining_bytes={max(0, limit-usage)}"
        )
    print(
        "DRIVE_ACTUAL_OUTPUT_QUOTA_CHECK=PASS "
        f"FILE_COUNT={len(records)} ACTUAL_CURATED_BYTES={local_total} "
        f"NEW_BYTES_REQUIRED={missing}"
    )
    for old in replacements:
        response = drive.request(
            "DELETE", f"{API}/files/{old['id']}",
            params={"supportsAllDrives": "true"},
        )
        response.close()
        print(f"DRIVE_REPLACED_SAME_RUN_RECEIPT name={old['name']}")

    outputs = []
    for path, md5, sha, size, mime in records:
        row = drive.upload_resumable(folder, path, mime, md5, sha, size)
        row = normalise_run_scoped_receipt_row(
            row, existing_files_are_new=args.existing_files_are_new,
        )
        outputs.append(row)
        print(
            f"DRIVE_FILE_VERIFIED name={path.name} bytes={size} "
            f"reused_from_prior_run={str(row['reused']).lower()} "
            f"reused_in_upload_attempt={str(row.get('upload_action_reused', False)).lower()}"
        )
    receipt = {'schema_version':'sarguardian-drive-retention-receipt/1.0','created_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'upload_kind':'run_bundle' if args.run_name else 'single_granule',
        'run_name':args.run_name, 'product_type':args.product_type, 'maturity':args.maturity, 'version_tag':args.version_tag,
        'granule_name':granule_folder, 'remote_folder_id':folder,'files':outputs,
        'uploaded_curated_bytes':sum(x['bytes'] for x in outputs),
        'raw_product_uploaded':False,'remote_verification':'PASS','raw_source_deleted':False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    print(f"DRIVE_VERIFICATION=PASS FILE_COUNT={len(outputs)} REMOTE_FOLDER_ID={folder} RECEIPT={args.out}")
    return 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except DriveError as e:
        print(f'ERROR={e}', file=sys.stderr); raise SystemExit(2)
