# Measurement method

## Coordinate systems

Longitude and latitude are angular coordinates. Their numeric differences are not metres, and their planar area changes with latitude. A source CRS describes how input coordinates relate to the Earth; it is not necessarily a suitable measurement plane. geo-api therefore requires a Shapefile `.prj`, interprets KML coordinates as WGS84, transforms source XY coordinates to WGS84, and then builds a local measurement projection per connected component.

Transformations use traditional XY ordering, checked errors, no ballpark fallback, and local PROJ resources only. An unavailable best transformation is a feature or file error; geo-api does not silently download grids or choose a weaker operation.

## Edge model and local projections

Consecutive supplied vertices define the shortest WGS84 ellipsoid geodesic. geo-api densifies those edges and projects the samples. Polygon rings use ellipsoidal Lambert azimuthal equal-area (LAEA); lines use ellipsoidal azimuthal equidistant (AEQD). The center comes from the normalized mean of 3D unit vectors, so longitude averaging does not place a dateline-crossing component near Greenwich.

The full component, including holes and sampled edges, must be provably within a 100 km radius of its center. Segment bounds use the triangle inequality and recursive subdivision. If the remaining work budget cannot establish the bound, geo-api fails closed. This can reject a path extremely close to the boundary.

The initial densification target is 500 m and is halved for at most five refinements. Area convergence is checked against `max(0.01 m², 1e-5 × area)`; length convergence is checked against `max(0.001 m, 1e-6 × length)`. Two consecutive transitions must meet the convergence threshold. Convergence is not an independent accuracy proof.

## Geometry validation

Raw coordinate finiteness, structure, and limits are checked before transformation. Geometry is validated in the projected chart after WGS84 normalization and edge sampling. KML polygon shell and hole roles come from explicit boundary tags. Shapefile roles come from source ring winding; holes are assigned only when projected containment is unambiguous.

geo-api does not close rings, snap coordinates, remove slivers, call `buffer(0)`, apply `make_valid`, or dissolve overlapping polygons. Ambiguous source topology returns an explicit issue and never a guessed measurement.

## Accuracy claims and limits

Numerical tests compare against independent GeographicLib ellipsoidal references for the same shortest-geodesic edge model. The stated acceptance threshold is 0.1% (with small absolute floors); it is a fixture-level acceptance target, not a global mathematical guarantee or survey certification. AEQD preserves distances from its center but not every arbitrary path length exactly, so off-center paths and high latitudes require independent comparison.

The 100 km component radius and resource budgets are part of v1. geo-api supports suitable local and regional features at global locations, including tested antimeridian and polar cases, subject to those limits. It does not measure terrain slope, elevation, extrusion, or planet-scale geometry.
