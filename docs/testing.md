# Testing

The test suite covers three boundaries: parsing KML and Shapefiles, computing measurements, and publishing API results to PostgreSQL. The local checks use the same commands as the CI workflow.

## Run checks locally

Install the development dependencies and run the static checks:

```sh
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src/geo_api
```

The parser, measurement, worker-lifecycle, middleware, and publication unit tests can run with:

```sh
uv run pytest -q
```

PostgreSQL integration tests skip unless `TEST_DATABASE_URL` points to a disposable PostgreSQL 17 database. The fixture copies that value into `DATABASE_URL`, creates the test schema, and exercises the real API and persistence code. Use a database reserved for tests because the suite inserts and deletes geo-api records.

```sh
docker compose up -d db
TEST_DATABASE_URL='postgresql+psycopg://geo_api:geo_api_local_only@127.0.0.1:5432/geo_api' uv run pytest -q
```

The database settings in the example come from `.env.example`. To run Alembic migrations separately, use `uv run alembic upgrade head`.

## Fixtures and numerical references

`tests/fixtures/sample.kml` contains a small Bengaluru polygon with a KML `Placemark` ID and ExtendedData. `tests/fixtures/partial.kml` contains a polygon, a point, and a mixed geometry so the API can report measured, not-applicable, and unsupported outcomes in one completed file.

The tests generate Shapefile ZIP fixtures with PyShp. They exercise the real `.shp`, `.shx`, `.dbf`, `.prj`, and optional encoding-file paths. Tests cover Unicode properties, deleted DBF records, malformed CRS values, absent projection files, archive path abuse, and XML abuse.

`tests/fixtures/geographiclib_reference.json` holds the numerical reference values used by measurement tests. `scripts/generate_reference_fixtures.py` generates the fixture with GeographicLib's WGS84 geodesic inverse and polygon accumulator. Polygon comparisons use the same shortest-geodesic edge model and explicit shell and hole roles. Regenerate the data with:

```sh
uv run python scripts/generate_reference_fixtures.py
```

The fixture acceptance thresholds are `max(0.01 m², 0.1% of reference area)` for area and `max(0.001 m, 0.1% of reference length)` for length. These are separate from the production densification convergence checks. They apply to the checked-in cases and do not establish a global accuracy guarantee.

## Coverage and limits

Measurement fixtures include low-level projected input, Bengaluru, equatorial and antimeridian features, polar KML, polygon holes, multipart overlap, invalid and open rings, unsupported geometry, and source Z and M values. The tests do not cover every CRS operation or every shape near the 100 km limit. Unresolved cases must return an explicit status rather than a guessed measurement.

Admission and worker tests cover streamed body limits without `Content-Length`, upload-slot release, receive deadlines, disconnect cancellation, process-group cleanup, child timeouts and crashes, malformed child output, and transaction rollback. PostgreSQL tests cover successful publication, failed-file persistence, cascading deletion, and rejection of non-finite measurements.

## Docker smoke check

The `docker-smoke` CI job builds the Linux AMD64 image, waits for `/health/ready`, uploads `tests/fixtures/sample.kml`, and checks the terminal response. You can run that flow locally with:

```sh
cp .env.example .env
docker compose up --build
```

Then use the upload command in the [README](../README.md). The README also shows how to retrieve the saved summary, measurements, and feature detail.
