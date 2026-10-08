import json
import os
import sys
from pathlib import Path
from typing import Any

from geo_api.config import Settings
from geo_api.processing.errors import ProcessingFailure
from geo_api.schemas import FeatureResult, ProcessingManifest


def _apply_resource_limits() -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    memory = 1024 * 1024 * 1024
    output = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))


def _manifest_failure(dataset_format: str, failure: ProcessingFailure) -> ProcessingManifest:
    return ProcessingManifest(
        protocol_version=1,
        format=dataset_format,
        status="FAILED",
        failure={
            "code": failure.code[:80],
            "message": failure.message[:500],
            "http_status": failure.status_code,
            "details": _bounded_details(failure.details),
        },
    )


def _bounded_details(details: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.dumps(details, ensure_ascii=False, allow_nan=False, default=str)
    except (TypeError, ValueError):
        return {"omitted": True}
    if len(raw.encode("utf-8")) > 8 * 1024:
        return {"omitted": True}
    return details


def _write_manifest(workspace: Path, manifest: ProcessingManifest) -> None:
    temporary = workspace / "manifest.json.tmp"
    final = workspace / "manifest.json"
    payload = json.dumps(
        manifest.model_dump(mode="json"), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    with temporary.open("x", encoding="utf-8") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(final)


def _write_features(workspace: Path, features: list[FeatureResult], settings: Settings) -> None:
    temporary = workspace / "features.jsonl.tmp"
    final = workspace / "features.jsonl"
    total_bytes = 0
    final_coordinates = 0
    cumulative_work = 0
    with temporary.open("x", encoding="utf-8", newline="\n") as target:
        for expected_index, feature in enumerate(features):
            if feature.feature_index != expected_index:
                raise ProcessingFailure("PROCESSOR_PROTOCOL_ERROR", "Feature order is invalid.")
            provenance = feature.provenance or {}
            generated_final = provenance.get("generated_coordinates_final", 0)
            generated_work = provenance.get("generated_coordinates_total", 0)
            if isinstance(generated_final, int) and generated_final >= 0:
                final_coordinates += generated_final
            if isinstance(generated_work, int) and generated_work >= 0:
                cumulative_work += generated_work
            if final_coordinates > settings.max_generated_coordinates_file:
                raise ProcessingFailure(
                    "FILE_GENERATED_COORDINATE_LIMIT",
                    "The dataset exceeds the generated-coordinate limit.",
                    status_code=413,
                )
            if cumulative_work > settings.max_generated_work:
                raise ProcessingFailure(
                    "FILE_GENERATED_WORK_LIMIT",
                    "The dataset exceeds the cumulative measurement-work limit.",
                    status_code=413,
                )
            raw_line = (
                json.dumps(
                    feature.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            if len(raw_line) > settings.max_feature_output_bytes:
                raise ProcessingFailure(
                    "FEATURE_OUTPUT_LIMIT",
                    "A feature result exceeds the serialized output limit.",
                    status_code=413,
                )
            total_bytes += len(raw_line)
            if total_bytes > settings.max_child_output_bytes:
                raise ProcessingFailure(
                    "FILE_OUTPUT_LIMIT",
                    "The dataset result exceeds the serialized output limit.",
                    status_code=413,
                )
            target.write(raw_line.decode("utf-8"))
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(final)


def run(input_path: Path, workspace: Path, dataset_format: str) -> int:
    _apply_resource_limits()
    if dataset_format not in {"KML", "SHAPEFILE"}:
        return 2
    try:
        settings = Settings(_env_file=None)
        if workspace.is_symlink():
            raise ProcessingFailure(
                "INVALID_PROCESSOR_INPUT", "The processor workspace is invalid."
            )
        root = workspace.resolve(strict=True)
        source = input_path.resolve(strict=True)
        if source.parent != root or source.name != "source.data" or input_path.is_symlink():
            raise ProcessingFailure(
                "INVALID_PROCESSOR_INPUT", "The processor input path is invalid."
            )
        if not source.is_file() or source.stat().st_size > settings.upload_limit_bytes:
            raise ProcessingFailure(
                "UPLOAD_SIZE_LIMIT", "The uploaded file exceeds its size limit.", status_code=413
            )
        from geo_api.processing.readers import process_input

        manifest, features = process_input(source, dataset_format, root, settings)
        if manifest.status == "FAILED":
            if features:
                raise ProcessingFailure(
                    "PROCESSOR_PROTOCOL_ERROR", "Failed processing returned features."
                )
            _write_manifest(root, manifest)
            return 0
        for feature in features:
            if feature.measurement_status == "MEASURED" and feature.provenance is not None:
                feature.provenance.setdefault("method", "projected_geodesic_densification")
                feature.provenance.setdefault("measurement_policy", manifest.measurement_policy)
                feature.provenance.setdefault("dimension", 2)
        if manifest.feature_count != len(features):
            raise ProcessingFailure(
                "PROCESSOR_PROTOCOL_ERROR", "Manifest feature count is inconsistent."
            )
        _write_features(root, features, settings)
        _write_manifest(root, manifest)
        return 0
    except ProcessingFailure as failure:
        for filename in ("features.jsonl.tmp", "features.jsonl"):
            path = workspace / filename
            if path.exists() and not path.is_symlink():
                path.unlink()
        _write_manifest(workspace, _manifest_failure(dataset_format, failure))
        return 0
    except MemoryError:
        for filename in ("features.jsonl.tmp", "features.jsonl"):
            path = workspace / filename
            if path.exists() and not path.is_symlink():
                path.unlink()
        resource_failure = ProcessingFailure(
            "PROCESSING_MEMORY_LIMIT",
            "The file exceeded the processor memory limit.",
            status_code=413,
        )
        _write_manifest(workspace, _manifest_failure(dataset_format, resource_failure))
        return 0
    except Exception:
        for filename in ("features.jsonl.tmp", "features.jsonl"):
            path = workspace / filename
            if path.exists() and not path.is_symlink():
                path.unlink()
        internal_failure = ProcessingFailure(
            "PROCESSOR_INTERNAL_ERROR",
            "The isolated processor could not complete the file.",
            status_code=500,
        )
        _write_manifest(workspace, _manifest_failure(dataset_format, internal_failure))
        return 0


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    return run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])


if __name__ == "__main__":
    raise SystemExit(main())
