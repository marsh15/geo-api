# ADR 0002: Local measurements from WGS84 geodesic edges

**Status:** Accepted

## Context

Shapefiles can use different source coordinate systems, while KML coordinates use WGS84 longitude and latitude. Planar measurements on angular coordinates do not produce meaningful metre or square-metre values. A single projected CRS would also be a poor fit for features spread across the globe.

## Decision

The reader transforms source coordinates to WGS84. The measurement code treats each supplied edge as the shortest WGS84 ellipsoid geodesic, densifies it, and projects each connected component around a deterministic local center. It uses ellipsoidal Lambert azimuthal equal-area for area and ellipsoidal azimuthal equidistant for length.

Each component must fit within 100 km of its center. Coordinate, generated-work, and refinement budgets bound processing. Independent GeographicLib fixtures check selected results separately from densification convergence.

## Consequences

The local projections support suitable regional features at many global locations, including tested polar and antimeridian cases. The same edge can differ from the rendering convention of the application that created the source file, because the service explicitly chooses the shortest ellipsoidal geodesic between vertices.

AEQD preserves radial distance from the component center but does not preserve every arbitrary path length exactly. The 0.1% fixture threshold is limited to the checked-in cases. Increasing the radius or claiming survey accuracy requires new evidence and a revised measurement contract.
