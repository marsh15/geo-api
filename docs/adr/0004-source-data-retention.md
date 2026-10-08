# ADR 0004: Retain source features, delete uploaded files

**Status:** Accepted

geo-api retains bounded source geometry, ordered properties, source CRS, measurement provenance, and issues in PostgreSQL so reviewers can inspect every feature. It deletes the uploaded archive and temporary extraction after processing.

The file record keeps a SHA-256 checksum for comparison, not automatic deduplication. Local records remain until the delete endpoint or explicit development reset.
