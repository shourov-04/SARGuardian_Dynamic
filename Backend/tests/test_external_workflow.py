"""Tests for running the unchanged upstream workflow from the website (external mode).

A real local HTTP server plays both the GitHub REST API and GitHub's signed artifact
storage (including HTTP range requests), so the redirect, range-read and polling code is
exercised end to end.  The tests need only the standard library, and the functions take
no fixtures.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app import config
from app import external_workflow as ew

WKT = "POLYGON((85 27, 85.01 27, 85 27.01, 85 27))"
RUN_ID = 4242
HTML_URL = f"https://github.com/owner/repo/actions/runs/{RUN_ID}"


@contextmanager
def raises(expected: type[BaseException], match: str = ""):
    try:
        yield
    except expected as exc:  # noqa: PERF203
        assert match in str(exc), f"{match!r} not in {str(exc)!r}"
        return
    raise AssertionError(f"{expected.__name__} was not raised")


# ----------------------------------------------------------------------------- fake GitHub


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.dispatch_status = 200
        self.dispatch_payload: dict | None = {"workflow_run_id": RUN_ID, "run_url": "x", "html_url": HTML_URL}
        self.dispatch_error: dict | None = None
        self.run: dict = {"id": RUN_ID, "status": "queued", "conclusion": None, "run_attempt": 1, "html_url": HTML_URL}
        self.run_get_status = 200
        self.artifacts: dict[int, dict] = {}
        self.range_supported = True
        self.blob_bytes_served = 0
        self.blob_authorization: list[str | None] = []
        self.port = 0

    def add_artifact(self, artifact_id: int, name: str, data: bytes, *, expired: bool = False, run_id: int = RUN_ID) -> None:
        self.artifacts[artifact_id] = {"name": name, "data": data, "expired": expired, "run_id": run_id}

    def artifact_json(self, artifact_id: int) -> dict:
        item = self.artifacts[artifact_id]
        return {
            "id": artifact_id, "name": item["name"], "size_in_bytes": len(item["data"]), "expired": item["expired"],
            "expires_at": "2026-11-30T00:00:00Z", "workflow_run": {"id": item["run_id"]},
        }

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def settings(self, **overrides) -> SimpleNamespace:
        values = {
            "github_api_url": self.url, "github_actions_repository": "owner/repo",
            "github_actions_workflow": "sarguardian-aoi-wkt-dashboard.yml", "github_actions_ref": "main",
            "github_actions_token": "test-token", "displacement_dispatch_timeout_seconds": 3600,
        }
        return SimpleNamespace(**(values | overrides))


def _handler(fake: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # silence
            return

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})

        def _record(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fake.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            return body

        def do_POST(self) -> None:  # noqa: N802
            self._record()
            path = urlsplit(self.path).path
            if path.endswith("/dispatches"):
                if fake.dispatch_error is not None:
                    return self._json(fake.dispatch_status, fake.dispatch_error)
                if fake.dispatch_status == 204:
                    return self._send(204)
                return self._json(fake.dispatch_status, fake.dispatch_payload)
            if path.endswith("/cancel"):
                return self._json(202, {})
            self._send(404)

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            base = "/repos/owner/repo/actions"
            if path == f"{base}/runs/{RUN_ID}":
                if fake.run_get_status != 200:
                    return self._json(fake.run_get_status, {"message": "boom"})
                return self._json(200, fake.run)
            if path == f"{base}/runs/{RUN_ID}/artifacts":
                name = query.get("name", [None])[0]
                items = [fake.artifact_json(i) for i, a in fake.artifacts.items() if name is None or a["name"] == name]
                return self._json(200, {"total_count": len(items), "artifacts": items})
            if path.startswith(f"{base}/artifacts/"):
                tail = path[len(f"{base}/artifacts/"):].split("/")
                artifact_id = int(tail[0])
                if artifact_id not in fake.artifacts:
                    return self._json(404, {"message": "Not Found"})
                if len(tail) == 1:
                    return self._json(200, fake.artifact_json(artifact_id))
                if tail[1] == "zip":
                    if fake.artifacts[artifact_id]["expired"]:
                        return self._json(410, {"message": "Gone"})
                    return self._send(302, b"", {"Location": f"{fake.url}/blob/{artifact_id}?sig=signed"})
            if path.startswith("/blob/"):
                fake.blob_authorization.append(self.headers.get("Authorization"))
                data = fake.artifacts[int(path.rsplit("/", 1)[1])]["data"]
                requested = self.headers.get("Range")
                if requested and fake.range_supported:
                    start, _, end = requested.removeprefix("bytes=").partition("-")
                    first, last = int(start), min(int(end), len(data) - 1)
                    chunk = data[first:last + 1]
                    fake.blob_bytes_served += len(chunk)
                    return self._send(206, chunk, {"Content-Range": f"bytes {first}-{last}/{len(data)}"})
                fake.blob_bytes_served += len(data)
                return self._send(200, data)
            self._send(404)

    return Handler


@contextmanager
def fake_github():
    fake = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = ew.ALLOWED_STORAGE_SCHEMES
    ew.ALLOWED_STORAGE_SCHEMES = ("http", "https")
    try:
        yield fake
    finally:
        ew.ALLOWED_STORAGE_SCHEMES = previous
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------------- fixtures as plain helpers


def canonical(wkt: str = WKT) -> str:
    """The upstream discovery tool's canonical WKT: ``%.17g`` coordinates (85.01 becomes 85.010000000000005)."""
    numbers = [float(token) for token in ew._NUMBER_RE.findall(wkt)]
    pairs = [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]
    return "POLYGON ((" + ", ".join(f"{x:.17g} {y:.17g}" for x, y in pairs) + "))"


def make_report(*, status: str = "PASS", products: int = 4, wkt: str = WKT, start: str = "2026-10-01", end: str = "2026-10-22") -> dict:
    rows = [{
        "id": f"{'GUNW' if i % 2 == 0 else 'GOFF'}-{i + 1:03d}", "product_type": "GUNW" if i % 2 == 0 else "GOFF",
        "status": "PASS", "granule_name": f"granule-{i}", "reference_date": "2026-09-01", "secondary_date": "2026-09-13",
        "valid_pixels": 100 + i, "metric_value": 1.5, "export_count": 2,
        "exports": [{"name": f"big-{n}.tif", "bytes": 10_000_000} for n in range(50)], "errors": [],
    } for i in range(products)]
    digest = hashlib.sha256(canonical(wkt).encode()).hexdigest()
    validated = products if status == "PASS" else products - 1
    summary = {
        "status": status, "products_expected": products, "products_validated": validated,
        "aoi_wkt": canonical(wkt), "aoi_wkt_sha256": digest, "date_start": start, "date_end": end,
        "date_filter_mode": "secondary-acquisition-in-window", "run_profile": "full-aoi-baseline",
        "retention_profile": "derived-only", "gunw_count": sum(r["product_type"] == "GUNW" for r in rows),
        "goff_count": sum(r["product_type"] == "GOFF" for r in rows), "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        "orchestration_sha": "abc123", "orchestration_branch": "feat/final-aoi-science-integration",
        "science_diagnostics": {"pixel_stack": {"status": "PASS"}}, "science_claim_limit": "Research product only.",
        "products_with_science_pixels": products,
    }
    return {"summary": summary, "products": rows, "processing_report": {"status": status, "aoi_wkt_sha256": digest}}


def make_artifact_zip(report: dict | None, *, filler_bytes: int = 12_000_000) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dashboard/index.html", "<html></html>")
        if report is not None:
            archive.writestr("dashboard/aoi-processing-report.json", json.dumps(report))
        archive.writestr("dashboard/products.csv", "id\nGUNW-001\n")
        archive.writestr("dashboard/aoi-derived-results.zip", os.urandom(filler_bytes))
        archive.writestr("dashboard-build.log", "ok")
    return buffer.getvalue()


def make_manifest_zip() -> bytes:
    buffer = io.BytesIO()
    manifest = {
        "baseline_product_counts": {"GUNW": 9, "GOFF": 7},
        "products": [{"product_type": "GUNW"}, {"product_type": "GOFF"}, {"product_type": "GUNW"}],
    }
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


def record(**overrides) -> dict:
    created = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    base = {
        "id": "11111111-1111-1111-1111-111111111111", "state": "dispatching", "stage": "dispatching",
        "created_at": created, "github_run_id": str(RUN_ID), "github_run_url": None,
        "aoi_wkt": WKT, "start_date": "2026-10-01", "end_date": "2026-10-22", "result_summary": None,
    }
    return base | overrides


DASHBOARD_NAME = f"sarguardian-aoi-dashboard-{RUN_ID}-1"


# ----------------------------------------------------------------------------- dispatch


def test_dispatch_inputs_are_exactly_the_six_upstream_inputs() -> None:
    inputs = ew.build_dispatch_inputs(
        aoi_wkt=WKT, start_date="2026-10-01", end_date="2026-10-22", acquisition_window="both-acquisitions-in-window",
        retention_mode="full-lossless", processing_profile="full-aoi", product_type="BOTH",
    )
    assert inputs == {
        "aoi_wkt": WKT, "start_date": "2026-10-01", "end_date": "2026-10-22",
        "date_filter_mode": "both-acquisitions-in-window", "run_profile": "full-aoi-baseline",
        "retention_profile": "full-lossless",
    }
    pilot = ew.build_dispatch_inputs(
        aoi_wkt=WKT, start_date="2026-10-01", end_date="2026-10-22", acquisition_window="secondary-acquisition-in-window",
        retention_mode="derived-only", processing_profile="validation-pilot", product_type="BOTH",
    )
    assert pilot["run_profile"] == "validation-pilot"


def test_a_single_product_family_cannot_be_requested_from_the_unchanged_workflow() -> None:
    with raises(ew.ExternalDispatchError, "both GUNW and GOFF"):
        ew.build_dispatch_inputs(
            aoi_wkt=WKT, start_date="2026-10-01", end_date="2026-10-22", acquisition_window="secondary-acquisition-in-window",
            retention_mode="derived-only", processing_profile="full-aoi", product_type="GUNW",
        )


def test_dispatch_targets_the_configured_repository_and_returns_the_run_id() -> None:
    with fake_github() as gh:
        inputs = {"aoi_wkt": WKT, "start_date": "2026-10-01", "end_date": "2026-10-22", "date_filter_mode": "secondary-acquisition-in-window",
                  "run_profile": "full-aoi-baseline", "retention_profile": "derived-only"}
        run = ew.dispatch(gh.settings(), inputs)
        method, path, headers, body = gh.requests[-1]
    assert run == ew.DispatchedRun(str(RUN_ID), HTML_URL)
    assert method == "POST"
    assert path == "/repos/owner/repo/actions/workflows/sarguardian-aoi-wkt-dashboard.yml/dispatches"
    assert headers["authorization"] == "Bearer test-token"
    assert json.loads(body) == {"ref": "main", "inputs": inputs, "return_run_details": True}


def test_dispatch_reports_github_rejection_without_secrets() -> None:
    with fake_github() as gh:
        gh.dispatch_status = 422
        gh.dispatch_error = {"message": "Unexpected inputs provided: [\"job_id\"]"}
        try:
            ew.dispatch(gh.settings(), {})
        except ew.ExternalDispatchError as exc:
            assert "rejected (422): Unexpected inputs provided" in str(exc)
            assert "test-token" not in str(exc) and exc.code == "GITHUB_DISPATCH_FAILED"
        else:
            raise AssertionError("expected ExternalDispatchError")


def test_dispatch_without_a_run_id_is_refused_instead_of_guessing() -> None:
    with fake_github() as gh:
        gh.dispatch_status = 204
        try:
            ew.dispatch(gh.settings(), {})
        except ew.ExternalDispatchError as exc:
            assert exc.code == "EXTERNAL_RUN_ID_UNAVAILABLE"
        else:
            raise AssertionError("expected ExternalDispatchError")


def test_missing_token_or_bad_repository_is_a_configuration_error() -> None:
    with fake_github() as gh:
        with raises(ew.ExternalWorkflowError, "GITHUB_ACTIONS_TOKEN"):
            ew.get_run(gh.settings(github_actions_token=""), str(RUN_ID))
        with raises(ew.ExternalWorkflowError, "owner/repository"):
            ew.get_run(gh.settings(github_actions_repository="not a repo"), str(RUN_ID))
        with raises(ew.ExternalWorkflowError, "run id"):
            ew.get_run(gh.settings(), "../../x")


# ----------------------------------------------------------------------------- artifacts and range reads


def test_signed_url_is_captured_without_following_it_and_without_the_github_token() -> None:
    with fake_github() as gh:
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(), filler_bytes=1000))
        url = ew.artifact_download_url(gh.settings(), 7)
        members = ew.read_zip_members(url, {ew.REPORT_MEMBER: ew.MAX_JSON_MEMBER_BYTES})
        authorizations = list(gh.blob_authorization)
    assert url == f"{gh.url}/blob/7?sig=signed"
    assert json.loads(members[ew.REPORT_MEMBER])["summary"]["status"] == "PASS"
    assert authorizations and all(value is None for value in authorizations)


def test_expired_artifact_download_is_reported_as_unavailable() -> None:
    with fake_github() as gh:
        gh.add_artifact(7, DASHBOARD_NAME, b"x", expired=True)
        with raises(ew.ArtifactUnavailableError, "no longer available"):
            ew.artifact_download_url(gh.settings(), 7)


def test_only_a_small_part_of_a_large_artifact_is_downloaded() -> None:
    with fake_github() as gh:
        data = make_artifact_zip(make_report(), filler_bytes=12_000_000)
        gh.add_artifact(7, DASHBOARD_NAME, data)
        url = ew.artifact_download_url(gh.settings(), 7)
        members = ew.read_zip_members(url, {ew.REPORT_MEMBER: ew.MAX_JSON_MEMBER_BYTES})
        served = gh.blob_bytes_served
    assert len(data) > 11_000_000
    assert json.loads(members[ew.REPORT_MEMBER])["summary"]["products_expected"] == 4
    assert served < len(data) * 0.4, (served, len(data))


def test_servers_that_ignore_range_still_work_for_small_artifacts_and_refuse_huge_ones() -> None:
    with fake_github() as gh:
        gh.range_supported = False
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(), filler_bytes=2000))
        members = ew.read_zip_members(ew.artifact_download_url(gh.settings(), 7), {ew.REPORT_MEMBER: ew.MAX_JSON_MEMBER_BYTES})
        assert json.loads(members[ew.REPORT_MEMBER])["summary"]["status"] == "PASS"
        previous = ew.FULL_DOWNLOAD_FALLBACK_BYTES
        ew.FULL_DOWNLOAD_FALLBACK_BYTES = 100
        try:
            with raises(ew.ExternalWorkflowError, "too large to inspect"):
                ew.read_zip_members(ew.artifact_download_url(gh.settings(), 7), {ew.REPORT_MEMBER: ew.MAX_JSON_MEMBER_BYTES})
        finally:
            ew.FULL_DOWNLOAD_FALLBACK_BYTES = previous


def test_missing_report_and_non_zip_artifacts_are_clear_contract_failures() -> None:
    with fake_github() as gh:
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(None, filler_bytes=500))
        gh.add_artifact(8, "other", b"this is not a zip archive at all")
        with raises(ew.ResultContractError, "does not contain aoi-processing-report.json"):
            ew.read_zip_members(ew.artifact_download_url(gh.settings(), 7), {ew.REPORT_MEMBER: 1000})
        with raises(ew.ArtifactUnavailableError, "not a readable ZIP"):
            ew.read_zip_members(ew.artifact_download_url(gh.settings(), 8), {ew.REPORT_MEMBER: 1000})


def test_find_run_artifact_matches_the_exact_name_and_prefers_the_newest() -> None:
    with fake_github() as gh:
        gh.add_artifact(5, DASHBOARD_NAME, b"old")
        gh.add_artifact(9, DASHBOARD_NAME, b"new")
        gh.add_artifact(11, "sarguardian-aoi-run-manifest", b"m")
        found = ew.find_run_artifact(gh.settings(), str(RUN_ID), DASHBOARD_NAME)
        missing = ew.find_run_artifact(gh.settings(), str(RUN_ID), "nope")
    assert found and found.id == 9 and found.size_in_bytes == 3
    assert missing is None


# ----------------------------------------------------------------------------- result contract


def _run(**overrides) -> ew.RunInfo:
    return ew.RunInfo(**({"run_id": str(RUN_ID), "status": "completed", "conclusion": "success", "html_url": HTML_URL, "run_attempt": 1} | overrides))


def _artifact() -> ew.ArtifactInfo:
    return ew.ArtifactInfo(id=7, name=DASHBOARD_NAME, size_in_bytes=123, expired=False, expires_at="2026-11-30T00:00:00Z", run_id=str(RUN_ID))


def test_valid_report_becomes_a_compact_website_summary() -> None:
    summary = ew.build_result_summary(make_report(products=4), request=record(), run=_run(), artifact=_artifact(), repository="owner/repo")
    assert summary["schema_version"] == 2 and summary["source"] == "github_artifact"
    assert summary["github_artifact"] == {
        "id": 7, "name": DASHBOARD_NAME, "size_in_bytes": 123, "expires_at": "2026-11-30T00:00:00Z",
        "repository": "owner/repo", "run_id": str(RUN_ID), "run_attempt": 1,
    }
    assert [p["product_type"] for p in summary["products"]] == ["GUNW", "GOFF", "GUNW", "GOFF"]
    assert "exports" not in summary["products"][0]
    assert summary["selected_granule_counts"] == {"GUNW": 2, "GOFF": 2}
    assert summary["catalog_granule_counts"] == {"GUNW": 2, "GOFF": 2}
    assert summary["progress"] == {"completed": 4, "total": 4, "failed": 0}
    assert summary["scientific_limit"] == "Research product only."
    assert summary["provenance"]["github_run_id"] == str(RUN_ID)
    assert len(json.dumps(summary)) < 6_000


def test_discovery_counts_from_the_running_stage_are_kept_in_the_final_summary() -> None:
    prior = {"catalog_granule_counts": {"GUNW": 9, "GOFF": 7}}
    summary = ew.build_result_summary(make_report(), request=record(result_summary=prior), run=_run(), artifact=_artifact(),
                                      repository="owner/repo", catalog_counts=prior["catalog_granule_counts"])
    assert summary["catalog_granule_counts"] == {"GUNW": 9, "GOFF": 7}
    assert summary["selected_granule_counts"] == {"GUNW": 2, "GOFF": 2}


def test_result_for_a_different_aoi_or_dates_is_rejected() -> None:
    other = "POLYGON((10 10, 10.1 10, 10 10.1, 10 10))"
    for bad_report, field in ((make_report(wkt=other), "AOI"), (make_report(start="2026-09-01"), "date window")):
        try:
            ew.build_result_summary(bad_report, request=record(), run=_run(), artifact=_artifact(), repository="owner/repo")
        except ew.ResultContractError as exc:
            assert exc.code == "RESULT_REQUEST_MISMATCH" and field in str(exc)
        else:
            raise AssertionError("mismatched result was accepted")


def test_vertex_comparison_ignores_start_vertex_orientation_and_float_formatting() -> None:
    reordered = "POLYGON ((85 27.01, 85 27, 85.01 27, 85 27.01))"
    assert ew._vertices(WKT) == ew._vertices(reordered) == ew._vertices(canonical())


def test_partial_or_inconsistent_reports_are_not_accepted_as_success() -> None:
    with raises(ew.ResultContractError, "status PARTIAL (3/4 products validated)"):
        ew.build_result_summary(make_report(status="PARTIAL"), request=record(), run=_run(), artifact=_artifact(), repository="owner/repo")
    broken = make_report()
    broken["products"].pop()
    with raises(ew.ResultContractError, "inconsistent"):
        ew.build_result_summary(broken, request=record(), run=_run(), artifact=_artifact(), repository="owner/repo")


def test_manifest_progress_counts_are_validated() -> None:
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(make_manifest_zip())).read("manifest.json"))
    progress = ew.build_progress_summary(manifest)
    assert progress["catalog_granule_counts"] == {"GUNW": 9, "GOFF": 7}
    assert progress["selected_granule_counts"] == {"GUNW": 2, "GOFF": 1}
    with raises(ew.ResultContractError, "product counts"):
        ew.build_progress_summary({"products": [], "baseline_product_counts": {"GUNW": -1, "GOFF": 0}})


# ----------------------------------------------------------------------------- refresh planning


def test_queued_run_keeps_the_job_dispatching_and_only_updates_the_stage() -> None:
    with fake_github() as gh:
        steps = ew.plan_refresh(gh.settings(), record())
        again = ew.plan_refresh(gh.settings(), record(stage="github_queued"))
    assert steps == [ew.RefreshStep("dispatching", {"stage": "github_queued", "github_run_url": HTML_URL})]
    assert again == []


def test_running_run_moves_the_job_to_running_and_publishes_discovery_counts() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "in_progress"}
        gh.add_artifact(3, ew.MANIFEST_ARTIFACT_NAME, make_manifest_zip())
        steps = ew.plan_refresh(gh.settings(), record())
        later = ew.plan_refresh(gh.settings(), record(state="running", stage="products_discovered", result_summary=steps[1].values["result_summary"]))
    assert [s.expected_state for s in steps] == ["dispatching", "running"]
    assert steps[0].values["state"] == "running" and steps[0].values["stage"] == "github_running"
    assert steps[1].values["stage"] == "products_discovered"
    assert steps[1].values["result_summary"]["catalog_granule_counts"] == {"GUNW": 9, "GOFF": 7}
    assert later == []


def test_finished_run_walks_the_database_state_machine_in_two_steps() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "completed", "conclusion": "success"}
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(), filler_bytes=3_000_000))
        steps = ew.plan_refresh(gh.settings(), record())
    assert [(s.expected_state, s.values["state"]) for s in steps] == [("dispatching", "running"), ("running", "succeeded")]
    final = steps[1].values
    assert final["stage"] == "completed" and final["error_code"] is None
    assert final["result_summary"]["github_artifact"]["id"] == 7
    assert final["result_summary"]["progress"]["total"] == 4


def test_a_job_already_running_gets_a_single_terminal_step() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "completed", "conclusion": "success"}
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(), filler_bytes=2000))
        steps = ew.plan_refresh(gh.settings(), record(state="running", stage="github_running"))
    assert [(s.expected_state, s.values["state"]) for s in steps] == [("running", "succeeded")]


def test_failed_run_reports_how_far_the_dashboard_got() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "completed", "conclusion": "failure"}
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(status="PARTIAL"), filler_bytes=2000))
        steps = ew.plan_refresh(gh.settings(), record(state="running"))
    values = steps[-1].values
    assert values["state"] == "failed" and values["error_code"] == "GITHUB_ACTIONS_WORKER_FAILED"
    assert "conclusion 'failure'" in values["error_message"] and "PARTIAL: 3/4 products validated" in values["error_message"]


def test_successful_run_with_a_partial_dashboard_fails_with_a_specific_code() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "completed", "conclusion": "success"}
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(status="PARTIAL"), filler_bytes=2000))
        steps = ew.plan_refresh(gh.settings(), record(state="running"))
    assert steps[-1].values["state"] == "failed" and steps[-1].values["error_code"] == "RESULT_NOT_COMPLETE"


def test_cancelled_and_expired_and_missing_artifacts_end_in_clear_states() -> None:
    with fake_github() as gh:
        gh.run = gh.run | {"status": "completed", "conclusion": "cancelled"}
        cancelled = ew.plan_refresh(gh.settings(), record(state="running"))
        gh.run = gh.run | {"conclusion": "success"}
        missing = ew.plan_refresh(gh.settings(), record(state="running"))
        gh.add_artifact(7, DASHBOARD_NAME, b"x", expired=True)
        expired = ew.plan_refresh(gh.settings(), record(state="running"))
    assert cancelled[-1].values["state"] == "cancelled" and cancelled[-1].values["error_code"] == "CANCELLED_ON_GITHUB"
    assert missing[-1].values["error_code"] == "RESULT_ARTIFACT_UNAVAILABLE" and "did not publish" in missing[-1].values["error_message"]
    assert expired[-1].values["error_code"] == "RESULT_ARTIFACT_UNAVAILABLE" and "expired" in expired[-1].values["error_message"]


def test_transient_github_errors_never_fail_a_job() -> None:
    with fake_github() as gh:
        gh.run_get_status = 502
        assert ew.plan_refresh(gh.settings(), record(state="running")) == []
        gh.run_get_status = 403
        assert ew.plan_refresh(gh.settings(), record(state="running")) == []
        gh.run_get_status = 404
        steps = ew.plan_refresh(gh.settings(), record(state="running"))
    assert steps[0].values["error_code"] == "GITHUB_RUN_NOT_FOUND"


def test_terminal_jobs_are_never_touched_and_a_stuck_queue_times_out() -> None:
    with fake_github() as gh:
        assert ew.plan_refresh(gh.settings(), record(state="succeeded")) == []
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        steps = ew.plan_refresh(gh.settings(), record(created_at=old))
        cancels = [r for r in gh.requests if r[1].endswith("/cancel")]
    assert steps[0].values["error_code"] == "WORKFLOW_START_TIMEOUT"
    assert len(cancels) == 1


# ----------------------------------------------------------------------------- downloads and throttle


def test_download_link_requires_the_stored_artifact_to_match_github() -> None:
    with fake_github() as gh:
        gh.add_artifact(7, DASHBOARD_NAME, make_artifact_zip(make_report(), filler_bytes=500))
        summary = ew.build_result_summary(make_report(), request=record(), run=_run(), artifact=_artifact(), repository="owner/repo")
        url, filename = ew.result_download_link(gh.settings(), summary)
        tampered = json.loads(json.dumps(summary))
        tampered["github_artifact"]["run_id"] = "999"
        with raises(ew.ArtifactUnavailableError, "no longer matches"):
            ew.result_download_link(gh.settings(), tampered)
        with raises(ew.ArtifactUnavailableError, "no downloadable"):
            ew.result_download_link(gh.settings(), {"products": []})
        gh.artifacts[7]["expired"] = True
        with raises(ew.ArtifactUnavailableError, "expired"):
            ew.result_download_link(gh.settings(), summary)
    assert url == f"{gh.url}/blob/7?sig=signed" and filename == f"{DASHBOARD_NAME}.zip"


def test_polling_is_throttled_per_job() -> None:
    assert ew.should_poll("job-a", min_interval=8, now=100.0) is True
    assert ew.should_poll("job-a", min_interval=8, now=105.0) is False
    assert ew.should_poll("job-b", min_interval=8, now=105.0) is True
    assert ew.should_poll("job-a", min_interval=8, now=108.5) is True


def test_external_mode_detection_is_tolerant_of_minimal_settings() -> None:
    assert ew.is_external_mode(SimpleNamespace(displacement_actions_enabled=True)) is False
    assert ew.is_external_mode(SimpleNamespace(displacement_workflow_mode="external")) is True
    assert ew.is_external_mode(SimpleNamespace(displacement_workflow_mode="callback")) is False


# ----------------------------------------------------------------------------- settings

PRODUCTION_BASE = {
    "APP_ENV": "production", "CORS_ORIGINS": "https://site.example", "SUPABASE_URL": "https://project.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "server-key", "DISPLACEMENT_ACTIONS_ENABLED": "true",
}


def test_workflow_mode_defaults_to_the_existing_callback_behaviour() -> None:
    assert config.get_settings({}).displacement_workflow_mode == "callback"
    assert config.get_settings({"DISPLACEMENT_WORKFLOW_MODE": " External "}).displacement_workflow_mode == "external"
    with raises(config.SettingsError, "DISPLACEMENT_WORKFLOW_MODE is invalid"):
        config.get_settings({"DISPLACEMENT_WORKFLOW_MODE": "magic"})


def test_external_mode_needs_no_callback_secret_or_public_api_url_but_callback_mode_still_does() -> None:
    external = config.get_settings(PRODUCTION_BASE | {
        "DISPLACEMENT_WORKFLOW_MODE": "external", "GITHUB_ACTIONS_TOKEN": "token",
        "GITHUB_ACTIONS_REPOSITORY": "moshiour0/SARGuardian-backend", "GITHUB_ACTIONS_WORKFLOW": "sarguardian-aoi-wkt-dashboard.yml",
    })
    assert config.validate_production_settings(external) is external
    callback = config.get_settings(PRODUCTION_BASE | {"GITHUB_ACTIONS_TOKEN": "token", "GITHUB_ACTIONS_REPOSITORY": "owner/repo"})
    with raises(config.SettingsError, "DISPLACEMENT_CALLBACK_SECRET, PUBLIC_API_URL"):
        config.validate_production_settings(callback)


def test_external_mode_requires_the_token_and_a_well_formed_repository() -> None:
    missing = config.get_settings(PRODUCTION_BASE | {"DISPLACEMENT_WORKFLOW_MODE": "external"})
    with raises(config.SettingsError, "GITHUB_ACTIONS_TOKEN, GITHUB_ACTIONS_REPOSITORY"):
        config.validate_production_settings(missing)
    malformed = config.get_settings(PRODUCTION_BASE | {
        "DISPLACEMENT_WORKFLOW_MODE": "external", "GITHUB_ACTIONS_TOKEN": "token", "GITHUB_ACTIONS_REPOSITORY": "not a repo",
    })
    with raises(config.SettingsError, "owner/repository"):
        config.validate_production_settings(malformed)
