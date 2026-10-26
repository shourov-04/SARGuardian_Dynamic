from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discover_aoi_products as discovery


class FakeResponse:
    status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {"granuleName": "NISAR_L2_PR_GUNW_001_TEST_20250510T010101_20250510T010202_20250522T010101_20250522T010202_P05023.h5"}}
            ],
        }


class FakeSession:
    def __init__(self):
        self.calls = []
    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse()


def test_record_name_supports_flat_and_case_independent_fields():
    assert discovery.record_name({"granuleName": "NISAR_L2_PR_GUNW_001_TEST.h5"}) == "NISAR_L2_PR_GUNW_001_TEST"
    assert discovery.record_name({"FILE_NAME": "/tmp/EXAMPLE.H5"}) == "EXAMPLE"
    assert discovery.record_name({"properties": {"granuleName": "x"}}) == ""


def test_pair_secondary_date_is_third_timestamp():
    name = "NISAR_L2_PR_GUNW_001_TEST_20250510T010101_20250510T010202_20250522T010101_20250522T010202_P05023"
    dates = discovery.product_dates(name)
    assert dates is not None
    assert dates[0].isoformat() == "2025-05-10"
    assert dates[1].isoformat() == "2025-05-22"


def test_asf_query_uses_documented_nisar_searchable_fields_and_normalizes_geojson_properties():
    session = FakeSession()
    wkt = "POLYGON ((85 28, 86 28, 86 29, 85 29, 85 28))"
    rows = discovery.query_asf_polygon(
        wkt, "GUNW", discovery.parse_date("2025-12-31", "END_DATE"),
        maturity_filter="PROVISIONAL", session=session
    )
    assert len(rows) == 1
    assert rows[0]["granuleName"].startswith("NISAR_L2_PR_GUNW")
    assert len(session.calls) == 1
    url, params, timeout = session.calls[0]
    assert url == discovery.ASF_API
    assert params["intersectsWith"] == wkt
    assert params["processingLevel"] == "GUNW"
    assert params["dataset"] == "NISAR"
    assert params["dataMaturity"] == "PROVISIONAL"
    assert rows[0]["_sarguardian_query_maturity"] == "PROVISIONAL"
    assert "platform" not in params
    assert "shortName" not in params
    assert params["output"] == "geojson"
    assert timeout == (10, 30)



def test_realistic_nisar_provisional_granule_name_is_accepted_and_pair_dates_parse():
    name = (
        "NISAR_L2_PR_GUNW_024_098_A_016_025_4000_SH_"
        "20260702T233920_20260702T233955_20260714T233920_"
        "20260714T233954_P05023_F_F_J_001"
    )
    match = discovery.PRODUCT_PATTERN.fullmatch(name)
    assert match is not None
    assert match.group(1) == "GUNW"
    dates = discovery.product_dates(name)
    assert dates is not None
    assert dates[0].isoformat() == "2026-07-02"
    assert dates[1].isoformat() == "2026-07-14"



class EmptyResponse(FakeResponse):
    def json(self):
        return {"type": "FeatureCollection", "features": []}


class EmptyPrimaryThenLegacySession:
    def __init__(self):
        self.calls = []
    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        if "dataset" in params:
            return EmptyResponse()
        return FakeResponse()


def test_empty_collection_query_retries_with_sarguardian_legacy_catalog_pattern():
    session = EmptyPrimaryThenLegacySession()
    wkt = "POLYGON ((84.9 28.2, 85.2 28.2, 85.2 28.5, 84.9 28.5, 84.9 28.2))"
    rows = discovery.query_asf_polygon(
        wkt, "GUNW", discovery.parse_date("2026-09-15", "END_DATE"), session=session
    )
    assert len(rows) == 1
    assert len(session.calls) == 2
    primary = session.calls[0][1]
    fallback = session.calls[1][1]
    assert primary["intersectsWith"] == wkt
    assert primary["dataset"] == "NISAR"
    assert primary["processingLevel"] == "GUNW"
    assert "shortName" not in primary
    assert fallback["intersectsWith"] == wkt
    assert fallback["platform"] == "NISAR"
    assert fallback["processingLevel"] == "GUNW"
    assert "dataset" not in fallback
    assert "shortName" not in fallback



class NestedListResponse:
    status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        name = (
            "NISAR_L2_PR_GUNW_024_098_A_016_025_4000_SH_"
            "20260702T233920_20260702T233955_20260714T233920_"
            "20260714T233954_P05023_F_F_J_001.h5"
        )
        return [[{"granuleName": name, "properties": {"productType": "GUNW"}}]]


class NestedListSession:
    def __init__(self):
        self.calls = []
    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return NestedListResponse()


def test_asf_nested_list_of_records_is_flattened_before_product_filtering():
    session = NestedListSession()
    wkt = "POLYGON ((84.9 28.2, 85.2 28.2, 85.2 28.5, 84.9 28.5, 84.9 28.2))"
    rows = discovery.query_asf_polygon(
        wkt, "GUNW", discovery.parse_date("2026-09-15", "END_DATE"), session=session
    )
    assert len(rows) == 1
    assert rows[0]["granuleName"].startswith("NISAR_L2_PR_GUNW_024")
    assert discovery.record_name(rows[0]).startswith("NISAR_L2_PR_GUNW_024")
    assert session.calls[0][1]["output"] == "geojson"
    assert session.calls[0][1]["processingLevel"] == "GUNW"



def test_strict_date_window_requires_both_pair_acquisitions_inside_interval():
    start = discovery.parse_date("2026-06-17", "START_DATE")
    end = discovery.parse_date("2026-09-15", "END_DATE")
    ref = discovery.parse_date("2026-06-08", "REFERENCE_DATE")
    sec = discovery.parse_date("2026-06-20", "SECONDARY_DATE")
    ok, reason = discovery.date_window_decision(
        ref, sec, start, end, "both-acquisitions-in-window"
    )
    assert not ok
    assert reason == "REFERENCE_ACQUISITION_OUTSIDE_DATE_WINDOW"
    ok, reason = discovery.date_window_decision(
        ref, sec, start, end, "secondary-acquisition-in-window"
    )
    assert ok and reason is None


def test_default_window_mode_selects_by_secondary_date_and_preserves_boundary_pairs():
    start = discovery.parse_date("2026-06-17", "START_DATE")
    end = discovery.parse_date("2026-09-15", "END_DATE")
    reference = discovery.parse_date("2026-06-08", "REFERENCE_DATE")
    secondary = discovery.parse_date("2026-06-20", "SECONDARY_DATE")

    assert discovery.DEFAULT_DATE_FILTER_MODE == "secondary-acquisition-in-window"
    included, reason = discovery.date_window_decision(
        reference, secondary, start, end, discovery.DEFAULT_DATE_FILTER_MODE
    )
    assert included and reason is None
    strict_included, strict_reason = discovery.date_window_decision(
        reference, secondary, start, end, "both-acquisitions-in-window"
    )
    assert not strict_included
    assert strict_reason == "REFERENCE_ACQUISITION_OUTSIDE_DATE_WINDOW"


def test_maturity_uses_asf_data_maturity_for_unmapped_crid_and_records_caveat():
    provisional_name = (
        "NISAR_L2_PR_GUNW_024_098_A_016_025_4000_SH_"
        "20260702T233920_20260702T233955_20260714T233920_"
        "20260714T233954_P05023_N_F_J_001"
    )
    mat, evidence, error, source = discovery.product_maturity_evidence(
        {
            "_sarguardian_query_maturity": "PROVISIONAL",
            "collectionName": "NISAR_L2_GUNW_PROVISIONAL_V1",
        },
        provisional_name, lambda _name: "PROVISIONAL"
    )
    assert mat == "PROVISIONAL"
    assert error is None
    assert source == "ASF_dataMaturity_filter_plus_collection"
    assert "P05023" in evidence

    # The current ASF catalog's explicit PROVISIONAL filter and collection
    # label include X05026, although the pinned local CRID map does not know it.
    # Use ASF's authoritative maturity classification and keep the caveat visible.
    unmapped_name = provisional_name.replace("P05023", "X05026")
    mat, evidence, error, source = discovery.product_maturity_evidence(
        {
            "_sarguardian_query_maturity": "PROVISIONAL",
            "collectionName": "NISAR_L2_GUNW_PROVISIONAL_V1",
        },
        unmapped_name, lambda _name: "UNKNOWN"
    )
    assert mat == "PROVISIONAL"
    assert error is None
    assert source == "ASF_dataMaturity_filter_plus_collection"
    assert "X05026" in evidence
    assert "local_crid_map=UNKNOWN" in evidence

    # If the explicit ASF class conflicts with a CRID the science parser
    # already understands, do not hide the disagreement.
    mat, evidence, error, source = discovery.product_maturity_evidence(
        {
            "_sarguardian_query_maturity": "BETA",
            "collectionName": "NISAR_L2_GUNW_BETA_V1",
        },
        provisional_name, lambda _name: "PROVISIONAL"
    )
    assert mat == "UNKNOWN"
    assert error == "CRID_AND_ASF_MATURITY_CONFLICT"
    assert source == "UNRESOLVED"

    unknown_without_catalog_label = provisional_name.replace("P05023", "X05026")
    mat, evidence, error, source = discovery.product_maturity_evidence(
        {}, unknown_without_catalog_label, lambda _name: "UNKNOWN"
    )
    assert mat == "UNKNOWN"
    assert error == "MATURITY_UNKNOWN_NO_AUTHORITATIVE_CATALOG_LABEL"
    assert source == "UNRESOLVED"



def test_catalog_exact_hdf5_byte_map_is_used_for_download_planning():
    granule = (
        "NISAR_L2_PR_GUNW_024_098_A_016_025_4000_SH_"
        "20260702T233920_20260702T233955_20260714T233920_"
        "20260714T233954_P05023_N_F_J_001"
    )
    expected = 2_214_592_512
    row = {
        "bytes": {
            granule + ".h5": {"bytes": expected, "format": "HDF5"},
            granule + "_LATLON.png": {"bytes": 2429011, "format": "PNG"},
        }
    }
    size, basis = discovery.product_size_bytes(row, "GUNW", granule)
    assert size == expected
    assert basis == "ASF_catalog_bytes_map_exact_granule_HDF5"


def test_observation_pair_count_key_collapses_product_families_but_retains_exact_pair_identity():
    stem = (
        "NISAR_L2_PR_{family}_024_098_A_016_025_4000_SH_"
        "20260702T233920_20260702T233955_20260714T233920_"
        "20260714T233954_{crid}_N_F_J_001"
    )
    gunw_p = stem.format(family="GUNW", crid="P05023")
    gunw_x = stem.format(family="GUNW", crid="X05026")
    goff_p = stem.format(family="GOFF", crid="P05023")

    assert discovery.acquisition_pair_key(gunw_p) == discovery.acquisition_pair_key(gunw_x)
    assert discovery.acquisition_observation_key(gunw_p) == discovery.acquisition_observation_key(goff_p)
    assert discovery.acquisition_observation_key(gunw_p) == discovery.acquisition_observation_key(gunw_x)
    assert discovery.acquisition_pair_key(gunw_p) != discovery.acquisition_pair_key(goff_p)

def test_duplicate_ur_variant_is_replaced_by_standard_pr_for_same_pair():
    base = (
        "NISAR_L2_{ptype}_GUNW_028_098_A_016_029_4000_SH_"
        "20260819T233918_20260819T233953_20260831T233918_"
        "20260831T233952_P05023_{accuracy}_F_J_001"
    )
    def product(ptype, accuracy):
        name = base.format(ptype=ptype, accuracy=accuracy)
        return {
            "product_type": "GUNW",
            "granule_name": name,
            "reference_date": "2026-08-19",
            "secondary_date": "2026-08-31",
            "maturity": "PROVISIONAL",
            "expected_source_bytes": 100,
        }
    selected, rejected = discovery.collapse_duplicate_variants([
        product("UR", "F"),
        product("PR", "N"),
    ])
    assert len(selected) == 1
    assert selected[0]["granule_name"].split("_")[2] == "PR"
    assert selected[0]["pair_variant_count"] == 2
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "DUPLICATE_ACQUISITION_PAIR_LOWER_VARIANT_PRIORITY"



def test_p_series_release_wins_over_unmapped_x_release_for_same_pair():
    stem = (
        "NISAR_L2_PR_GUNW_024_098_A_016_025_4000_SH_"
        "20260702T233920_20260702T233955_20260714T233920_"
        "20260714T233954_{crid}_N_F_J_001"
    )
    def product(crid):
        return {
            "product_type": "GUNW",
            "granule_name": stem.format(crid=crid),
            "crid": crid,
            "maturity": "PROVISIONAL",
            "maturity_source": "ASF_dataMaturity_filter_plus_collection",
            "reference_date": "2026-07-02",
            "secondary_date": "2026-07-14",
            "expected_source_bytes": 100,
        }
    selected, rejected = discovery.collapse_duplicate_variants([
        product("X05026"), product("P05023")
    ])
    assert len(selected) == 1
    assert selected[0]["crid"] == "P05023"
    assert len(rejected) == 1
    assert rejected[0]["excluded_crid"] == "X05026"

def test_cli_summary_uses_current_pair_count_schema(tmp_path, monkeypatch, capsys):
    manifest = {
        "aoi_wkt_sha256": "wkt-hash",
        "run_profile": "full-aoi-baseline",
        "time_window": {
            "unique_product_family_pair_entries_in_window": 33,
            "unique_acquisition_pairs_across_families": 17,
            "supported_product_variants_after_maturity_policy": 44,
            "products_with_reference_outside_window": 2,
            "boundary_pair_products_included_by_secondary_policy": 2,
        },
        "processing_variant_selection": {"excluded_duplicate_variants": [{} for _ in range(11)]},
        "products": [{
            "id": "GOFF-001",
            "granule_name": "NISAR_L2_PR_GOFF_TEST_PRODUCT",
            "product_type": "GOFF",
            "maturity": "PROVISIONAL",
            "expected_source_bytes": 100,
        }],
        "baseline_product_counts": {"GUNW": 16, "GOFF": 17},
    }
    monkeypatch.setattr(discovery, "discover", lambda _args: manifest)
    output_file = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", [
        "discover_aoi_products.py",
        "--science-root", str(tmp_path),
        "--start-date", "2026-06-17",
        "--end-date", "2026-09-15",
        "--run-profile", "full-aoi-baseline",
        "--retention-profile", "derived-only",
        "--out", str(tmp_path / "manifest.json"),
    ])

    assert discovery.main() == 0
    output = capsys.readouterr().out
    assert "AOI_DISCOVERY_INVENTORY=33" in output
    assert "AOI_UNIQUE_ACQUISITION_PAIRS_ACROSS_FAMILIES=17" in output
    assert "AOI_BASELINE_GUNW=16" in output
    assert "AOI_BASELINE_GOFF=17" in output
    github_output = output_file.read_text(encoding="utf-8")
    assert "product_count=1" in github_output
    assert 'shard_matrix={"include":[{"shard_index":0,"shard_count":1,"shard_number":1}]}' in github_output


def test_processing_shards_cap_each_runner_at_three_products() -> None:
    assert [discovery.processing_shard_count(count) for count in (1, 3, 4, 15, 16, 30, 31)] == [
        1, 1, 2, 5, 6, 10, 11,
    ]
