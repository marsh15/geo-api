# Interview guide

Use this guide to explain the decisions in geo-api and to walk a reviewer through a real upload. The [architecture guide](../ARCHITECTURE.md) and [API reference](api.md) provide the fuller diagrams and contracts.

## Explain the architecture

**Why FastAPI?** FastAPI provides typed request and response models, async route handlers, and a generated OpenAPI schema for this REST API. PostgreSQL stores file summaries and feature results.

**Why does the upload run synchronously?** V1 accepts files up to 10 MiB and returns a terminal result from one request. A worker deadline bounds processing. A larger service would need durable job state, polling, retries, and a queue lifecycle.

**Why use a subprocess?** File parsing and geometry libraries handle untrusted input and can fail inside native code. A child process separates that work from the API process and allows Linux CPU, memory, and file-size limits. The process still shares the container user and network namespace, so it is not a complete sandbox.

**Why PostgreSQL without PostGIS?** V1 retrieves stored features and measurements but does not search or filter by spatial relationships. PostgreSQL supplies transactions, constraints, JSON storage, pagination, and cascading deletion. PostGIS would add scope without serving a current endpoint.

**Why validate worker output before writing rows?** The worker is a separate process and its files are an internal protocol boundary. The API checks the manifest, JSON records, feature order, counts, and statuses before it opens a publication transaction. A completed dataset is written as one transaction, so readers do not see a partial feature set.

**What happens if publication fails?** The transaction rolls back and the API returns `503`. A connection loss during commit can leave the client unsure whether the database committed. V1 does not have an idempotency key, so retrying can create another file record.

## Explain the GIS method

**Why not measure directly in EPSG:4326?** Longitude and latitude are angular units. Their ground scale varies with latitude, so planar calculations on degree values do not produce square metres or metres.

**Why use LAEA for area and AEQD for length?** Ellipsoidal LAEA preserves area. AEQD preserves radial distance from the component center and suits the service's short local lines, but it does not preserve every arbitrary path length exactly. Densification convergence and independent fixture comparisons check the method within the stated test domain.

**What does the 0.1% target show?** It shows that the checked-in cases fall within their configured difference from independent GeographicLib values under the same edge model. It does not prove accuracy for all possible geometries or certify a survey measurement.

**Why require a Shapefile `.prj`?** The service needs a declared source CRS to transform coordinates correctly. It returns a structured error rather than guessing when the CRS is absent or unusable.

**What happens if one multipart component fails?** The entire feature receives a null measurement and an explicit status or issue. The API does not add the remaining components into a partial total.

## Walk through the repository

For a short review, start the service with Docker Compose and upload `tests/fixtures/sample.kml`. Show the `201` response and its file ID. Then fetch the file summary, measurement page, and feature detail. Point out the source ID `sample-plot`, the `EPSG:4326` CRS, and the area of about `12003.06 m²`.

For a second case, upload `tests/fixtures/partial.kml`. It contains a polygon, a point, and a mixed geometry. Explain why those features can produce `MEASURED`, `NOT_APPLICABLE`, and `UNSUPPORTED` outcomes while the file still completes.

To discuss failure handling, inspect the invalid-archive and worker-lifecycle cases in the tests. Trace how a processor failure differs from an admission rejection or a PostgreSQL publication failure. The [testing guide](testing.md) lists the relevant coverage.

## Discuss what would change for public hosting

The current Compose service binds the API to localhost and has no authentication. Before public hosting, the design would need authentication and authorization, per-user quotas, retention and storage rules, abuse monitoring, network isolation, and a stronger worker sandbox. V1 also keeps processing in the request and does not cap database growth.
