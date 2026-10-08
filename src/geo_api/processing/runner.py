import asyncio
import json
import logging
import math
import os
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geo_api.config import Settings
from geo_api.processing.errors import ProcessingFailure
from geo_api.schemas import FeatureResult, ProcessingManifest

DIAGNOSTIC_TAIL_BYTES = 64 * 1024
MANIFEST_LIMIT_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessingResult:
    manifest: ProcessingManifest
    features_path: Path


async def run_processor(
    input_path: Path,
    dataset_format: str,
    workspace: Path,
    settings: Settings,
) -> ProcessingResult:
    command = [
        sys.executable,
        "-m",
        "geo_api.processing.worker",
        str(input_path),
        str(workspace),
        dataset_format,
    ]
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(workspace),
        "TMPDIR": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROJ_NETWORK": "OFF",
        "UPLOAD_LIMIT_BYTES": str(settings.upload_limit_bytes),
        "EXPANDED_LIMIT_BYTES": str(settings.expanded_limit_bytes),
        "MAX_ARCHIVE_ENTRIES": str(settings.max_archive_entries),
        "MAX_FEATURES": str(settings.max_features),
        "MAX_COORDINATES": str(settings.max_coordinates),
        "MAX_COORDINATES_PER_FEATURE": str(settings.max_coordinates_per_feature),
        "MAX_GENERATED_COORDINATES_PER_FEATURE": str(
            settings.max_generated_coordinates_per_feature
        ),
        "MAX_GENERATED_COORDINATES_FILE": str(settings.max_generated_coordinates_file),
        "MAX_GENERATED_WORK": str(settings.max_generated_work),
        "MAX_FEATURE_OUTPUT_BYTES": str(settings.max_feature_output_bytes),
        "MAX_FILENAME_BYTES": str(settings.max_filename_bytes),
        "MAX_CHILD_OUTPUT_BYTES": str(settings.max_child_output_bytes),
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProcessingFailure(
            "PROCESSOR_START_FAILED", "The file processor could not start.", status_code=500
        ) from exc

    assert process.stderr is not None
    diagnostics_task = asyncio.create_task(_read_diagnostic_tail(process.stderr))
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=settings.child_timeout_seconds)
        except TimeoutError as exc:
            await _terminate(process)
            raise ProcessingFailure(
                "PROCESSING_TIMEOUT", "The file exceeded the processing time limit."
            ) from exc
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    finally:
        await _terminate(process)
        diagnostic_tail = await diagnostics_task

    if process.returncode != 0:
        return_code = process.returncode
        if return_code == -signal.SIGXCPU:
            raise ProcessingFailure(
                "PROCESSING_CPU_LIMIT",
                "The file exceeded the processor CPU limit.",
                status_code=413,
            )
        if return_code == -signal.SIGXFSZ:
            raise ProcessingFailure(
                "PROCESSING_OUTPUT_LIMIT",
                "The processor exceeded its output limit.",
                status_code=413,
            )
        logger.error(
            "Processor exited unexpectedly with code %s; retained %d diagnostic bytes",
            return_code,
            len(diagnostic_tail),
        )
        raise ProcessingFailure(
            "PROCESSOR_CRASHED",
            "The isolated file processor exited unexpectedly.",
            status_code=500,
            details={"exit_code": return_code},
        )

    manifest_path = workspace / "manifest.json"
    results_path = workspace / "features.jsonl"
    try:
        manifest_bytes = _read_bounded_file(manifest_path, MANIFEST_LIMIT_BYTES)
        manifest = _decode_model(ProcessingManifest, manifest_bytes)
    except ProcessingFailure:
        raise
    except Exception as exc:
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL", "Processor manifest is malformed.", status_code=500
        ) from exc
    if manifest.format != dataset_format:
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL",
            "Processor format did not match the upload.",
            status_code=500,
        )

    if manifest.status == "FAILED":
        if results_path.exists() and _safe_file_size(results_path) != 0:
            raise ProcessingFailure(
                "INVALID_PROCESSOR_PROTOCOL",
                "A failed processor result included unpublished features.",
                status_code=500,
            )
        assert manifest.failure is not None
        failure = manifest.failure
        raise ProcessingFailure(
            str(failure.get("code", "PROCESSING_FAILED"))[:80],
            str(failure.get("message", "The file could not be processed."))[:500],
            status_code=_manifest_failure_status(failure),
            details=failure.get("details") if isinstance(failure.get("details"), dict) else {},
        )

    if not results_path.exists():
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL", "Processor results are missing.", status_code=500
        )
    validation_task = asyncio.create_task(
        asyncio.to_thread(_validate_completed_output, results_path, manifest, settings)
    )
    try:
        await asyncio.shield(validation_task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await validation_task
        raise
    return ProcessingResult(manifest, results_path)


def _validate_completed_output(
    results_path: Path, manifest: ProcessingManifest, settings: Settings
) -> None:
    counts = {status: 0 for status in ("MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "ERROR")}
    feature_count = 0
    for feature in iter_feature_results(results_path, settings):
        if feature.feature_index != feature_count:
            raise ProcessingFailure(
                "INVALID_PROCESSOR_PROTOCOL", "Processor feature order is invalid.", status_code=500
            )
        counts[feature.measurement_status] += 1
        feature_count += 1
    if feature_count != manifest.feature_count:
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL",
            "Processor feature count did not match its manifest.",
            status_code=500,
        )
    expected_counts = {
        "MEASURED": manifest.measured_count,
        "NOT_APPLICABLE": manifest.not_applicable_count,
        "UNSUPPORTED": manifest.unsupported_count,
        "ERROR": manifest.error_count,
    }
    if counts != expected_counts:
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL",
            "Processor status counts did not match its manifest.",
            status_code=500,
        )


async def iter_feature_result_batches(
    path: Path, settings: Settings, batch_size: int = 200
) -> AsyncIterator[list[FeatureResult]]:
    iterator = iter_feature_results(path, settings)
    while True:
        batch_task = asyncio.create_task(asyncio.to_thread(_take_batch, iterator, batch_size))
        try:
            batch = await asyncio.shield(batch_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await batch_task
            raise
        if not batch:
            return
        yield batch


def _take_batch(iterator: Iterator[FeatureResult], batch_size: int) -> list[FeatureResult]:
    batch: list[FeatureResult] = []
    for _ in range(batch_size):
        try:
            batch.append(next(iterator))
        except StopIteration:
            break
    return batch


async def _read_diagnostic_tail(stream: asyncio.StreamReader) -> bytes:
    tail = bytearray()
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > DIAGNOSTIC_TAIL_BYTES:
            del tail[: len(tail) - DIAGNOSTIC_TAIL_BYTES]
    return bytes(tail)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        await process.wait()


def iter_feature_results(path: Path, settings: Settings) -> Iterator[FeatureResult]:
    size = _safe_file_size(path)
    if size > settings.max_child_output_bytes:
        raise ProcessingFailure(
            "PROCESSING_OUTPUT_LIMIT",
            "The processor result exceeds its output limit.",
            status_code=413,
        )
    bytes_read = 0
    feature_count = 0
    with path.open("rb") as source:
        while raw_line := source.readline(settings.max_feature_output_bytes + 1):
            bytes_read += len(raw_line)
            if (
                bytes_read > settings.max_child_output_bytes
                or len(raw_line) > settings.max_feature_output_bytes
            ):
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "A processor result record exceeds its limit.",
                    status_code=500,
                )
            if not raw_line.endswith(b"\n") or raw_line == b"\n":
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "Processor results are truncated or malformed.",
                    status_code=500,
                )
            _check_json_depth(raw_line, 32)
            try:
                raw = json.loads(
                    raw_line, object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
                _assert_json_values(raw)
                feature = FeatureResult.model_validate(raw)
            except Exception as exc:
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "A processor result record is malformed.",
                    status_code=500,
                ) from exc
            feature_count += 1
            if feature_count > settings.max_features:
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "Processor returned too many features.",
                    status_code=500,
                )
            yield feature


def _read_bounded_file(path: Path, maximum: int) -> bytes:
    size = _safe_file_size(path)
    if size > maximum:
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL", "Processor manifest exceeds its limit.", status_code=500
        )
    data = path.read_bytes()
    _check_json_depth(data, 32)
    return data


def _safe_file_size(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise ProcessingFailure(
            "INVALID_PROCESSOR_PROTOCOL", "Processor output is not a regular file.", status_code=500
        )
    return path.stat().st_size


def _decode_model(model: type[Any], data: bytes) -> Any:
    raw = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    _assert_json_values(raw)
    return model.model_validate(raw)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def _check_json_depth(raw: bytes, maximum: int) -> None:
    depth = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
        elif byte == 0x22:
            quoted = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > maximum:
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "Processor JSON nesting exceeds its limit.",
                    status_code=500,
                )
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise ProcessingFailure(
                    "INVALID_PROCESSOR_PROTOCOL",
                    "Processor JSON structure is malformed.",
                    status_code=500,
                )


def _assert_json_values(value: Any) -> None:
    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 1_000_000:
            raise ProcessingFailure(
                "INVALID_PROCESSOR_PROTOCOL",
                "Processor JSON contains too many values.",
                status_code=500,
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise ProcessingFailure(
                "INVALID_PROCESSOR_PROTOCOL",
                "Processor JSON contains a non-finite number.",
                status_code=500,
            )
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _failure_status(code: str) -> int:
    if code.endswith("_LIMIT") or code in {
        "ARCHIVE_TOO_LARGE",
        "TOO_MANY_FEATURES",
        "TOO_MANY_COORDINATES",
    }:
        return 413
    return 422


def _manifest_failure_status(failure: dict[str, Any]) -> int:
    status = failure.get("http_status")
    if isinstance(status, int) and status in {413, 422, 500}:
        return status
    return _failure_status(str(failure.get("code", "")))
