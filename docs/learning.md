# Learning notes

Use these notes to follow one feature from its source file through measurement and storage. The examples use KML, Shapefiles, and the test fixtures in this repository.

## Trace one feature through the service

1. Start with `tests/fixtures/sample.kml`. Find its `Placemark` ID, ExtendedData, polygon coordinates, and the WGS84 CRS assumption in [the file reader](../src/geo_api/processing/readers.py).
2. Read the source-to-WGS84 transformation in [the measurement module](../src/geo_api/processing/measurement.py). The implementation sets XY axis order explicitly and records the selected transformation and its known accuracy.
3. Compare a fixed longitude and latitude difference near the equator and near Bengaluru. The coordinate difference has the same angular values, but its ground distance changes with latitude. A planar Shapely area on those degree values is not an area in square metres.
4. Follow a polygon with a hole. KML boundary tags identify the shell and holes. Shapefile rings use winding, then projected containment to assign a hole. The measurement code subtracts hole area from shell area.
5. Follow an edge from longitude `179.9°` to `-179.9°`. The edge model selects the short WGS84 geodesic across the antimeridian. The component center uses a 3D unit-vector mean, so it does not land near Greenwich because of longitude wraparound.
6. Compare successive densification refinements in `measurement.py`. The implementation requires two consecutive transitions to meet the convergence tolerance. The independent values in `tests/fixtures/geographiclib_reference.json` check the numerical result against a separate implementation.
7. Trace an upload from `RequestBoundaryMiddleware` in `middleware/admission.py` through `api/routes.py:upload_file`. The middleware applies the request limits before the route parses multipart data. The route stages the file and starts `processing/worker.py` as a child process.
8. Follow the worker output through `processing/runner.py`. The parent checks the terminal manifest, feature ordering, status totals, and output bounds before it starts a PostgreSQL transaction.
9. Inspect the resulting file and feature rows through the API. A completed file can include `MEASURED`, `NOT_APPLICABLE`, `UNSUPPORTED`, and `ERROR` features. File completion means all source records were enumerated and the result was published. It does not mean every feature has a measurement.
10. Trigger an invalid archive or a processor-reported failure. Check the structured error, whether the API stored a `FAILED` file summary, and whether the route removed its private workspace. A publication failure can return `503` without a saved file record.

## Keep these concepts separate

- **Source CRS:** the coordinate system declared by the input. Shapefiles need a readable `.prj`; KML uses WGS84 longitude and latitude.
- **Normalized WGS84:** the common geographic coordinate system used to define the edge model.
- **Measurement projection:** the component-centred metre-based plane used for the final 2D area or length calculation.
- **Convergence:** evidence that the next densification refinement changed the measurement by less than the configured tolerance.
- **Independent reference:** a separate GeographicLib result used to check the fixtures. Convergence and reference comparison answer different questions.
- **Source representation:** retained coordinates, geometry, properties, and CRS metadata. The API calls the geometry `geojson-style-source-crs`; it does not claim RFC 7946 GeoJSON conformance.

## Suggested reading order

Read the [API reference](api.md) to see the request and response contract. Then read the [design guide](design.md) for upload ownership and failure handling, followed by the [measurement method](measurement-method.md) for projection and topology rules. The [testing guide](testing.md) shows which fixtures and checks support those claims.
