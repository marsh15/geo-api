# geo-api

geo-api is a FastAPI service for measuring features in KML files and zipped Shapefiles. It stores source features and their attributes with the file's CRS and measurement results in PostgreSQL. Polygon and line measurements are reported in square metres and metres. Points have no measurement, and unsupported or invalid features receive an explicit status.

This repository contains a complete local implementation of the geospatial measurement API assignment. The API processes an upload during the `POST /api/files/` request and returns the saved terminal result.

## Run the service

You need Docker Engine with Docker Compose. From the repository root, run:

```sh
cp .env.example .env
docker compose up --build
```

The API is available at `http://127.0.0.1:8000`. Compose also starts PostgreSQL 17 and binds both services to localhost. The API container applies database migrations before it starts Uvicorn. If port 8000 is already in use, set `API_PORT` in `.env`.

To stop the services, press Ctrl+C. To remove the database volume as well, run `docker compose down -v`. That command deletes all retained geo-api results in the local database.

## Try a KML upload

The repository includes a small KML polygon for a first request:

```sh
curl --fail-with-body -F 'file=@tests/fixtures/sample.kml' \
  http://127.0.0.1:8000/api/files/
```

The upload response includes the file ID. Save it as `FILE_ID` in the examples below, then retrieve the file summary and its measurements:

```sh
curl http://127.0.0.1:8000/api/files/FILE_ID/
curl 'http://127.0.0.1:8000/api/files/FILE_ID/measurements/?limit=50&offset=0'
curl 'http://127.0.0.1:8000/api/files/FILE_ID/features/?limit=50&offset=0'
curl http://127.0.0.1:8000/api/files/FILE_ID/features/0/
```

The sample contains one polygon. Its upload response reports one feature and one measurement, with source CRS `EPSG:4326`. The area is about `12003.06 m²`. See the [API reference](docs/api.md) for response examples, pagination, statuses, and error codes. The running service also exposes [Swagger UI](http://127.0.0.1:8000/docs) and [OpenAPI JSON](http://127.0.0.1:8000/openapi.json).

## Architecture

The API keeps upload handling and database publication in the parent process. A separate worker reads the file and computes measurements. The parent checks the worker's output before writing the file summary and feature rows in one transaction.

![Hand-drawn architecture diagram showing the geo-api upload, worker, measurement, PostgreSQL, read API, and cleanup flow](docs/architecture.svg)

The [architecture guide](ARCHITECTURE.md) describes the request lifecycle, process boundary, failure behavior, and source files behind the diagram.

## Inputs and measurement rules

- Upload either a `.kml` file or a `.zip` containing one Shapefile dataset with `.shp`, `.shx`, `.dbf`, and `.prj` files. A `.cpg` file is optional.
- KML coordinates are interpreted as WGS84 longitude and latitude. A Shapefile must provide a readable `.prj`; geo-api does not infer its CRS.
- Polygon and multipolygon features produce an area. LineString and MultiLineString features produce a length. Point and MultiPoint features have status `NOT_APPLICABLE`. Other recognized but unmeasured types receive `UNSUPPORTED`.
- The service transforms source coordinates to WGS84, models each edge as the shortest WGS84 ellipsoid geodesic, densifies it, then measures in a local metre-based projection. It uses Lambert azimuthal equal-area for polygon area and azimuthal equidistant for line length.
- The service limits uploads to 10 MiB, expanded archives to 50 MiB, files to 5,000 features, and original coordinates to 100,000 per file. The local measurement model also applies a 100 km component-radius limit and coordinate and processing-work budgets.
- Unsupported, invalid, or unverified geometry gets an explicit status and issue. An unavailable measurement is `null`, not zero.

The independent numerical fixtures use separate acceptance thresholds: `max(0.01 m², 0.1% of reference area)` for area and `max(0.001 m, 0.1% of reference length)` for length. These thresholds apply to the checked-in cases. They do not certify every geometry or provide survey-grade accuracy.

The service deletes the original upload and temporary worker output after processing. It retains the file summary and feature results in PostgreSQL until `DELETE /api/files/{file_id}/` or a local database reset.

## Development and checks

For development without the API container, install Python 3.13, `uv`, and PostgreSQL 17. The included `.env.example` configures the local database used by the Compose setup.

```sh
cp .env.example .env
docker compose up -d db
uv sync --frozen --all-groups
uv run alembic upgrade head
uv run uvicorn geo_api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Run formatting, lint, type checks, and the test suite with:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy src/geo_api
TEST_DATABASE_URL='postgresql+psycopg://geo_api:geo_api_local_only@127.0.0.1:5432/geo_api' uv run pytest -q
```

The PostgreSQL integration tests require `TEST_DATABASE_URL` to point to a disposable PostgreSQL 17 database. They create and remove geo-api rows during the run. The [testing guide](docs/testing.md) describes the fixtures and coverage.

## Design decisions

- **FastAPI and PostgreSQL:** FastAPI provides request validation and an OpenAPI contract. PostgreSQL stores file summaries and feature results. V1 does not run spatial searches, so it does not require PostGIS.
- **Synchronous requests and a worker process:** the client receives a terminal result in one request. A child process isolates parser crashes and applies processing limits. The service does not use a queue or retain an in-progress job state.
- **Strict CRS and geometry handling:** the service requires an explicit Shapefile CRS and reports invalid or ambiguous topology instead of repairing input silently.
- **Local measurements:** a component-centred projection and explicit shortest-geodesic edge model avoid measuring longitude and latitude as planar metres. The supported area and extent are bounded and documented.

The [design guide](docs/design.md), [measurement method](docs/measurement-method.md), and [architecture decisions](docs/adr/) explain these choices and their limits.

## Project map

```text
src/geo_api/api/          FastAPI routes and error responses
src/geo_api/middleware/   Request IDs, upload admission, body and time limits
src/geo_api/processing/   File readers, measurement logic, worker protocol, storage
src/geo_api/db/           PostgreSQL models and session setup
migrations/               Alembic schema migration
tests/                    Parser, measurement, lifecycle, and PostgreSQL tests
docs/                     API, design, method, testing, and learning notes
```

## Learning notes and future work

This project uses a local file-processing service to explore FastAPI, PostgreSQL, CRS transformations, geodesic measurements, and bounded worker processes. The [learning notes](docs/learning.md) follow those ideas through the code and fixtures. The [interview guide](docs/interview-guide.md) summarizes the design trade-offs.

Authentication, public hosting, durable background jobs, KMZ, automatic geometry repair, more input formats, and PostGIS spatial search are outside v1. The service has no authentication and is intended for local use with trusted users and files. The worker process is resource-limited, but it is not a complete security sandbox.

## License

MIT. See [LICENSE](LICENSE).
