# ADR 0003: Strict CRS and topology handling

**Status:** Accepted

geo-api requires the Shapefile `.prj`, uses explicit XY transformations, disables PROJ network access and ballpark operations, and does not guess a CRS. It validates geometry in the normalized projected chart and never silently repairs, snaps, closes, or dissolves input boundaries.

Ambiguous Shapefile winding and invalid topology produce explicit feature errors. KML shell and hole roles are retained from the document. A repair workflow would need to expose both original and repaired geometry and is outside v1.
