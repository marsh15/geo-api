# Measurement method

geo-api reports two-dimensional area and length. The method keeps source coordinates and CRS metadata for inspection, then measures a normalized copy of each supported feature. It does not treat longitude and latitude as planar metres.

## Coordinate systems and transformations

Longitude and latitude are angular coordinates. Their numeric differences are not distances, and a planar area computed directly from them changes with latitude. A source CRS describes the coordinates in an input file. It does not necessarily define a suitable plane for measuring area or length.

KML coordinates are interpreted as WGS84 longitude and latitude. A Shapefile must include a readable `.prj` file. The reader rejects missing, unsupported, or invalid CRS definitions rather than guessing. The measurement code transforms source XY coordinates to WGS84, with traditional XY ordering, checked transformation errors, no ballpark fallback, and local PROJ resources only. If the best available operation needs a missing grid, the feature or file fails; geo-api does not download the grid or silently choose a weaker operation.

## Edge model and local projections

Each pair of supplied vertices defines the shortest geodesic between them on the WGS84 ellipsoid. geo-api densifies that edge, then projects the samples into a local plane centered on the connected component. It finds the center from the normalized mean of 3D unit vectors. This avoids placing a component that crosses the antimeridian near Greenwich through ordinary longitude averaging.

The service uses ellipsoidal Lambert azimuthal equal-area (LAEA) for polygon area and ellipsoidal azimuthal equidistant (AEQD) for line length. LAEA preserves area on the ellipsoid. AEQD preserves radial distance from the projection center, but it does not preserve every arbitrary path length exactly. The method is intended for the documented local domain.

The entire component, including polygon holes and sampled edges, must fit within 100 km of its center. Segment bounds use the triangle inequality and recursive subdivision. If the configured work budget cannot prove that the component fits, geo-api returns an explicit failure. A valid path extremely close to the radius limit can therefore be rejected when the remaining budget cannot resolve it.

## Densification and convergence

The initial edge segment target is 500 m. geo-api halves that target for at most five refinements. It checks area against `max(0.01 m², 1e-5 × area)` and length against `max(0.001 m, 1e-6 × length)`. Two consecutive refinement transitions must meet the applicable threshold.

Convergence shows that successive projected approximations have stopped changing beyond the selected tolerance. It does not independently establish accuracy. The test suite compares results with separate GeographicLib references using the same edge model.

## Geometry validation and topology

The reader checks coordinate types, finiteness, structure, and resource limits before transformation. The measurement code validates geometry in the projected chart after WGS84 normalization and edge sampling.

KML polygon shell and hole roles come from the document's boundary tags. Shapefile ring roles come from source winding; the code assigns holes only when projected containment is unambiguous. If the input does not establish those roles clearly, the feature receives an issue.

geo-api does not close rings, snap coordinates, remove slivers, call `buffer(0)`, apply `make_valid`, or dissolve overlapping polygons. These operations can change the source shape. V1 returns an explicit issue for invalid or ambiguous topology instead of silently changing the geometry.

## Accuracy and supported domain

Numerical tests compare the implementation with independent GeographicLib ellipsoidal references. The acceptance thresholds are `max(0.01 m², 0.1% of reference area)` for area and `max(0.001 m, 0.1% of reference length)` for length. They apply to the checked-in fixtures under the same shortest-geodesic edge model. They are not a guarantee for all possible geometries and do not certify survey measurements.

The v1 policy limits each connected component to a 100 km radius and places finite budgets on input coordinates, generated coordinates, and processing work. It supports suitable local and regional features at global locations, including tested polar and antimeridian cases. It does not measure terrain slope, elevation, extrusion, or planet-scale geometry.
