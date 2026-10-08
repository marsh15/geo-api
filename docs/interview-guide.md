# Interview guide

## Architecture questions

**Why FastAPI?** It provides typed request/response boundaries, async routing, and generated OpenAPI for this focused REST API.

**Why PostgreSQL without PostGIS?** V1 stores immutable source feature payloads and measurements and does not perform spatial search. PostgreSQL supplies relational constraints, transactions, JSON storage, pagination, and cascading deletion without adding spatial query scope.

**Why one synchronous upload request instead of Redis and a queue?** The product limit is small, the client needs an immediate terminal result, and the child has a bounded deadline. Larger workloads would need durable jobs, polling, and retry semantics.

**Why a subprocess instead of only threads?** It gives parser crashes and Linux CPU/address-space/file-size limits a process boundary. It is still not a complete sandbox.

## GIS questions

**Why not measure directly in EPSG:4326?** Degrees are angular and their ground scale varies with latitude. geo-api transforms to a local metre-based projection before planar geometry measurements.

**Why LAEA for area and AEQD for length?** LAEA preserves area on the ellipsoid. AEQD preserves radial distance from the component center and works well for short local lines, but does not preserve every arbitrary path length exactly; refinement and independent tests check the stated domain.

**What does the 0.1% target establish?** It bounds differences on the independent fixtures under the specified edge model. It is not a theorem for all possible geometry or a survey certification.

**What happens when one multipart component fails?** The complete feature measurement is null and the feature status explains why; geo-api does not return a partial sum.

## Reliability and next steps

**What if PostgreSQL fails during commit?** The transaction is not blindly retried. The API reports a storage failure; a connection loss during commit can leave an ambiguous outcome, so the client retry can create a second record in v1.

**What must change before public hosting?** Authentication and authorization, quotas, retention and storage controls, network isolation, abuse monitoring, and a stronger worker sandbox need a separate design.

For a live walkthrough, upload `sample.kml`, inspect its feature detail, compare the Bengaluru result with the numerical test, then trigger an unsupported geometry and trace the explicit status through storage and response serialization.
