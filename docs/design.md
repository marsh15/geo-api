# Design and failure behavior

This guide describes what the running service owns, how an upload moves through it, and what the API returns when a step fails. The [architecture guide](../ARCHITECTURE.md) has the system diagram and source map. The [API reference](api.md) lists every endpoint and response status.

## Process boundaries

The API process owns request admission, multipart parsing, upload staging, worker lifecycle, worker-output validation, database transactions, and response serialization. `RequestBoundaryMiddleware` runs before the upload route parses multipart data. It assigns a request ID, permits one upload at a time, counts the full request body, and enforces receive and overall deadlines.

The API starts one fixed Python worker module without a shell. It passes a server-generated input path, the private request workspace, the input format, and bounded settings. The worker receives no database URL or application secrets. It writes bounded JSON Lines for feature results and a terminal manifest. On Linux, the worker also receives address-space, CPU-time, and file-size limits.

The worker is a process boundary for parser crashes and resource limits. It is not a complete sandbox: it runs as the same container user and shares the container's network namespace. The supported Compose setup binds the API and database to localhost and does not configure authentication. Use the service with trusted local users and files.

## Upload lifecycle

The upload request follows these steps:

1. The middleware acquires the single-upload slot. If another upload holds it, the API returns `503` with `Retry-After: 5`.
2. The middleware counts streamed bytes, including multipart overhead. It enforces the request-body limit and upload receive and overall deadlines.
3. The route parses the multipart form and requires exactly one `file` field. It checks the filename, suffix, filename length, and actual uploaded byte count.
4. The route creates a private, app-owned workspace, copies the upload into it, and computes a SHA-256 checksum.
5. The route checks PostgreSQL readiness. If the database is unavailable at this point, it returns `503` without starting processing or creating a file row.
6. The runner starts the worker with the selected format and bounded settings. The request remains open while the worker parses and measures the dataset.
7. The worker writes a manifest and ordered feature records. The parent checks output sizes, JSON shape, feature indexes, record counts, and status totals before it opens a publication transaction.
8. For a completed result, one PostgreSQL transaction inserts the file summary and every feature. For a processor-reported file failure, the API attempts to save one `FAILED` summary without feature rows. Database publication errors return `503` and may leave no file record.
9. The route removes its temporary workspace and releases the upload slot in cleanup. Startup also removes abandoned `request-<uuid>` workspaces left by a stopped process.

The service does not hold a database transaction while it receives or processes the upload. PostgreSQL connection, statement, transaction, publication, and commit-reconciliation timeouts bound storage work. A retry creates a new file ID. V1 does not deduplicate by checksum or accept an idempotency key.

## Limits and failure behavior

The defaults live in `geo_api.config.Settings`:

| Resource or deadline | Default |
|---|---:|
| Uploaded file | 10 MiB |
| Complete multipart body | 11 MiB |
| Expanded archive | 50 MiB |
| ZIP entries | 64 |
| Features per file | 5,000 |
| Original coordinates per file | 100,000 |
| Original coordinates per feature | 20,000 |
| Generated coordinates per feature and refinement | 100,000 |
| Final generated coordinates per file | 500,000 |
| Cumulative processing work | 2,000,000 units |
| Child output | 64 MiB |
| Upload receive deadline | 30 seconds |
| Total request deadline | 120 seconds |
| Worker deadline | 60 seconds |
| Publication deadline | 20 seconds |
| Database transaction | 15 seconds |
| Commit reconciliation after cancellation | 5 seconds |

Linux workers also have a 1 GiB address-space limit, a 60-second CPU limit, and a 64 MiB file-size limit. The application byte and wall-clock limits still apply on macOS, where Linux-specific `RLIMIT_*` controls are not used. CPU and file-size signals map to bounded `413` errors.

The API maps malformed multipart bodies to `400`, upload and processing limits to `413`, invalid datasets to `422`, database readiness or publication failures to `503`, and unexpected worker crashes or malformed worker output to `500`. A parser or file-level geometry failure is recorded as `FAILED` when PostgreSQL is available. A per-feature problem can coexist with a `COMPLETED` file; the feature receives status `UNSUPPORTED` or `ERROR`, and its measurement value remains `null`.

## Storage and deletion

The `files` table stores the checksum, format, terminal counters, source CRS, warnings, and file-level failure metadata. The `features` table stores feature indexes, source geometry, ordered properties, parser metadata, measurement values, issues, and provenance. The composite key `(file_id, feature_index)` preserves feature order. Its foreign key cascades deletion from a file to its features.

The original upload and worker output live only in the private request workspace. The route removes that workspace after it finishes. The database keeps the file summary and feature results until the user calls `DELETE /api/files/{file_id}/` or removes the local Compose volume with `docker compose down -v`.

V1 does not cap database growth. It is a local reviewer-run service, not a public upload endpoint. Public hosting would require authentication and authorization, quotas, retention rules, abuse monitoring, network isolation, and a stronger worker sandbox.
