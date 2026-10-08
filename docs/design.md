# Design

## Ownership

The ASGI boundary assigns a request ID and admits one upload at a time before FastAPI parses multipart data. It counts the complete streamed body, including multipart overhead, and applies receive and overall request deadlines.

The API process owns upload staging, subprocess lifecycle, protocol validation, database transactions, and response serialization. A child process receives only a server-generated input path, a private request directory, the format, and bounded processing settings. It parses the file and writes bounded JSON Lines plus a terminal manifest. It receives no database URL or application secrets.

The API validates the complete child output before opening a publication transaction. It then inserts the file and every feature in one PostgreSQL transaction. A file record is published only in a terminal state. A failed processing result has bounded error metadata and no feature rows.

## Upload lifecycle

1. Acquire the single-upload slot; reject a concurrent upload with `503` and `Retry-After: 5`.
2. Count request bytes and enforce the multipart body limit while Starlette parses the stream.
3. Validate exactly one `file` field, filename length, extension, and actual uploaded byte count.
4. Copy bytes into an app-owned private workspace and compute SHA-256.
5. Check PostgreSQL readiness, then start the fixed worker module without a shell.
6. Reap the worker, validate its manifest and each bounded JSONL record, and verify feature ordering and status totals.
7. Persist either one complete dataset or one failed file record. Remove the temporary workspace and release the slot in `finally` cleanup.

The database transaction is not held while the request body is received or the file is processed. PostgreSQL statement and pool timeouts bound storage operations. Retries create a new file record; v1 does not deduplicate by checksum or provide an idempotency key.

## Limits and failure behavior

Defaults are in `geo_api.config.Settings`: 10 MiB uploaded bytes, 11 MiB complete multipart body, 50 MiB expanded archive, 64 ZIP entries, 5,000 features, 100,000 original coordinates per file, 20,000 per feature, 100,000 generated coordinates per feature/refinement, 500,000 in the final file geometry, 2,000,000 cumulative processing-work units, and 64 MiB total child output. The request has 30 seconds to arrive and 120 seconds overall; the child has 60 seconds, publication 20 seconds, and a database transaction 15 seconds. A canceled request allows at most five seconds for publication reconciliation.

Linux workers apply 1 GiB address-space, 60-second CPU, and 64 MiB file-size limits. macOS runs keep the same application byte and wall-clock limits; Linux-specific `RLIMIT_*` controls are not applied there. CPU and file-size signals map to bounded `413` errors. An unexpected worker crash or malformed protocol maps to `500`; parser and geometry failures map to `422` and are recorded as failed files when PostgreSQL is available.

The child process improves crash and resource isolation. It is not a complete sandbox: it shares the container user and network namespace with the API. Run geo-api only with trusted local users and files. The deployment binds ports to localhost and does not include authentication.

## Storage and deletion

`files` stores checksums, terminal counters, CRS details, warnings, and failure metadata. `features` stores ordered source geometry, ordered properties, feature issues, measurements, and provenance. A composite feature key preserves ingestion order, and a cascading foreign key removes all features with their file. Original uploads exist only in request workspaces and are deleted after the response path completes. Startup removes abandoned, app-owned request directories immediately; the supported deployment uses one API worker and one private temporary root.

Database growth is not capped in v1. The local reviewer workflow retains results until explicit deletion or `docker compose down -v`.
