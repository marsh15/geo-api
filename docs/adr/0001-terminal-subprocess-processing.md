# ADR 0001: Terminal subprocess processing

**Status:** Accepted

geo-api processes each accepted upload synchronously in one child process and persists only terminal `COMPLETED` or `FAILED` records. This keeps the reviewer flow direct while isolating parser crashes and resource use. It avoids queue, Redis, and in-progress database state for the v1 10 MiB upload limit.

The parent owns request admission, temporary storage, protocol validation, and one atomic publication transaction. A child failure cannot publish partial features. Larger or durable workloads require a new design with job state, retries, and polling.
