# ADR 0003: Strict CRS and topology handling

**Status:** Accepted

## Context

Area and length results depend on the source CRS and polygon boundary roles. A missing projection file or ambiguous ring topology can lead to a plausible-looking but incorrect measurement if the service guesses or repairs the input without telling the caller.

## Decision

The service requires a readable Shapefile `.prj` and never guesses a source CRS. Transformations use explicit XY ordering, checked errors, local PROJ resources, and no ballpark fallback. The measurement code validates geometry after WGS84 normalization and projection.

The reader keeps KML shell and hole roles from the document. For Shapefile rings, it uses source winding and projected containment. geo-api does not silently close or snap rings, remove slivers, call `buffer(0)`, apply `make_valid`, or dissolve overlapping polygons.

## Consequences

An unavailable CRS operation, invalid topology, or ambiguous ring assignment produces a structured error or feature issue. This can reject input that another GIS tool might repair automatically, but the API does not return a measurement for geometry it cannot interpret under the documented policy.

A future repair workflow would need to preserve and expose both the original and repaired geometry, and explain which geometry produced the measurement. Automatic geometry repair is outside v1.
