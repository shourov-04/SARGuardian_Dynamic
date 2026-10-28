from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch_aoi_rechecks as watcher


def test_safe_extract_rejects_paths_outside_destination(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.json", "{}")

    with pytest.raises(ValueError, match="unsafe path"):
        watcher.safe_extract(archive, tmp_path / "out")


def test_merge_keeps_primary_products_and_overlays_rechecked_product(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    rechecks = tmp_path / "rechecks"
    consolidated = tmp_path / "consolidated"
    first = primary / "products" / "sarguardian-granule-GUNW-001"
    failed = primary / "products" / "sarguardian-granule-GOFF-027"
    recovered = rechecks / "products" / "sarguardian-granule-GOFF-027"
    first.mkdir(parents=True)
    failed.mkdir(parents=True)
    recovered.mkdir(parents=True)
    (first / "product-processing.json").write_text("primary-pass", encoding="utf-8")
    (failed / "product-processing.json").write_text("primary-fail", encoding="utf-8")
    (recovered / "product-processing.json").write_text("recheck-pass", encoding="utf-8")

    watcher.merge_checkpoints(primary, rechecks, consolidated)

    assert (
        consolidated / "products" / "sarguardian-granule-GUNW-001" / "product-processing.json"
    ).read_text(encoding="utf-8") == "primary-pass"
    assert (
        consolidated / "products" / "sarguardian-granule-GOFF-027" / "product-processing.json"
    ).read_text(encoding="utf-8") == "recheck-pass"
