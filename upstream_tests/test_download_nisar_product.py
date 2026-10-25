from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "download_nisar_product.py"
spec = importlib.util.spec_from_file_location("download_nisar_product", TOOL)
assert spec and spec.loader
download = importlib.util.module_from_spec(spec)
spec.loader.exec_module(download)


WANTED = (
    "NISAR_L2_PR_GUNW_006_048_D_074_007_2000_SH_"
    "20251125T125813_20251125T125848_20251207T125814_20251207T125849_X05010_N_F_J_001"
)


class FakeResponse:
    def __init__(self, status_code, headers=None, reason="OK"):
        self.status_code = status_code
        self.headers = headers or {}
        self.reason = reason
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, head_response=None, get_response=None):
        self.head_response = head_response
        self.get_response = get_response
        self.head_calls = []
        self.get_calls = []

    def head(self, url, **kwargs):
        self.head_calls.append((url, kwargs))
        if isinstance(self.head_response, list):
            return self.head_response.pop(0)
        return self.head_response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_response


class FakeSourceAcquisition:
    def __init__(self, session):
        self.session = session
        self.AOI_NAME = None

    def build_session(self):
        return self.session


def test_direct_url_uses_documented_collection_folder_and_exact_file_path():
    expected = (
        "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/"
        f"NISAR_L2_GUNW_PROVISIONAL_V1/{WANTED}/{WANTED}.h5"
    )
    assert download.direct_product_url(WANTED, "GUNW", "PROVISIONAL") == expected
    assert download.trusted_asf_url(expected)


def test_direct_url_uses_requested_maturity_and_product_family():
    other = download.direct_product_url(WANTED.replace("GUNW", "GOFF"), "GOFF", "BETA")
    assert "/NISAR_L2_GOFF_BETA_V1/" in other
    assert other.endswith(f"/{WANTED.replace('GUNW', 'GOFF')}.h5")


def test_probe_direct_product_reads_exact_remote_content_length():
    url = download.direct_product_url(WANTED, "GUNW", "PROVISIONAL")
    response = FakeResponse(200, {"Content-Length": "123456789"})
    session = FakeSession(head_response=response)
    acquisition = FakeSourceAcquisition(session)

    record, returned_session, size = download.probe_direct_product(
        acquisition, WANTED, "GUNW", "PROVISIONAL"
    )

    assert session.head_calls[0][0] == url
    assert session.head_calls[0][1] == {"allow_redirects": True, "timeout": (10, 30)}
    assert session.get_calls == []
    assert record["granuleName"] == WANTED
    assert record["productType"] == "GUNW"
    assert record["maturity"] == "PROVISIONAL"
    assert record["downloadUrl"] == url
    assert record["_sarguardian_discovery_backend"] == "direct_https_known_granule"
    assert returned_session is session
    assert size == 123456789
    assert response.closed is True


def test_probe_direct_product_uses_range_get_when_head_is_not_supported():
    url = download.direct_product_url(WANTED, "GUNW", "PROVISIONAL")
    head = FakeResponse(405, reason="Method Not Allowed")
    ranged = FakeResponse(206, {"Content-Range": "bytes 0-0/987654321"}, reason="Partial Content")
    session = FakeSession(head_response=head, get_response=ranged)

    _record, _session, size = download.probe_direct_product(
        FakeSourceAcquisition(session), WANTED, "GUNW", "PROVISIONAL"
    )

    assert head.closed is True
    assert session.get_calls[0][0] == url
    assert session.get_calls[0][1] == {
        "headers": {"Range": "bytes=0-0"},
        "allow_redirects": True,
        "stream": True,
        "timeout": (10, 30),
    }
    assert ranged.closed is True
    assert size == 987654321


def test_probe_direct_product_fails_closed_for_missing_direct_object():
    response = FakeResponse(404, reason="Not Found")
    session = FakeSession(head_response=response)
    with pytest.raises(RuntimeError, match="DIRECT_PRODUCT_HTTP_STATUS_404"):
        download.probe_direct_product(
            FakeSourceAcquisition(session), WANTED, "GUNW", "PROVISIONAL"
        )
    assert response.closed is True


def test_trusted_asf_url_accepts_https_asf_hosts_only():
    assert download.trusted_asf_url("https://datapool.asf.alaska.edu/a.h5")
    assert download.trusted_asf_url("https://nisar.asf.earthdatacloud.nasa.gov/a.h5")
    assert not download.trusted_asf_url("http://datapool.asf.alaska.edu/a.h5")
    assert not download.trusted_asf_url("https://example.invalid/a.h5")


def test_safe_remote_error_redacts_urls_and_secret_like_fields():
    exc = RuntimeError("failed https://host.invalid/file.h5 token=secret-value")
    text = download.safe_remote_error(exc)
    assert "[URL]" in text
    assert "secret-value" not in text
    assert "[REDACTED]" in text


def test_load_source_acquisition_sets_requested_aoi_and_records_module_hash(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    module_file = src / "nisar_acquisition.py"
    module_file.write_text(
        "AOI_NAME = None\n"
        "def set_aoi(name):\n"
        "    global AOI_NAME\n"
        "    AOI_NAME = name\n",
        encoding="utf-8",
    )
    module, module_hash = download.load_source_acquisition(tmp_path, "source")
    assert module.AOI_NAME == "source"
    assert len(module_hash) == 64

def test_probe_url_retries_transient_http_status(monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    session = FakeSession(
        head_response=[
            FakeResponse(503, reason="Service Unavailable"),
            FakeResponse(200, {"Content-Length": "123456"}),
        ]
    )
    size = download.probe_url_content_length(
        session,
        "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/example.h5",
    )
    assert size == 123456
    assert len(session.head_calls) == 2


def test_authenticated_session_retries_connection_error(monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    session = FakeSession(head_response=FakeResponse(200, {"Content-Length": "123"}))

    class FlakyAcquisition:
        def __init__(self):
            self.calls = 0

        def build_session(self):
            self.calls += 1
            if self.calls == 1:
                raise download.requests.exceptions.ConnectionError("temporary endpoint outage")
            return session

    acquisition = FlakyAcquisition()
    assert download.build_authenticated_session_with_retry(acquisition) is session
    assert acquisition.calls == 2


def test_urgent_response_product_uses_exact_asf_catalog_url(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    urgent = WANTED.replace("_PR_GUNW_", "_UR_GUNW_")
    url = f"https://nisar.asf.earthdatacloud.nasa.gov/UR/{urgent}/{urgent}.h5"

    class FakeCatalogProduct:
        properties = {"fileName": f"{urgent}.h5", "bytes": 123}

        def get_urls(self):
            return [url]

    calls = []
    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(search=lambda **kwargs: (calls.append(kwargs) or [FakeCatalogProduct()])),
    )
    session = FakeSession(head_response=FakeResponse(200, {"Content-Length": "123"}))
    acquisition = FakeSourceAcquisition(session)

    record, returned_session, size = download.probe_known_product(
        acquisition, urgent, "GUNW", "BETA"
    )

    assert calls == [{"dataset": "NISAR", "granule_list": [urgent]}]
    assert record["downloadUrl"] == url
    assert record["_sarguardian_discovery_backend"] == "asf_catalog_exact_granule"
    assert returned_session is session
    assert size == 123


def test_missing_urgent_response_catalog_entry_is_actionable(monkeypatch):
    from types import SimpleNamespace

    urgent = WANTED.replace("_PR_GUNW_", "_UR_GUNW_")
    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(search=lambda **kwargs: []),
    )
    with pytest.raises(RuntimeError, match="UR_GRANULE_NOT_FOUND_IN_ASF_CATALOG"):
        download._catalog_record_for_exact_granule(urgent, "GUNW", "BETA")


def test_non_urgent_direct_404_falls_back_to_exact_catalog_url(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    catalog_url = f"https://datapool.asf.alaska.edu/{WANTED}.h5"

    class FakeCatalogProduct:
        properties = {"fileName": f"{WANTED}.h5"}

        def get_urls(self):
            return [catalog_url]

    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(search=lambda **kwargs: [FakeCatalogProduct()]),
    )
    session = FakeSession(
        head_response=[
            FakeResponse(404, reason="Not Found"),
            FakeResponse(200, {"Content-Length": "987"}),
        ]
    )
    record, returned_session, size = download.probe_known_product(
        FakeSourceAcquisition(session), WANTED, "GUNW", "PROVISIONAL"
    )

    assert record["downloadUrl"] == catalog_url
    assert record["_sarguardian_discovery_backend"] == "asf_catalog_exact_granule"
    assert returned_session is session
    assert size == 987
    assert len(session.head_calls) == 2


def test_urgent_response_catalog_url_404_reports_possible_expiration(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    urgent = WANTED.replace("_PR_GUNW_", "_UR_GUNW_")
    url = f"https://nisar.asf.earthdatacloud.nasa.gov/UR/{urgent}/{urgent}.h5"

    class FakeCatalogProduct:
        properties = {"fileName": f"{urgent}.h5"}

        def get_urls(self):
            return [url]

    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(search=lambda **kwargs: [FakeCatalogProduct()]),
    )
    session = FakeSession(head_response=FakeResponse(404, reason="Not Found"))

    with pytest.raises(RuntimeError, match="UR_GRANULE_OBJECT_EXPIRED_OR_NOT_AVAILABLE_IN_UR_BUCKET"):
        download.probe_known_product(
            FakeSourceAcquisition(session), urgent, "GUNW", "BETA"
        )


def test_probe_only_reports_availability_without_creating_download_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EARTHDATA_USERNAME", "user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "password")
    out_dir = tmp_path / "must-not-be-created"
    record = {
        "granuleName": WANTED,
        "productType": "GUNW",
        "maturity": "PROVISIONAL",
        "downloadUrl": download.direct_product_url(WANTED, "GUNW", "PROVISIONAL"),
        "_sarguardian_discovery_backend": "direct_https_known_granule",
    }
    session = object()
    monkeypatch.setattr(download, "load_source_acquisition", lambda *_args: (object(), "a" * 64))
    monkeypatch.setattr(download, "probe_known_product", lambda *_args: (record, session, 123456))
    monkeypatch.setattr(
        download.sys,
        "argv",
        [
            str(TOOL),
            "--product-type", "GUNW",
            "--maturity", "PROVISIONAL",
            "--granule-name", WANTED,
            "--science-root", str(tmp_path),
            "--out-dir", str(out_dir),
            "--probe-only",
        ],
    )

    assert download.main() == 0
    output = capsys.readouterr().out
    assert "PRODUCT_PREFLIGHT_STATUS=PASS" in output
    assert "DISCOVERY_BACKEND=direct_https_known_granule" in output
    assert not out_dir.exists()



def test_retryable_remote_failure_classifier_only_retries_transient_failures():
    assert download.is_retryable_remote_failure(download.requests.exceptions.ConnectionError("network down"))
    assert download.is_retryable_remote_failure(download.requests.exceptions.Timeout("timed out"))
    permanent_http = download.requests.exceptions.HTTPError("not found")
    permanent_http.response = FakeResponse(404, reason="Not Found")
    transient_http = download.requests.exceptions.HTTPError("temporary unavailable")
    transient_http.response = FakeResponse(503, reason="Service Unavailable")
    assert not download.is_retryable_remote_failure(permanent_http)
    assert download.is_retryable_remote_failure(transient_http)
    assert not download.is_retryable_remote_failure(RuntimeError("INVALID_HDF5_CONTAINER"))


def test_authenticated_session_can_be_configured_for_more_than_two_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(download.time, "sleep", lambda seconds: sleeps.append(seconds))
    session = FakeSession(head_response=FakeResponse(200, {"Content-Length": "123"}))

    class FlakyAcquisition:
        def __init__(self):
            self.calls = 0

        def build_session(self):
            self.calls += 1
            if self.calls < 4:
                raise download.requests.exceptions.ConnectionError("temporary endpoint outage")
            return session

    acquisition = FlakyAcquisition()
    assert download.build_authenticated_session_with_retry(acquisition, attempts=4) is session
    assert acquisition.calls == 4
    assert len(sleeps) == 3


def test_pre_generated_earthdata_user_token_bypasses_login_token_endpoint(monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.setenv("EARTHDATA_TOKEN", "pre-generated-user-token")
    calls = []
    expected_session = object()

    class FakeASFSession:
        def auth_with_token(self, token):
            calls.append(token)
            return expected_session

    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(ASFSession=FakeASFSession),
    )

    class CredentialsMustNotBeUsed:
        def build_session(self):
            raise AssertionError("user token mode must not request a new token")

    session = download.build_authenticated_session_with_retry(CredentialsMustNotBeUsed())

    assert session is expected_session
    assert calls == ["pre-generated-user-token"]
    assert "EARTHDATA_AUTH_MODE=pre_generated_user_token" in capsys.readouterr().err


def test_username_password_exchange_installs_bearer_without_asf_cookie(monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    monkeypatch.setenv("EARTHDATA_USERNAME", "user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "password")
    calls = []

    class TokenResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "issued-user-token"}

        def close(self):
            calls.append("closed")

    class FakeASFSession:
        edl_host = "urs.earthdata.nasa.gov"

        def post(self, url, **kwargs):
            calls.append((url, kwargs["timeout"]))
            return TokenResponse()

        def _update_edl_token(self, token):
            calls.append(("token", token))

    monkeypatch.setitem(
        download.sys.modules,
        "asf_search",
        SimpleNamespace(ASFSession=FakeASFSession),
    )

    session = download.build_authenticated_session_with_retry(object())

    assert isinstance(session, FakeASFSession)
    assert calls[0] == ("https://urs.earthdata.nasa.gov/api/users/find_or_create_token", (10, 20))
    assert ("token", "issued-user-token") in calls
    assert "closed" in calls
    assert "username_password_bearer_token_exchange" in capsys.readouterr().err


def test_user_token_is_sufficient_without_username_password(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EARTHDATA_TOKEN", "pre-generated-user-token")
    monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
    monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
    out_dir = tmp_path / "must-not-be-created-token"
    record = {
        "granuleName": WANTED,
        "productType": "GUNW",
        "maturity": "PROVISIONAL",
        "downloadUrl": download.direct_product_url(WANTED, "GUNW", "PROVISIONAL"),
        "_sarguardian_discovery_backend": "direct_https_known_granule",
    }
    monkeypatch.setattr(download, "load_source_acquisition", lambda *_args: (object(), "a" * 64))
    monkeypatch.setattr(download, "probe_known_product", lambda *_args: (record, object(), 123456))
    monkeypatch.setattr(
        download.sys,
        "argv",
        [str(TOOL), "--product-type", "GUNW", "--maturity", "PROVISIONAL",
         "--granule-name", WANTED, "--science-root", str(tmp_path),
         "--out-dir", str(out_dir), "--probe-only"],
    )

    assert download.main() == 0
    assert "PRODUCT_PREFLIGHT_STATUS=PASS" in capsys.readouterr().out
    assert not out_dir.exists()


def test_rejected_user_token_falls_back_once_to_credentials(monkeypatch, capsys):
    monkeypatch.setenv("EARTHDATA_TOKEN", "expired-user-token")
    monkeypatch.setenv("EARTHDATA_USERNAME", "user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "password")
    token_session = FakeSession(head_response=FakeResponse(401, reason="Unauthorized"))
    credential_session = FakeSession(head_response=FakeResponse(200, {"Content-Length": "987654"}))
    auth_modes = []
    monkeypatch.setattr(
        download,
        "build_authenticated_session_with_retry",
        lambda _acquisition, force_credentials=False: (
            auth_modes.append(force_credentials) or
            (credential_session if force_credentials else token_session)
        ),
    )
    acquisition = FakeSourceAcquisition(credential_session)

    record, returned_session, size = download.probe_known_product(
        acquisition, WANTED, "GUNW", "PROVISIONAL"
    )

    assert returned_session is credential_session
    assert size == 987654
    assert record["granuleName"] == WANTED
    assert auth_modes == [False, True]
    output = capsys.readouterr().err
    assert "EARTHDATA_USER_TOKEN_REJECTED=retrying_once_with_username_password_session" in output


class FakeDownloadResponse(FakeResponse):
    def __init__(self, status_code=200, body_chunks=None, headers=None, reason="OK"):
        super().__init__(status_code, headers=headers, reason=reason)
        self.body_chunks = list(body_chunks or [])

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = download.requests.exceptions.HTTPError(f"HTTP {self.status_code}: {self.reason}")
            exc.response = self
            raise exc

    def iter_content(self, chunk_size=1):
        yield from self.body_chunks


def test_streamed_download_uses_connect_read_timeouts_and_checks_size(tmp_path):
    response = FakeDownloadResponse(
        body_chunks=[b"abcd", b"efgh"],
        headers={"Content-Length": "8"},
    )
    session = FakeSession(get_response=response)
    out = tmp_path / "product.h5"

    size = download.download_exact_product_with_timeout(
        session,
        "https://datapool.asf.alaska.edu/product.h5",
        out,
        expected_bytes=8,
        progress_interval_bytes=4,
    )

    assert size == 8
    assert out.read_bytes() == b"abcdefgh"
    assert session.get_calls[0][1] == {
        "allow_redirects": True,
        "stream": True,
        "timeout": (30, 120),
    }
    assert response.closed


def test_streamed_download_removes_incomplete_file_and_marks_size_mismatch_retryable(tmp_path):
    response = FakeDownloadResponse(
        body_chunks=[b"partial"],
        headers={"Content-Length": "7"},
    )
    session = FakeSession(get_response=response)
    out = tmp_path / "product.h5"

    with pytest.raises(RuntimeError, match="DOWNLOAD_CONTENT_LENGTH_MISMATCH"):
        download.download_exact_product_with_timeout(
            session,
            "https://datapool.asf.alaska.edu/product.h5",
            out,
            expected_bytes=99,
        )

    assert not out.exists()
    assert download.is_retryable_remote_failure(
        RuntimeError("DOWNLOAD_CONTENT_LENGTH_MISMATCH expected=99 actual=7")
    )
    assert response.closed
