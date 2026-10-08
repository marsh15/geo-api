import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from geo_api.api.errors import GeoAPIError
from geo_api.config import Settings
from geo_api.db.models import FeatureRecord, FileRecord
from geo_api.db.session import check_database, get_session
from geo_api.middleware.admission import BodyLimitExceeded, UploadReceiveTimeout
from geo_api.processing.errors import ProcessingFailure
from geo_api.processing.runner import iter_feature_result_batches, run_processor
from geo_api.processing.storage import create_workspace, remove_workspace
from geo_api.schemas import CRSInfo, FileSummary

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/api/files/",
    status_code=201,
    response_model=FileSummary,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "additionalProperties": False,
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
    responses={
        400: {"description": "Malformed multipart request"},
        413: {"description": "Upload or processing resource limit exceeded"},
        415: {"description": "Unsupported filename format"},
        422: {"description": "Invalid dataset"},
        503: {"description": "Service is busy or storage is unavailable"},
    },
)
async def upload_file(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> FileSummary:
    # This endpoint parses multipart data manually so the admission middleware runs first.
    settings: Settings = request.app.state.settings
    try:
        form = await request.form(max_files=2, max_fields=8, max_part_size=64 * 1024)
    except BodyLimitExceeded as exc:
        raise GeoAPIError(
            413,
            "REQUEST_BODY_TOO_LARGE",
            "The complete multipart request exceeds the configured body limit.",
        ) from exc
    except UploadReceiveTimeout as exc:
        raise GeoAPIError(
            408, "UPLOAD_TIMEOUT", "The upload request exceeded its time limit."
        ) from exc
    except Exception as exc:
        raise GeoAPIError(
            400, "MALFORMED_MULTIPART", "The multipart request could not be parsed."
        ) from exc
    try:
        upload = _get_single_upload(form)
        filename = _validate_filename(upload.filename, settings)
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".zip", ".kml"}:
            raise GeoAPIError(415, "UNSUPPORTED_FILE_EXTENSION", "Upload a .zip or .kml file.")
        dataset_format = "KML" if suffix == ".kml" else "SHAPEFILE"

        workspace = create_workspace(request.app.state.temp_root)
        try:
            upload_size, checksum = await _copy_upload(upload, workspace / "source.data", settings)
            try:
                await asyncio.wait_for(check_database(), timeout=4)
            except Exception as exc:
                raise GeoAPIError(503, "DATABASE_UNAVAILABLE", "PostgreSQL is not ready.") from exc

            file_id = uuid4()
            started = time.monotonic()
            try:
                result = await run_processor(
                    workspace / "source.data", dataset_format, workspace, settings
                )
            except ProcessingFailure as failure:
                request.scope["state"]["publication_started"] = True
                await _await_publication(
                    _persist_failed(
                        session,
                        file_id=file_id,
                        filename=filename,
                        dataset_format=dataset_format,
                        upload_size=upload_size,
                        checksum=checksum,
                        failure=failure,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        settings=settings,
                    ),
                    settings.commit_reconciliation_seconds,
                )
                raise GeoAPIError(
                    failure.status_code,
                    failure.code,
                    failure.message,
                    details=failure.details,
                    file_id=file_id,
                ) from failure

            manifest = result.manifest
            assert manifest.feature_count is not None
            assert manifest.measured_count is not None
            assert manifest.not_applicable_count is not None
            assert manifest.unsupported_count is not None
            assert manifest.error_count is not None
            completed_at = datetime.now(UTC)
            record = FileRecord(
                id=file_id,
                filename=filename,
                format=dataset_format,
                upload_size=upload_size,
                sha256=checksum,
                status="COMPLETED",
                created_at=completed_at,
                completed_at=completed_at,
                source_crs_authority=manifest.source_crs.authority if manifest.source_crs else None,
                source_crs_name=manifest.source_crs.name if manifest.source_crs else None,
                source_crs_wkt=manifest.source_crs.wkt if manifest.source_crs else None,
                feature_count=manifest.feature_count,
                measured_count=manifest.measured_count,
                not_applicable_count=manifest.not_applicable_count,
                unsupported_count=manifest.unsupported_count,
                error_count=manifest.error_count,
                deleted_record_count=manifest.deleted_record_count,
                has_issues=manifest.has_issues,
                warnings=[issue.model_dump(mode="json") for issue in manifest.warnings],
                failure=None,
                processing_duration_ms=int((time.monotonic() - started) * 1000),
                measurement_policy=manifest.measurement_policy,
            )
            request.scope["state"]["publication_started"] = True
            try:
                await _await_publication(
                    _persist_completed(session, record, result.features_path, settings),
                    settings.commit_reconciliation_seconds,
                )
            except ProcessingFailure as failure:
                await _await_publication(
                    _persist_failed(
                        session,
                        file_id=file_id,
                        filename=filename,
                        dataset_format=dataset_format,
                        upload_size=upload_size,
                        checksum=checksum,
                        failure=failure,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        settings=settings,
                    ),
                    settings.commit_reconciliation_seconds,
                )
                raise GeoAPIError(
                    failure.status_code,
                    failure.code,
                    failure.message,
                    details=failure.details,
                    file_id=file_id,
                ) from failure
            except (SQLAlchemyError, TimeoutError, OSError) as exc:
                logger.error(
                    "Dataset publication failed type=%s sqlstate=%s constraint=%s",
                    type(exc).__name__,
                    getattr(getattr(exc, "orig", None), "sqlstate", None),
                    getattr(
                        getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None
                    ),
                )
                raise GeoAPIError(
                    503,
                    "PUBLICATION_FAILED",
                    "The completed dataset could not be published to PostgreSQL.",
                    file_id=file_id,
                ) from exc
            response.headers["Location"] = f"/api/files/{file_id}/"
            return _file_summary(record)
        finally:
            with suppress(FileNotFoundError):
                remove_workspace(request.app.state.temp_root, workspace)
    finally:
        await form.close()


@router.get("/api/files/", response_model=None)
async def list_files(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=1024),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    statement = select(FileRecord).order_by(FileRecord.created_at.desc(), FileRecord.id.desc())
    if cursor:
        created_at, file_id = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                FileRecord.created_at < created_at,
                (FileRecord.created_at == created_at) & (FileRecord.id < file_id),
            )
        )
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_next = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1]) if has_next and rows else None
    return {
        "results": [_file_summary(row).model_dump(mode="json") for row in rows],
        "next_cursor": next_cursor,
    }


@router.get("/api/files/{file_id}/", response_model=FileSummary)
async def get_file(file_id: UUID, session: AsyncSession = Depends(get_session)) -> FileSummary:
    record = await _require_file(session, file_id)
    return _file_summary(record)


@router.get("/api/files/{file_id}/measurements/", response_model=None)
async def list_measurements(
    file_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await _require_completed_file(session, file_id)
    count = await _feature_count(session, file_id)
    rows = list(
        (
            await session.scalars(
                select(FeatureRecord)
                .where(FeatureRecord.file_id == file_id)
                .order_by(FeatureRecord.feature_index)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "next": f"/api/files/{file_id}/measurements/?limit={limit}&offset={offset + limit}"
        if offset + limit < count
        else None,
        "results": [_measurement_summary(record, row) for row in rows],
    }


@router.get("/api/files/{file_id}/features/", response_model=None)
async def list_features(
    file_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_completed_file(session, file_id)
    count = await _feature_count(session, file_id)
    rows = list(
        (
            await session.scalars(
                select(FeatureRecord)
                .where(FeatureRecord.file_id == file_id)
                .order_by(FeatureRecord.feature_index)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "next": f"/api/files/{file_id}/features/?limit={limit}&offset={offset + limit}"
        if offset + limit < count
        else None,
        "results": [_feature_summary(file_id, row) for row in rows],
    }


@router.get("/api/files/{file_id}/features/{feature_index}/", response_model=None)
async def get_feature(
    file_id: UUID,
    feature_index: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    file_record = await _require_completed_file(session, file_id)
    if feature_index < 0:
        raise GeoAPIError(422, "REQUEST_VALIDATION_ERROR", "feature_index must be non-negative.")
    feature = await session.get(FeatureRecord, (file_id, feature_index))
    if feature is None:
        raise GeoAPIError(404, "FEATURE_NOT_FOUND", "The requested feature was not found.")
    return {
        "file_id": str(file_id),
        "feature_index": feature.feature_index,
        "source_id": feature.source_id,
        "geometry_type": feature.geometry_type,
        "dimensions": feature.dimensions,
        "geometry_representation": "geojson-style-source-crs",
        "crs": _crs_summary(file_record),
        "geometry": feature.geometry,
        "source_parts": feature.source_parts,
        "properties": feature.properties,
        "metadata": feature.source_metadata,
        "measurement": _measurement_payload(feature),
        "issues": feature.issues,
    }


@router.delete("/api/files/{file_id}/", status_code=204)
async def delete_file(file_id: UUID, session: AsyncSession = Depends(get_session)) -> Response:
    record = await _require_file(session, file_id)
    await session.delete(record)
    await session.commit()
    return Response(status_code=204)


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await asyncio.wait_for(check_database(), timeout=4)
    except Exception as exc:
        raise GeoAPIError(503, "DATABASE_UNAVAILABLE", "PostgreSQL is not ready.") from exc
    return {"status": "ready"}


def _get_single_upload(form: FormData) -> UploadFile:
    items = list(form.multi_items())
    if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
        raise GeoAPIError(
            422, "INVALID_UPLOAD_FIELDS", "Send exactly one multipart field named file."
        )
    return items[0][1]


def _validate_filename(filename: str | None, settings: Settings) -> str:
    if not filename or "\x00" in filename:
        raise GeoAPIError(422, "INVALID_FILENAME", "A non-empty filename is required.")
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GeoAPIError(422, "INVALID_FILENAME", "The filename is not valid UTF-8.") from exc
    if len(encoded) > settings.max_filename_bytes:
        raise GeoAPIError(422, "FILENAME_TOO_LONG", "The filename is too long.")
    return filename


async def _copy_upload(
    upload: UploadFile, destination: Path, settings: Settings
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as target:
        while chunk := await upload.read(64 * 1024):
            size += len(chunk)
            if size > settings.upload_limit_bytes:
                raise GeoAPIError(413, "UPLOAD_SIZE_LIMIT", "The uploaded file exceeds 10 MiB.")
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        import os

        os.fsync(target.fileno())
    if size == 0:
        raise GeoAPIError(422, "EMPTY_UPLOAD", "The uploaded file is empty.")
    return size, digest.hexdigest()


async def _await_publication(
    operation: Coroutine[Any, Any, None], reconciliation_seconds: int
) -> None:
    task = asyncio.create_task(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=reconciliation_seconds)
        except TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
            except TimeoutError:
                logger.error("Publication did not stop after its reconciliation deadline")
        except Exception as exc:
            logger.error("Publication ended after request cancellation: %s", type(exc).__name__)
        raise


async def _persist_failed(
    session: AsyncSession,
    *,
    file_id: UUID,
    filename: str,
    dataset_format: str,
    upload_size: int,
    checksum: str,
    failure: ProcessingFailure,
    duration_ms: int,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    record = FileRecord(
        id=file_id,
        filename=filename,
        format=dataset_format,
        upload_size=upload_size,
        sha256=checksum,
        status="FAILED",
        created_at=now,
        completed_at=now,
        source_crs_authority=None,
        source_crs_name=None,
        source_crs_wkt=None,
        feature_count=None,
        measured_count=None,
        not_applicable_count=None,
        unsupported_count=None,
        error_count=None,
        deleted_record_count=0,
        has_issues=True,
        warnings=[],
        failure={
            "code": failure.code[:80],
            "message": failure.message[:500],
            "details": failure.details,
        },
        processing_duration_ms=duration_ms,
        measurement_policy="geo-api-measurement-v1",
    )
    try:
        async with asyncio.timeout(settings.publication_timeout_seconds):
            async with asyncio.timeout(settings.transaction_timeout_seconds):
                async with session.begin():
                    session.add(record)
                    await session.flush()
    except Exception as exc:
        raise GeoAPIError(
            503, "FAILURE_PERSISTENCE_FAILED", "PostgreSQL could not save the failed upload."
        ) from exc


async def _persist_completed(
    session: AsyncSession,
    record: FileRecord,
    features_path: Path,
    settings: Settings,
) -> None:
    added = 0
    try:
        async with asyncio.timeout(settings.publication_timeout_seconds):
            async with asyncio.timeout(settings.transaction_timeout_seconds):
                async with session.begin():
                    session.add(record)
                    await session.flush()
                    async for batch in iter_feature_result_batches(features_path, settings):
                        for feature in batch:
                            session.add(
                                FeatureRecord(
                                    file_id=record.id,
                                    feature_index=feature.feature_index,
                                    source_id=feature.source_id,
                                    geometry_type=feature.geometry_type,
                                    dimensions=feature.dimensions,
                                    geometry=feature.geometry,
                                    source_parts=feature.source_parts,
                                    properties=feature.properties,
                                    source_metadata=feature.metadata,
                                    measurement_status=feature.measurement_status,
                                    area_m2=feature.area_m2,
                                    length_m=feature.length_m,
                                    provenance=feature.provenance,
                                    issues=[
                                        issue.model_dump(mode="json") for issue in feature.issues
                                    ],
                                )
                            )
                            added += 1
                        await session.flush()
                    if added != record.feature_count:
                        raise ProcessingFailure(
                            "INVALID_PROCESSOR_PROTOCOL",
                            "The processor feature count changed before publication.",
                            status_code=500,
                        )
                    await session.flush()
    except Exception:
        await session.rollback()
        raise


async def _require_file(session: AsyncSession, file_id: UUID) -> FileRecord:
    record = await session.get(FileRecord, file_id)
    if record is None:
        raise GeoAPIError(404, "FILE_NOT_FOUND", "The requested file was not found.")
    return record


async def _require_completed_file(session: AsyncSession, file_id: UUID) -> FileRecord:
    record = await _require_file(session, file_id)
    if record.status == "FAILED":
        raise GeoAPIError(
            409,
            "FILE_PROCESSING_FAILED",
            "This file did not produce a complete dataset.",
            file_id=file_id,
        )
    return record


async def _feature_count(session: AsyncSession, file_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(FeatureRecord).where(FeatureRecord.file_id == file_id)
        )
        or 0
    )


def _file_summary(record: FileRecord) -> FileSummary:
    return FileSummary(
        id=record.id,
        filename=record.filename,
        format=record.format,
        status=record.status,
        upload_size=record.upload_size,
        sha256=record.sha256,
        created_at=record.created_at,
        completed_at=record.completed_at,
        feature_count=record.feature_count,
        measured_count=record.measured_count,
        not_applicable_count=record.not_applicable_count,
        unsupported_count=record.unsupported_count,
        error_count=record.error_count,
        deleted_record_count=record.deleted_record_count,
        has_issues=record.has_issues,
        crs=_crs_summary(record),
        measurement_policy=record.measurement_policy,
        warnings=record.warnings,
        failure=record.failure,
        links={
            "self": f"/api/files/{record.id}/",
            "measurements": f"/api/files/{record.id}/measurements/",
            "features": f"/api/files/{record.id}/features/",
        },
    )


def _crs_summary(record: FileRecord) -> CRSInfo | None:
    if not record.source_crs_name and not record.source_crs_wkt:
        return None
    return CRSInfo(
        authority=record.source_crs_authority,
        name=record.source_crs_name or "Unknown source CRS",
        wkt=record.source_crs_wkt,
    )


def _measurement_payload(feature: FeatureRecord) -> dict[str, Any]:
    kind = (
        "area"
        if feature.area_m2 is not None
        else "length"
        if feature.length_m is not None
        else None
    )
    value = feature.area_m2 if feature.area_m2 is not None else feature.length_m
    provenance = feature.provenance or {}
    return {
        "status": feature.measurement_status,
        "kind": kind,
        "value": value,
        "unit": "m2" if kind == "area" else "m" if kind == "length" else None,
        "method": provenance.get("method"),
        "policy": provenance.get("measurement_policy", "geo-api-measurement-v1"),
        "edge_model": provenance.get("edge_model"),
        "dimension": provenance.get("dimension", 2),
        "component_count": len(provenance.get("components", [])),
        "provenance": provenance,
    }


def _measurement_summary(file_record: FileRecord, feature: FeatureRecord) -> dict[str, Any]:
    measurement = _measurement_payload(feature)
    measurement.pop("provenance", None)
    measurement["detail_url"] = f"/api/files/{file_record.id}/features/{feature.feature_index}/"
    return {
        "feature_index": feature.feature_index,
        "source_id": feature.source_id,
        "geometry_type": feature.geometry_type,
        **measurement,
        "issues": feature.issues,
    }


def _feature_summary(file_id: UUID, feature: FeatureRecord) -> dict[str, Any]:
    return {
        "file_id": str(file_id),
        "feature_index": feature.feature_index,
        "source_id": feature.source_id,
        "geometry_type": feature.geometry_type,
        "measurement_status": feature.measurement_status,
        "measurement": {
            key: value
            for key, value in _measurement_payload(feature).items()
            if key
            in {
                "kind",
                "value",
                "unit",
                "method",
                "policy",
                "edge_model",
                "dimension",
                "component_count",
            }
        },
        "issues": feature.issues,
        "detail_url": f"/api/files/{file_id}/features/{feature.feature_index}/",
    }


def _encode_cursor(record: FileRecord) -> str:
    data = json.dumps(
        {"created_at": record.created_at.isoformat(), "id": str(record.id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"created_at", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at.astimezone(UTC), UUID(data["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise GeoAPIError(422, "INVALID_CURSOR", "The file-list cursor is invalid.") from exc
