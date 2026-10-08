# geo-api

geo-api is a Python 3.13 API that reads KML files and zipped Shapefiles, preserves their source geometry and attributes, and reports 2D area or length measurements in metres. It uses FastAPI, PostgreSQL 17, PyShp, FastKML, Shapely, and pyproj.

## Run with Docker

```sh
cp .env.example .env
docker compose up --build
```

The API listens on `http://127.0.0.1:8000`. Compose binds both the API and PostgreSQL to localhost. Set `API_PORT` in `.env` if port 8000 is already in use. The container applies Alembic migrations before starting Uvicorn.

To remove the local database and all retained results, run `docker compose down -v`. This reset is destructive to local geo-api data.

## Upload and retrieve a file

```sh
curl --fail-with-body -F 'file=@tests/fixtures/sample.kml' \
  http://127.0.0.1:8000/api/files/
```

The response includes the new `id`. Use it to inspect the file, its measurements, and its features:

```sh
curl http://127.0.0.1:8000/api/files/FILE_ID/
curl 'http://127.0.0.1:8000/api/files/FILE_ID/measurements/?limit=50&offset=0'
curl 'http://127.0.0.1:8000/api/files/FILE_ID/features/?limit=50&offset=0'
curl http://127.0.0.1:8000/api/files/FILE_ID/features/0/
```

Open [Swagger UI](http://127.0.0.1:8000/docs) or [OpenAPI JSON](http://127.0.0.1:8000/openapi.json) for the endpoint contract.

For `sample.kml`, the upload response is `201` with `status: "COMPLETED"`, `feature_count: 1`, `measured_count: 1`, and `crs.authority: "EPSG:4326"`. Its first measurement row has `kind: "area"`, `unit: "m2"`, and a value of about `12003.06`.

The example in `tests/fixtures/partial.kml` contains a measurable polygon, a point with no applicable measurement, and a mixed geometry that is explicitly unsupported. A completed file can contain a mix of `MEASURED`, `NOT_APPLICABLE`, `UNSUPPORTED`, and `ERROR` features; unavailable measurements are `null`, never zero.

An upload without a Shapefile projection file returns a structured error like:

```json
{"error":{"code":"MISSING_SHAPEFILE_CRS","message":"The archive must include a readable .prj file.","request_id":"...","details":{}}}
```

Antimeridian coverage uses the short edge between longitudes `179.9°` and `-179.9°`. A supported ring can be represented in KML as `179.9,-0.05 -179.9,-0.05 -179.9,0.05 179.9,0.05 179.9,-0.05`; numerical reference cases are in `tests/test_measurement.py`.

## Native development

Requirements: Python 3.13, `uv`, and PostgreSQL 17. The included `.env.example` uses a local PostgreSQL database.

```sh
cp .env.example .env
docker compose up -d db
uv sync --frozen --all-groups
uv run alembic upgrade head
uv run uvicorn geo_api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Run the quality checks and tests with:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy src/geo_api
TEST_DATABASE_URL='postgresql+psycopg://geo_api:geo_api_local_only@127.0.0.1:5432/geo_api' uv run pytest -q
```

The PostgreSQL API tests use the database in `TEST_DATABASE_URL` and clean up the rows they create.

## Supported inputs and measurement model

- `.kml` files with local Placemark geometry and `.zip` archives containing one Shapefile dataset (`.shp`, `.shx`, `.dbf`, `.prj`).
- Upload limit: 10 MiB; expanded archive limit: 50 MiB; at most 5,000 features and 100,000 original coordinate positions per file.
- Shapefiles require a usable `.prj`; geo-api never guesses a CRS. KML coordinates are WGS84 longitude and latitude.
- Coordinates are normalized to WGS84. Edges follow the shortest WGS84 ellipsoid geodesic, are densified, projected into a local ellipsoidal Lambert azimuthal equal-area projection for polygon area or azimuthal equidistant projection for line length, then measured in 2D.
- Components are limited to a 100 km radius around a deterministic local center. Invalid, ambiguous, unsupported, or unverified geometry has an explicit feature status and issue.
- The acceptance target for independent numerical fixtures is `max(0.01 m², 0.1% of reference area)` for area and `max(0.001 m, 0.1% of reference length)` for length. This is a test target, not survey certification.

geo-api deletes uploaded originals and temporary results after processing. It retains the file summary and feature results in PostgreSQL until `DELETE /api/files/{file_id}/` or a local data reset.

## Local security boundary

This is a reviewer-run local service with no authentication. Only use it with files from trusted local users. Processing runs in a separate resource-limited process with no database credentials, but that process is not a complete sandbox against native-code compromise. The project is not an Internet-facing upload service.

## Documentation

- [Design and failure behavior](docs/design.md)
- [API reference](docs/api.md)
- [Measurement method](docs/measurement-method.md)
- [Testing and fixture provenance](docs/testing.md)
- [Glossary](GLOSSARY.md)
- [Learning notes](docs/learning.md)
- [Interview guide](docs/interview-guide.md)
- [Architecture decisions](docs/adr/)

## Learning and future scope

The implementation is intended as a practical backend and GIS learning project. The notes explain the choices exercised by the code and tests; they do not claim measured performance or survey-grade accuracy. Authentication, public hosting, durable jobs, KMZ, automatic geometry repair, additional formats, and PostGIS spatial search are outside v1.

## License

MIT. See [LICENSE](LICENSE).
