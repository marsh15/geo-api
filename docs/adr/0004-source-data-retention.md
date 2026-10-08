# ADR 0004: Retain source features, delete uploaded files

**Status:** Accepted

## Context

Reviewers need to inspect the source geometry and attributes alongside each measurement. Keeping the original upload would make file handling and retention more complicated, while keeping only aggregate measurements would hide which feature produced a result or issue.

## Decision

After processing, geo-api deletes the uploaded archive and temporary worker output. PostgreSQL retains the file summary and bounded source feature data: geometry, ordered properties, CRS metadata, measurement provenance, and issues. The file row stores a SHA-256 checksum for comparison, but the API does not deduplicate uploads by checksum.

Call `DELETE /api/files/{file_id}/` to remove a file and its feature rows. The database's cascading foreign key deletes the features. In the local Compose setup, `docker compose down -v` removes the database volume and all results in it.

## Consequences

The API can return feature details without keeping the submitted archive. A caller can compare checksums outside the service, but repeated uploads create separate file records. Local results remain in PostgreSQL until an API delete or database reset. V1 does not impose a database retention policy or growth limit.
