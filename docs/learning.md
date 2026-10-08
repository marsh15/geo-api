# Learning notes

These are implementation notes for a developer learning GIS; they describe what the code and fixtures demonstrate.

## Follow one feature through the code

1. Read a KML `Placemark` or Shapefile record and identify its coordinates, properties, and source CRS.
2. Compare the same longitude/latitude deltas near the equator and Bengaluru. The numeric degrees are angular units; using planar `.area` on them does not produce square metres.
3. Trace the source-to-WGS84 transformation. geo-api sets XY axis order explicitly and records the selected operation and known accuracy.
4. Follow a polygon with a hole. KML gives shell and hole roles directly; Shapefile rings use winding and projected containment. The hole is subtracted after projection.
5. Follow a dateline edge from `179.9°` to `-179.9°`. The chosen edge is the short WGS84 geodesic, and vector averaging avoids a center near Greenwich.
6. Compare one projected refinement with the next. Stable successive approximations are required, then the independent GeographicLib reference is checked separately.
7. Upload the fixture and follow the request ID through body admission, worker output validation, and the PostgreSQL transaction.
8. Trigger one invalid ZIP path and one failed worker result. Check that the response is structured, the file-wide failure has no feature rows, and the workspace is removed.

## Concepts to keep distinct

- Source CRS, normalized WGS84, and measurement projection describe different stages.
- A planar projected result is an approximation under a documented local model, not a universal Earth measurement.
- Densification convergence answers whether the approximation is changing; an independent reference answers whether it is accurate enough for a fixture.
- A completed dataset can have unsupported or erroneous features. Completion means the file was fully enumerated and published.
- A source representation can be retained with coordinates and CRS without claiming the coordinates follow RFC 7946 GeoJSON axis and CRS rules.
