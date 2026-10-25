#!/usr/bin/env python3
"""Run the existing dynamic-AOI product pipeline once inside a persistent batch runner.

This is orchestration only. It invokes the pinned science readers, validation tools and
Drive uploader with the same contracts used by the one-product reusable workflow.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


SCIENCE_COMMIT = "10639a916c9829850775410b3ed7de62e527770d"
PRODUCT_ID_RE = re.compile(r"(?:GUNW|GOFF)-[0-9]{3,5}")
GRANULE_RE = re.compile(r"NISAR_L2_(?:(?:PR|UR|OD)_)?(?:GUNW|GOFF)_[A-Za-z0-9_.-]{10,250}(?:\.h5)?")


class ProductPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Product:
    product_id: str
    granule_name: str
    product_type: str
    maturity: str
    expected_source_bytes: int


def log(message: str) -> None:
    print(message, flush=True)


def _python() -> list[str]:
    return [sys.executable, "-u"]


def _run(command: list[str], *, log_path: Path | None = None) -> None:
    """Stream combined output immediately and optionally retain a diagnostic copy."""
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    sink = log_path.open("a", encoding="utf-8") if log_path else None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            if sink:
                sink.write(line)
                sink.flush()
        status = process.wait()
    finally:
        if sink:
            sink.close()
    if status:
        failed_command = Path(command[2]).name if len(command) > 2 and command[1] == "-u" else Path(command[0]).name
        raise ProductPipelineError(f"Command failed with exit code {status}: {failed_command}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductPipelineError(f"Required JSON is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise ProductPipelineError(f"Required JSON has an invalid shape: {path.name}")
    return value


def _canonical_granule(value: object) -> str:
    name = Path(str(value or "").strip()).name
    return name[:-3] if name.lower().endswith(".h5") else name


def _copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _hdf5_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".h5")


class ProductPipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.product = Product(
            args.product_id,
            args.granule_name,
            args.product_type,
            args.maturity,
            args.expected_source_bytes,
        )
        self.run_root = args.work_root / args.product_id
        self.raw_dir = self.run_root / "raw"
        self.curated = self.run_root / "curated"
        self.archive_dir = self.curated / "archive"
        self.derived_dir = self.curated / "derived"
        self.download_log = self.run_root / "download.log"
        self.raw_path: Path | None = None
        self.archive_path: Path | None = None
        self.retention_manifest: Path | None = None
        self.stats_path: Path | None = None
        self.reuse_raw = False
        self.same_source = False
        self.outcomes = {
            "download": "skipped",
            "aoi_preflight": "skipped",
            "archive": "skipped",
            "reader_equivalence": "skipped",
            "reader": "skipped",
            "derived_validation": "skipped",
            "drive": "skipped",
            "cleanup": "skipped",
        }

    def validate(self) -> None:
        product = self.product
        if not PRODUCT_ID_RE.fullmatch(product.product_id):
            raise ProductPipelineError("Invalid product ID.")
        if product.product_type not in {"GUNW", "GOFF"} or f"_{product.product_type}_" not in product.granule_name:
            raise ProductPipelineError("Product family does not match the granule.")
        if product.maturity not in {"BETA", "PROVISIONAL"}:
            raise ProductPipelineError("Invalid product maturity.")
        if not GRANULE_RE.fullmatch(product.granule_name) or product.expected_source_bytes <= 0:
            raise ProductPipelineError("Invalid product download contract.")
        if self.args.retention_profile not in {"derived-only", "full-lossless"}:
            raise ProductPipelineError("Invalid retention profile.")
        if self.args.execution_label not in {"initial", "retry-1"}:
            raise ProductPipelineError("Invalid execution label.")
        if not self.args.science_root.is_dir():
            raise ProductPipelineError("Pinned science checkout is missing.")
        required = (
            "EARTHDATA_TOKEN",
            "GOOGLE_DRIVE_OAUTH_CLIENT_JSON",
            "GOOGLE_DRIVE_REFRESH_TOKEN",
            "GOOGLE_DRIVE_PARENT_FOLDER_ID",
        )
        if not os.environ.get("EARTHDATA_TOKEN", "").strip() and not (
            os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD")
        ):
            raise ProductPipelineError("Earthdata authentication is not configured.")
        for name in required[1:]:
            if not os.environ.get(name, "").strip():
                raise ProductPipelineError(f"{name} is not configured.")

    def prepare(self) -> None:
        if self.run_root.exists():
            if self.args.execution_label == "initial":
                raise ProductPipelineError(f"Refusing to reuse a non-empty product work directory: {self.product.product_id}")
            existing_raw = _hdf5_files(self.raw_dir)
            if len(existing_raw) == 1 and (self.raw_dir / "download-manifest.json").is_file():
                self.reuse_raw = True
                shutil.rmtree(self.curated, ignore_errors=True)
                self.download_log.unlink(missing_ok=True)
            else:
                # A failed downloader leaves no accepted raw HDF5. Its staging area is
                # already removed by the downloader, so a clean retry is safe.
                if existing_raw:
                    raise ProductPipelineError("Retry work directory contains an unverified raw product.")
                shutil.rmtree(self.run_root)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.derived_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.run_root, 0o700)
            os.chmod(self.raw_dir, 0o700)
            os.chmod(self.curated, 0o700)
        except OSError:
            pass
        log(
            f"PRODUCT_PROCESSING_START product_id={self.product.product_id} "
            f"family={self.product.product_type} execution={self.args.execution_label}"
        )

    def compare_prior_source(self) -> None:
        prior = self.args.prior_checkpoint
        if prior is None:
            return
        prior_manifest = prior / "download-manifest.json"
        prior_retention = prior / "drive-retention-receipt.json"
        prior_metadata = prior / "product-processing.json"
        if not prior_manifest.is_file():
            if prior_retention.is_file() or prior_metadata.is_file():
                raise ProductPipelineError("Prior retry evidence has retention metadata without a source manifest.")
            log("RETRY_SOURCE_IDENTITY=NO_PRIOR_MANIFEST_NO_PRIOR_RETENTION")
            return
        current = _read_json(self.raw_dir / "download-manifest.json")
        previous = _read_json(prior_manifest)
        old_sha = str(previous.get("source_sha256", "")).lower()
        new_sha = str(current.get("source_sha256", "")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", old_sha) or not re.fullmatch(r"[a-f0-9]{64}", new_sha):
            raise ProductPipelineError("Retry source SHA-256 is missing or invalid.")
        if old_sha != new_sha:
            raise ProductPipelineError("Retry source bytes changed.")
        self.same_source = True
        log("RETRY_SOURCE_IDENTITY=PASS_SOURCE_SHA256_MATCH")

    def download(self) -> None:
        product = self.product
        self.download_log.write_text("", encoding="utf-8")
        if self.reuse_raw:
            candidates = _hdf5_files(self.raw_dir)
            manifest = _read_json(self.raw_dir / "download-manifest.json")
            if len(candidates) != 1:
                raise ProductPipelineError("Retry did not retain exactly one raw product.")
            raw = candidates[0]
            expected_sha = str(manifest.get("source_sha256", "")).lower()
            if (
                _canonical_granule(raw.name) != _canonical_granule(product.granule_name)
                or raw.stat().st_size != manifest.get("source_bytes")
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)
                or _sha256(raw) != expected_sha
            ):
                raise ProductPipelineError("Retained retry source does not match its verified download manifest.")
            self.raw_path = raw
            self.same_source = True
            self.outcomes["download"] = "success"
            log(f"PRODUCT_MILESTONE=download_reused product_id={product.product_id} family={product.product_type} status=PASS")
            return
        log(f"DOWNLOAD_ATTEMPT=1/1")
        log(f"PRODUCT_MILESTONE=download_started product_id={product.product_id} family={product.product_type} attempt=1/1")
        try:
            _run(
                _python() + [
                    "tools/download_nisar_product.py",
                    "--product-type", product.product_type,
                    "--maturity", product.maturity,
                    "--granule-name", product.granule_name,
                    "--science-root", str(self.args.science_root),
                    "--aoi", self.args.aoi_name,
                    "--expected-source-bytes", str(product.expected_source_bytes),
                    "--out-dir", str(self.raw_dir),
                ],
                log_path=self.download_log,
            )
        except ProductPipelineError:
            self.outcomes["download"] = "failure"
            raise
        candidates = _hdf5_files(self.raw_dir)
        if len(candidates) != 1 or not (self.raw_dir / "download-manifest.json").is_file():
            self.outcomes["download"] = "failure"
            raise ProductPipelineError("The downloader did not produce one verified raw product and manifest.")
        self.raw_path = candidates[0]
        self.compare_prior_source()
        self.outcomes["download"] = "success"
        log(f"PRODUCT_MILESTONE=download_completed product_id={product.product_id} family={product.product_type}")

    def validate_aoi(self) -> None:
        assert self.raw_path is not None
        product = self.product
        log(f"PRODUCT_MILESTONE=aoi_validation_started product_id={product.product_id} family={product.product_type}")
        try:
            _run(_python() + [
                "tools/validate_product_aoi.py",
                "--input", str(self.raw_path),
                "--product-type", product.product_type,
                "--aoi", self.args.aoi_name,
                "--science-root", str(self.args.science_root),
                "--science-commit", SCIENCE_COMMIT,
                "--out", str(self.curated / "preflight-report.json"),
            ])
        except ProductPipelineError:
            self.outcomes["aoi_preflight"] = "failure"
            raise
        self.outcomes["aoi_preflight"] = "success"
        log(f"PRODUCT_MILESTONE=aoi_validation_completed product_id={product.product_id} family={product.product_type} status=PASS")

    def inventory_and_archive(self) -> None:
        assert self.raw_path is not None
        product = self.product
        _run(_python() + [
            "tools/sarguardian_hdf5_archive.py", "inventory",
            "--input", str(self.raw_path),
            "--product-type", product.product_type,
            "--out", str(self.curated / "source-inventory.json"),
        ])
        if self.args.retention_profile == "derived-only":
            self.outcomes["archive"] = "skipped"
            self.outcomes["reader_equivalence"] = "skipped"
            return
        stem = self.raw_path.stem
        self.archive_path = self.archive_dir / f"{stem}.science.h5"
        self.retention_manifest = self.curated / f"{stem}.retention-manifest.json"
        try:
            _run(_python() + [
                "tools/sarguardian_hdf5_archive.py", "archive",
                "--input", str(self.raw_path),
                "--out", str(self.archive_path),
                "--manifest", str(self.retention_manifest),
                "--product-type", product.product_type,
                "--retention-profile", "full-lossless",
            ])
            self.outcomes["archive"] = "success"
            _run(_python() + [
                "tools/verify_reader_equivalence.py",
                "--product-type", product.product_type,
                "--source", str(self.raw_path),
                "--archive", str(self.archive_path),
                "--science-root", str(self.args.science_root),
                "--science-commit", SCIENCE_COMMIT,
                "--aoi", self.args.aoi_name,
                "--preflight-report", str(self.curated / "preflight-report.json"),
                "--out", str(self.curated / "reader-equivalence.json"),
            ])
            equivalence = _read_json(self.curated / "reader-equivalence.json")
            if equivalence.get("status") not in {"PASS", "PASS_NO_VALID_AOI_PIXELS"}:
                raise ProductPipelineError("Reader equivalence did not pass.")
            self.outcomes["reader_equivalence"] = "success"
            log(f"READER_EQUIVALENCE_STATUS={equivalence['status']}")
        except ProductPipelineError:
            if self.outcomes["archive"] != "success":
                self.outcomes["archive"] = "failure"
            else:
                self.outcomes["reader_equivalence"] = "failure"
            raise

    def run_reader(self) -> None:
        assert self.raw_path is not None
        product = self.product
        log(f"PRODUCT_MILESTONE=science_reader_started product_id={product.product_id} family={product.product_type}")
        if product.product_type == "GUNW":
            self.stats_path = self.derived_dir / "gunw_stats_custom.csv"
            export_dir = self.derived_dir / "export_custom"
            command = _python() + [
                str(self.args.science_root / "src" / "gunw_reader.py"),
                "--read", str(self.raw_path), "--aoi", self.args.aoi_name,
                "--auto-ref", "--csv", str(self.stats_path), "--export", str(export_dir),
            ]
        else:
            self.stats_path = self.derived_dir / "goff_stats_custom.csv"
            export_dir = self.derived_dir / "export_goff_custom"
            command = _python() + [
                str(self.args.science_root / "src" / "goff_reader.py"),
                "--read", str(self.raw_path), "--aoi", self.args.aoi_name,
                "--layer", "layer2", "--csv", str(self.stats_path), "--export", str(export_dir),
            ]
        export_dir.mkdir(parents=True, exist_ok=True)
        try:
            _run(command)
        except ProductPipelineError:
            self.outcomes["reader"] = "failure"
            raise
        if not self.stats_path.is_file() or self.stats_path.stat().st_size <= 0:
            self.outcomes["reader"] = "failure"
            raise ProductPipelineError("Science reader did not produce its statistics CSV.")
        self.outcomes["reader"] = "success"
        log(f"PRODUCT_MILESTONE=science_reader_completed product_id={product.product_id} family={product.product_type} status=PASS")

    def validate_derived(self) -> None:
        assert self.stats_path is not None
        product = self.product
        export_dir = self.derived_dir / ("export_custom" if product.product_type == "GUNW" else "export_goff_custom")
        log(f"PRODUCT_MILESTONE=derived_validation_started product_id={product.product_id} family={product.product_type}")
        try:
            _run(_python() + [
                "tools/validate_aoi_derived_outputs.py",
                "--product-id", product.product_id,
                "--product-type", product.product_type,
                "--granule-name", product.granule_name,
                "--aoi-name", self.args.aoi_name,
                "--stats-csv", str(self.stats_path),
                "--export-dir", str(export_dir),
                "--out", str(self.curated / "derived-output-validation.json"),
            ])
        except ProductPipelineError:
            self.outcomes["derived_validation"] = "failure"
            raise
        self.outcomes["derived_validation"] = "success"
        log(f"PRODUCT_MILESTONE=derived_validation_completed product_id={product.product_id} family={product.product_type} status=PASS")

    def build_metadata(self) -> None:
        assert self.stats_path is not None and self.raw_path is not None
        shutil.copy2(self.raw_dir / "download-manifest.json", self.curated / "download-manifest.json")
        product = self.product
        with self.stats_path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            fields = reader.fieldnames or []
        expected = _canonical_granule(product.granule_name)
        if not rows or "file" not in fields or any(_canonical_granule(row.get("file")) != expected for row in rows):
            raise ProductPipelineError("Reader output is empty or belongs to another granule.")
        if product.product_type == "GUNW" and len(rows) != 1:
            raise ProductPipelineError("GUNW reader must produce one row.")
        if product.product_type == "GOFF" and (len(rows) != 1 or "layer2" not in rows[0].get("layer", "").lower()):
            raise ProductPipelineError("GOFF reader must produce one layer2 row.")
        validation = _read_json(self.curated / "derived-output-validation.json")
        if validation.get("status") != "PASS":
            raise ProductPipelineError("Derived output validation did not pass.")
        equivalence_path = self.curated / "reader-equivalence.json"
        equivalence = _read_json(equivalence_path) if equivalence_path.is_file() else None
        if self.args.retention_profile == "full-lossless":
            if not self.archive_path or not self.archive_path.is_file() or not equivalence or equivalence.get("status") not in {"PASS", "PASS_NO_VALID_AOI_PIXELS"}:
                raise ProductPipelineError("Full-lossless requirements were not met.")
        elif self.archive_path is not None:
            raise ProductPipelineError("Derived-only processing created an archive unexpectedly.")
        inventory = _read_json(self.curated / "source-inventory.json")
        download = _read_json(self.curated / "download-manifest.json")
        export_dir = self.derived_dir / ("export_custom" if product.product_type == "GUNW" else "export_goff_custom")
        exports = [
            path.relative_to(self.curated).as_posix()
            for path in sorted(export_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
        ]
        if not exports:
            raise ProductPipelineError("Science reader did not export any GeoTIFF.")
        metadata = {
            "schema_version": "sarguardian-stream-product/1.1",
            "id": product.product_id,
            "product_type": product.product_type,
            "maturity": product.maturity,
            "granule_name": expected,
            "aoi": self.args.aoi_name,
            "science_commit": SCIENCE_COMMIT,
            "orchestration_branch": "main",
            "orchestration_sha": self.args.orchestration_sha,
            "retention_profile": self.args.retention_profile,
            "source_file_sha256": download["source_sha256"],
            "source_file_bytes": download["source_bytes"],
            "retained_archive": self.archive_path.relative_to(self.curated).as_posix() if self.archive_path else None,
            "retained_archive_sha256": _sha256(self.archive_path) if self.archive_path else None,
            "stats_csv": self.stats_path.relative_to(self.curated).as_posix(),
            "stats_rows": len(rows),
            "export_dir": export_dir.relative_to(self.curated).as_posix(),
            "export_files": exports,
            "derived_output_validation_status": validation["status"],
            "derived_output_validation": "derived-output-validation.json",
            "reader_equivalence_status": equivalence["status"] if equivalence else "NOT_RUN_DERIVED_ONLY",
            "reader_equivalence_report": "reader-equivalence.json" if equivalence else None,
            "source_dataset_inventory": "source-inventory.json",
            "preflight_report": "preflight-report.json",
            "download_manifest": "download-manifest.json",
            "drive_remote_verification": "PENDING",
            "local_raw_source_deleted_at_manifest_creation": False,
            "cleanup_receipt": "local-cleanup-receipt.json",
            "source_dataset_inventory_count": len(inventory.get("datasets", [])),
            "excluded_dataset_count": 0 if self.args.retention_profile == "full-lossless" else None,
            "future_reprocessing_note": (
                "Full-lossless HDF5 archive retains all source datasets and attributes."
                if self.args.retention_profile == "full-lossless"
                else "Derived-only: no HDF5 archive is retained; only validated outputs and provenance are preserved."
            ),
        }
        (self.curated / "product-processing.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def _uploader(self, files: Iterable[Path], output: Path) -> None:
        product = self.product
        retry_flags: list[str] = []
        if self.args.execution_label != "initial":
            retry_flags.append("--existing-files-are-new")
        if self.same_source:
            retry_flags.append("--replace-existing-retry-sidecars")
        command = _python() + [
            "tools/drive_resumable_upload.py", *retry_flags,
            "--root", str(self.curated),
            "--product-type", product.product_type,
            "--maturity", product.maturity,
            "--granule-name", product.granule_name,
            "--version-tag", self.args.drive_version_tag,
            "--job-id", self.args.job_id,
            "--files", *[str(path) for path in files],
            "--out", str(output),
        ]
        _run(command)

    def upload(self) -> None:
        product = self.product
        log(f"PRODUCT_MILESTONE=drive_upload_started product_id={product.product_id} family={product.product_type}")
        derived_files = sorted(path for path in self.derived_dir.rglob("*") if path.is_file())
        files = [
            self.curated / "source-inventory.json",
            self.curated / "preflight-report.json",
            self.curated / "derived-output-validation.json",
            self.curated / "download-manifest.json",
            *derived_files,
        ]
        if self.args.retention_profile == "full-lossless":
            if not self.archive_path or not self.retention_manifest:
                raise ProductPipelineError("Full-lossless files are missing before upload.")
            files.extend((self.archive_path, self.retention_manifest, self.curated / "reader-equivalence.json"))
        elif self.archive_path or (self.curated / "reader-equivalence.json").exists():
            raise ProductPipelineError("Unexpected archive artifact in derived-only mode.")
        try:
            retention_path = self.curated / "drive-retention-receipt.json"
            self._uploader(files, retention_path)
            receipt = _read_json(retention_path)
            metadata_path = self.curated / "product-processing.json"
            metadata = _read_json(metadata_path)
            if receipt.get("remote_verification") != "PASS":
                raise ProductPipelineError("Drive outputs were not remotely verified.")
            uploaded = {str(item.get("name", "")) for item in receipt.get("files", []) if isinstance(item, dict)}
            required = {"source-inventory.json", "preflight-report.json", "derived-output-validation.json", "download-manifest.json"}
            required.add(Path(metadata["stats_csv"]).name)
            required.update(Path(relative).name for relative in metadata["export_files"])
            missing = sorted(required - uploaded)
            if missing:
                raise ProductPipelineError("Drive output receipt is missing: " + ",".join(missing))
            archives = [name for name in uploaded if name.endswith(".science.h5")]
            if self.args.retention_profile == "full-lossless" and len(archives) != 1:
                raise ProductPipelineError("Full-lossless archive is missing from the Drive receipt.")
            if self.args.retention_profile == "derived-only" and archives:
                raise ProductPipelineError("Derived-only Drive receipt contains an archive.")
            metadata["drive_remote_verification"] = "PASS"
            metadata["drive_remote_folder_id"] = receipt.get("remote_folder_id")
            metadata["drive_retention_receipt"] = retention_path.name
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            metadata_receipt = self.curated / "drive-metadata-upload-receipt.json"
            self._uploader((metadata_path, retention_path), metadata_receipt)
            metadata_upload = _read_json(metadata_receipt)
            names = {str(item.get("name", "")) for item in metadata_upload.get("files", []) if isinstance(item, dict)}
            if metadata_upload.get("remote_verification") != "PASS" or not {
                "product-processing.json", "drive-retention-receipt.json"
            }.issubset(names):
                raise ProductPipelineError("Drive metadata was not remotely verified.")
        except ProductPipelineError:
            self.outcomes["drive"] = "failure"
            raise
        self.outcomes["drive"] = "success"
        log(f"DRIVE_VALIDATED_OUTPUTS_VERIFIED={len(metadata_upload.get('files', []))}")
        log(f"PRODUCT_MILESTONE=drive_upload_completed product_id={product.product_id} family={product.product_type} status=PASS")

    def write_diagnostic(self) -> None:
        prefixes = (
            "ERROR=", "DIRECT_PRODUCT_DIAGNOSTIC=", "SOURCE_ACQUISITION_MODULE=",
            "DISCOVERY_FALLBACK=", "DIRECT_PRODUCT_HTTP_404=", "NETWORK_RETRY=",
            "DOWNLOAD_FAILURE_RETRYABLE=",
        )
        lines: list[str] = []
        if self.download_log.is_file():
            for raw_line in self.download_log.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if line.startswith(prefixes):
                    lines.append(line[:420])
        record = {
            "schema_version": "sarguardian-product-run-diagnostic/1.0",
            "product_id": self.product.product_id,
            "product_type": self.product.product_type,
            "granule_name": self.product.granule_name,
            "aoi": self.args.aoi_name,
            "science_commit": SCIENCE_COMMIT,
            "execution_label": self.args.execution_label,
            "step_outcomes": self.outcomes,
            "download_error_code": next((line for line in lines if line.startswith("ERROR=")), None),
            "download_diagnostics": lines[:16],
            "download_manifest_created": (self.curated / "download-manifest.json").is_file(),
            "raw_hdf5_present_at_diagnostic_time": bool(_hdf5_files(self.raw_dir)),
            "retained_archive_created": any(self.archive_dir.glob("*.science.h5")),
            "drive_receipt_created": (self.curated / "drive-retention-receipt.json").is_file(),
            "local_raw_delete_step_is_gated_on_success": True,
        }
        self.curated.mkdir(parents=True, exist_ok=True)
        (self.curated / "product-run-diagnostics.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log(f"PRODUCT_RUN_DIAGNOSTICS_WRITTEN={self.curated / 'product-run-diagnostics.json'}")

    def checkpoint_product(self) -> None:
        destination = self.args.checkpoint_root / "products" / f"sarguardian-granule-{self.product.product_id}"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        names = (
            "product-run-diagnostics.json", "product-processing.json", "reader-equivalence.json",
            "derived-output-validation.json", "source-inventory.json", "preflight-report.json",
            "download-manifest.json", "drive-retention-receipt.json", "drive-metadata-upload-receipt.json",
        )
        for name in names:
            _copy_if_present(self.curated / name, destination / name)
        for manifest in self.curated.glob("*retention-manifest.json"):
            _copy_if_present(manifest, destination / manifest.name)
        if self.derived_dir.is_dir():
            shutil.copytree(self.derived_dir, destination / "derived", dirs_exist_ok=True)

    def cleanup(self) -> None:
        assert self.raw_path is not None
        product = self.product
        log(f"PRODUCT_MILESTONE=cleanup_started product_id={product.product_id} family={product.product_type}")
        try:
            metadata = _read_json(self.curated / "product-processing.json")
            retention = _read_json(self.curated / "drive-retention-receipt.json")
            validation = _read_json(self.curated / "derived-output-validation.json")
            if validation.get("status") != "PASS" or metadata.get("derived_output_validation_status") != "PASS":
                raise ProductPipelineError("Refusing raw deletion without derived validation.")
            if metadata.get("drive_remote_verification") != "PASS" or retention.get("remote_verification") != "PASS":
                raise ProductPipelineError("Refusing raw deletion without remote verification.")
            if metadata.get("drive_remote_folder_id") != retention.get("remote_folder_id"):
                raise ProductPipelineError("Drive retention folder identity mismatch.")
            names = {str(item.get("name", "")) for item in retention.get("files", []) if isinstance(item, dict)}
            required = {"source-inventory.json", "preflight-report.json", "derived-output-validation.json", "download-manifest.json"}
            required.add(Path(metadata["stats_csv"]).name)
            required.update(Path(relative).name for relative in metadata["export_files"])
            if required - names:
                raise ProductPipelineError("Refusing raw deletion because required remote outputs are missing.")
            archives = [item for item in retention.get("files", []) if str(item.get("name", "")).endswith(".science.h5")]
            if self.args.retention_profile == "full-lossless":
                equivalence = _read_json(self.curated / "reader-equivalence.json")
                if len(archives) != 1 or not self.archive_path or equivalence.get("status") not in {"PASS", "PASS_NO_VALID_AOI_PIXELS"}:
                    raise ProductPipelineError("Full-lossless cleanup gate failed.")
            elif archives or self.archive_path:
                raise ProductPipelineError("Derived-only cleanup gate found an archive.")
            self.raw_path.unlink()
            if self.archive_path:
                self.archive_path.unlink()
            if self.raw_path.exists() or (self.archive_path and self.archive_path.exists()):
                raise ProductPipelineError("Local raw/archive deletion did not complete.")
            cleanup = {
                "schema_version": "sarguardian-local-cleanup-receipt/1.1",
                "created_utc": datetime.now(UTC).isoformat(),
                "product_id": product.product_id,
                "product_type": product.product_type,
                "maturity": product.maturity,
                "granule_name": product.granule_name,
                "aoi": self.args.aoi_name,
                "science_commit": SCIENCE_COMMIT,
                "retention_profile": self.args.retention_profile,
                "source_file_sha256": _read_json(self.curated / "download-manifest.json")["source_sha256"],
                "retained_archive_name": archives[0]["name"] if archives else None,
                "retained_archive_sha256": metadata.get("retained_archive_sha256"),
                "remote_retention_verification": "PASS",
                "remote_retention_folder_id": retention["remote_folder_id"],
                "runner_raw_hdf5_deleted": True,
                "runner_archive_deleted": True,
                "runner_paths_stored": False,
                "cleanup_scope": (
                    "temporary runner raw and archive deleted; remote full-lossless archive remains retained"
                    if self.args.retention_profile == "full-lossless"
                    else "temporary runner raw deleted after verified derived-only outputs; no HDF5 archive retained"
                ),
            }
            cleanup_path = self.curated / "local-cleanup-receipt.json"
            cleanup_path.write_text(json.dumps(cleanup, indent=2), encoding="utf-8")
            log("LOCAL_RAW_AND_POLICY_REQUIRED_ARCHIVE_COPIES_DELETED_AFTER_REMOTE_VERIFICATION=yes")
            cleanup_upload_path = self.curated / "drive-cleanup-upload-receipt.json"
            self._uploader((cleanup_path,), cleanup_upload_path)
            cleanup_upload = _read_json(cleanup_upload_path)
            if cleanup_upload.get("remote_verification") != "PASS" or not any(
                item.get("name") == cleanup_path.name for item in cleanup_upload.get("files", []) if isinstance(item, dict)
            ):
                raise ProductPipelineError("Local cleanup receipt was not remotely verified.")
            cleanup_destination = self.args.checkpoint_root / "cleanups" / f"sarguardian-cleanup-{product.product_id}"
            if cleanup_destination.exists():
                shutil.rmtree(cleanup_destination)
            cleanup_destination.mkdir(parents=True)
            shutil.copy2(cleanup_path, cleanup_destination / cleanup_path.name)
            shutil.copy2(cleanup_upload_path, cleanup_destination / cleanup_upload_path.name)
        except ProductPipelineError:
            self.outcomes["cleanup"] = "failure"
            raise
        self.outcomes["cleanup"] = "success"
        log("LOCAL_CLEANUP_RECEIPT_DRIVE_VERIFIED=PASS")
        log(f"PRODUCT_MILESTONE=cleanup_completed product_id={product.product_id} family={product.product_type} status=PASS")

    def run(self) -> int:
        try:
            self.validate()
            self.prepare()
            self.download()
            self.validate_aoi()
            self.inventory_and_archive()
            self.run_reader()
            self.validate_derived()
            self.build_metadata()
            self.upload()
            self.write_diagnostic()
            self.checkpoint_product()
            self.cleanup()
            self.write_diagnostic()
            self.checkpoint_product()
        except Exception as exc:  # noqa: BLE001 - always preserve bounded product diagnostics
            log(f"PRODUCT_ERROR product_id={self.product.product_id} type={type(exc).__name__} detail={str(exc)[:300]}")
            try:
                self.write_diagnostic()
                self.checkpoint_product()
            except Exception as diagnostic_error:  # noqa: BLE001
                log(f"PRODUCT_DIAGNOSTIC_ERROR product_id={self.product.product_id} type={type(diagnostic_error).__name__}")
            log(
                f"PRODUCT_PROCESSING_STATUS=failed product_id={self.product.product_id} "
                f"family={self.product.product_type} execution={self.args.execution_label} "
                + " ".join(f"{name}={value}" for name, value in self.outcomes.items())
            )
            return 1
        log(
            f"PRODUCT_PROCESSING_STATUS=completed product_id={self.product.product_id} "
            f"family={self.product.product_type} execution={self.args.execution_label} "
            + " ".join(f"{name}={value}" for name, value in self.outcomes.items())
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--granule-name", required=True)
    parser.add_argument("--product-type", required=True, choices=("GUNW", "GOFF"))
    parser.add_argument("--maturity", required=True, choices=("BETA", "PROVISIONAL"))
    parser.add_argument("--expected-source-bytes", required=True, type=int)
    parser.add_argument("--retention-profile", required=True, choices=("derived-only", "full-lossless"))
    parser.add_argument("--job-id", default="")
    parser.add_argument("--drive-version-tag", required=True)
    parser.add_argument("--orchestration-sha", required=True)
    parser.add_argument("--science-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--checkpoint-root", required=True, type=Path)
    parser.add_argument("--aoi-name", default="custom")
    parser.add_argument("--execution-label", default="initial", choices=("initial", "retry-1"))
    parser.add_argument("--prior-checkpoint", type=Path)
    return parser.parse_args()


def main() -> int:
    return ProductPipeline(parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
