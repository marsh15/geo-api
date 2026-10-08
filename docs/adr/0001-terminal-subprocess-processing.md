# ADR 0001: Terminal subprocess processing

**Status:** Accepted

## Context

V1 accepts files up to 10 MiB and returns the terminal result from the upload request. Parsing and measuring geospatial files can raise parser errors or use substantial CPU and memory. The API needs a process boundary around that work while keeping the reviewer workflow to one request.

## Decision

The API starts one fixed worker subprocess for each accepted upload. The worker writes ordered feature records and one terminal manifest to a private request workspace. The parent validates both outputs and publishes a completed dataset in one PostgreSQL transaction. A processor-reported file failure can be saved as one `FAILED` file record without feature rows.

## Consequences

The client waits for parsing, measurement, validation, and publication to finish. The parent can apply a worker deadline and, on Linux, CPU, address-space, and output-file limits. The database never exposes partial features from a completed dataset.

The request remains open while the worker runs. V1 has no queue, durable job state, in-progress status, automatic retry, or polling endpoint. The worker shares the container user and network namespace with the API, so the subprocess is not a full sandbox. Larger or durable workloads need a separate design for jobs, retries, and polling.
