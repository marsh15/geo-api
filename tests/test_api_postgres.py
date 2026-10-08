from __future__ import annotations

import asyncio
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import shapefile  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from geo_api.api.routes import _persist_completed
from geo_api.config import Settings, get_settings
from geo_api.db.models import Base, FileRecord
from geo_api.db.session import get_engine, get_session_factory
from geo_api.main import create_app
from geo_api.schemas import FeatureResult

FIXTURES = Path(__file__).parent / "fixtures"
WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


def _sync_database_url(value: str) -> str:
    url = make_url(value)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _prepare_schema(database_url: str) -> None:
    async def create() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL 17 database.")
    monkeypatch.setenv("DATABASE_URL", database_url)
    temp_root = tmp_path / "geo_api-requests"
    get_settings.cache_clear()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    _prepare_schema(database_url)
    app = create_app(Settings(_env_file=None, database_url=database_url, temp_root=temp_root))
    with TestClient(app) as client:
        yield client
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    get_settings.cache_clear()


def _make_point_shapefile_zip(directory: Path, *, include_prj: bool) -> bytes:
    base = directory / "points"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POINT)
    writer.field("parcel", "C", size=20)
    writer.point(77.5, 12.9)
    writer.record("BLR-API-1")
    writer.close()
    if include_prj:
        (directory / "points.prj").write_text(WGS84_WKT, encoding="ascii")
    archive_path = directory / "points.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            path = directory / f"points{suffix}"
            if path.exists():
                archive.write(path, arcname=f"points{suffix}")
    return archive_path.read_bytes()


def _delete(client: TestClient, file_id: str) -> None:
    response = client.delete(f"/api/files/{file_id}/")
    assert response.status_code == 204


def test_kml_upload_read_and_cascade_delete(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/files/",
        files={
            "file": (
                "sample.kml",
                (FIXTURES / "sample.kml").read_bytes(),
                "application/octet-stream",
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["location"].endswith("/")
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    file_id = response.json()["id"]

    summary = api_client.get(f"/api/files/{file_id}/").json()
    assert summary["status"] == "COMPLETED"
    assert summary["feature_count"] == 1
    assert summary["measured_count"] == 1
    assert summary["crs"]["authority"] == "EPSG:4326"

    measurements = api_client.get(f"/api/files/{file_id}/measurements/?limit=1").json()
    assert measurements["count"] == 1
    assert measurements["results"][0]["status"] == "MEASURED"
    assert measurements["results"][0]["unit"] == "m2"

    detail = api_client.get(f"/api/files/{file_id}/features/0/").json()
    assert detail["geometry_representation"] == "geojson-style-source-crs"
    assert detail["properties"] == [{"name": "parcel_id", "value": "BLR-001"}]

    _delete(api_client, file_id)
    assert api_client.get(f"/api/files/{file_id}/").status_code == 404
    sync_url = _sync_database_url(os.environ["TEST_DATABASE_URL"])
    with psycopg.connect(sync_url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM features WHERE file_id = %s", (file_id,)
            ).fetchone()[0]
            == 0
        )


def test_zip_upload_and_missing_crs_failed_persistence(
    api_client: TestClient, tmp_path: Path
) -> None:
    complete_dir = tmp_path / "complete"
    complete_dir.mkdir()
    archive = _make_point_shapefile_zip(complete_dir, include_prj=True)
    response = api_client.post("/api/files/", files={"file": ("points.zip", archive)})
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    assert response.json()["format"] == "SHAPEFILE"
    assert response.json()["not_applicable_count"] == 1
    _delete(api_client, file_id)

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing_projection = _make_point_shapefile_zip(missing_dir, include_prj=False)
    response = api_client.post(
        "/api/files/", files={"file": ("missing-crs.zip", missing_projection)}
    )
    assert response.status_code == 422, response.text
    failure = response.json()["error"]
    assert failure["code"] == "MISSING_SHAPEFILE_CRS"
    failed_id = failure["file_id"]
    summary = api_client.get(f"/api/files/{failed_id}/").json()
    assert summary["status"] == "FAILED"
    assert summary["feature_count"] is None
    assert api_client.get(f"/api/files/{failed_id}/measurements/").status_code == 409
    _delete(api_client, failed_id)


def test_rejects_duplicate_upload_fields_without_persisting(api_client: TestClient) -> None:
    payload = (FIXTURES / "sample.kml").read_bytes()
    response = api_client.post(
        "/api/files/",
        files=[("file", ("one.kml", payload)), ("file", ("two.kml", payload))],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_UPLOAD_FIELDS"


def test_database_unavailable_before_processing_does_not_persist(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable() -> None:
        raise OSError("database is offline")

    monkeypatch.setattr("geo_api.api.routes.check_database", unavailable)
    response = api_client.post(
        "/api/files/",
        files={"file": ("unavailable.kml", (FIXTURES / "sample.kml").read_bytes())},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    with psycopg.connect(_sync_database_url(os.environ["TEST_DATABASE_URL"])) as connection:
        count = connection.execute(
            "SELECT count(*) FROM files WHERE filename = 'unavailable.kml'"
        ).fetchone()[0]
    assert count == 0


def test_api_docs_keep_request_headers_and_use_a_bounded_content_policy(
    api_client: TestClient,
) -> None:
    response = api_client.get("/docs")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"


def test_postgres_rejects_nonfinite_and_inconsistent_measurements(
    api_client: TestClient,
) -> None:
    del api_client
    sync_url = _sync_database_url(os.environ["TEST_DATABASE_URL"])
    file_id = uuid4()
    with psycopg.connect(sync_url) as connection:
        connection.execute(
            "INSERT INTO files (id, filename, format, upload_size, sha256, status, "
            "feature_count, measured_count, not_applicable_count, unsupported_count, error_count, "
            "failure) VALUES (%s, 'constraint-test.kml', 'KML', 1, %s, 'COMPLETED', "
            "1, 1, 0, 0, 0, NULL)",
            (file_id, "0" * 64),
        )

    invalid_rows = [
        ("NaN", None, "ck_features_finite_measurements"),
        ("Infinity", None, "ck_features_finite_measurements"),
        ("-Infinity", None, "ck_features_finite_measurements"),
        (1.0, 2.0, "ck_features_measurement_consistency"),
    ]
    for area, length, expected_constraint in invalid_rows:
        try:
            with psycopg.connect(sync_url) as connection:
                connection.execute(
                    "INSERT INTO features (file_id, feature_index, properties, metadata, "
                    "measurement_status, area_m2, length_m, provenance) "
                    "VALUES (%s, 0, '[]'::json, '{}'::json, 'MEASURED', "
                    "%s::double precision, %s::double precision, '{}'::jsonb)",
                    (file_id, area, length),
                )
        except psycopg.errors.CheckViolation as exc:
            assert exc.diag.constraint_name == expected_constraint
        else:
            raise AssertionError(f"PostgreSQL accepted invalid measurements {area=}, {length=}")

    with psycopg.connect(sync_url) as connection:
        connection.execute("DELETE FROM files WHERE id = %s", (file_id,))


def test_failed_feature_insert_rolls_back_the_whole_publication(
    api_client: TestClient, tmp_path: Path
) -> None:
    del api_client
    database_url = os.environ["TEST_DATABASE_URL"]
    file_id = uuid4()
    features_path = tmp_path / "features.jsonl"
    with features_path.open("w", encoding="utf-8") as output:
        for index in range(201):
            feature = FeatureResult(
                feature_index=index,
                source_id="x" * 256 if index == 200 else None,
                geometry_type="Point",
                dimensions=2,
                geometry={"type": "Point", "coordinates": [77.5, 12.9]},
                measurement_status="NOT_APPLICABLE",
            )
            output.write(feature.model_dump_json() + "\n")

    now = datetime.now(UTC)
    record = FileRecord(
        id=file_id,
        filename="rollback.kml",
        format="KML",
        upload_size=1,
        sha256="0" * 64,
        status="COMPLETED",
        created_at=now,
        completed_at=now,
        feature_count=201,
        measured_count=0,
        not_applicable_count=201,
        unsupported_count=0,
        error_count=0,
        has_issues=False,
        warnings=[],
        failure=None,
    )

    async def publish() -> None:
        async with get_session_factory()() as session:
            await _persist_completed(
                session,
                record,
                features_path,
                Settings(_env_file=None, database_url=database_url),
            )

    with pytest.raises(SQLAlchemyError):
        asyncio.run(publish())

    with psycopg.connect(_sync_database_url(database_url)) as connection:
        files = connection.execute(
            "SELECT count(*) FROM files WHERE id = %s", (file_id,)
        ).fetchone()[0]
        features = connection.execute(
            "SELECT count(*) FROM features WHERE file_id = %s", (file_id,)
        ).fetchone()[0]
    assert files == 0
    assert features == 0


def test_transaction_deadline_rolls_back_publication(
    api_client: TestClient, tmp_path: Path
) -> None:
    del api_client
    database_url = os.environ["TEST_DATABASE_URL"]
    file_id = uuid4()
    now = datetime.now(UTC)
    record = FileRecord(
        id=file_id,
        filename="timeout.kml",
        format="KML",
        upload_size=1,
        sha256="0" * 64,
        status="COMPLETED",
        created_at=now,
        completed_at=now,
        feature_count=0,
        measured_count=0,
        not_applicable_count=0,
        unsupported_count=0,
        error_count=0,
        has_issues=False,
        warnings=[],
        failure=None,
    )
    features_path = tmp_path / "empty.jsonl"
    features_path.write_bytes(b"")

    async def publish() -> None:
        async with get_session_factory()() as session:
            await _persist_completed(
                session,
                record,
                features_path,
                Settings(_env_file=None, database_url=database_url, transaction_timeout_seconds=0),
            )

    with pytest.raises(TimeoutError):
        asyncio.run(publish())

    with psycopg.connect(_sync_database_url(database_url)) as connection:
        files = connection.execute(
            "SELECT count(*) FROM files WHERE id = %s", (file_id,)
        ).fetchone()[0]
    assert files == 0
