# API reference

The API accepts one geospatial file per upload. `POST /api/files/` keeps the request open while it parses the file, measures supported features, and saves a terminal result. A successful response has status `201` and includes a `Location` header for the saved file summary.

All responses include `X-Request-ID`. Application and validation errors use this shape:

```json
{
  "error": {
    "code": "MISSING_SHAPEFILE_CRS",
    "message": "The archive must include a readable .prj file.",
    "request_id": "server-generated-id",
    "file_id": "optional UUID",
    "details": {}
  }
}
```

Error responses do not include tracebacks or temporary paths. `file_id` appears when the service has assigned an ID to the processing result.

## Endpoints

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/files/` | Accept one multipart field named `file`, process it synchronously, save the terminal result, and return `201` after commit. |
| `GET` | `/api/files/` | List files from newest to oldest with an opaque cursor. The default page size is 20 and the maximum is 100. |
| `GET` | `/api/files/{file_id}/` | Return file metadata, terminal status, counters, CRS, warnings, and resource links. |
| `GET` | `/api/files/{file_id}/measurements/` | Return ordered measurement and status rows. The default page size is 50 and the maximum is 200. |
| `GET` | `/api/files/{file_id}/features/` | Return ordered feature summaries. The default page size is 50 and the maximum is 200. |
| `GET` | `/api/files/{file_id}/features/{feature_index}/` | Return one feature with source geometry, properties, metadata, issues, measurement, and provenance. |
| `DELETE` | `/api/files/{file_id}/` | Delete the file and its feature rows. An unknown ID returns `404`. |
| `GET` | `/health/live` | Return `{"status":"live"}` while the application process is running. |
| `GET` | `/health/ready` | Check PostgreSQL. Return `503` when the database is unavailable. |

File listing uses `results` and `next_cursor`. Measurement and feature pages use `count`, `limit`, `offset`, `next`, and `results`. An offset beyond the last row returns an empty `results` array. Feature results use ascending `feature_index` order.

## Upload a file

Send one `.kml` file or one `.zip` containing a Shapefile dataset. The filename suffix selects the parser; the service does not trust the supplied MIME type.

```sh
curl --fail-with-body -F 'file=@tests/fixtures/sample.kml' \
  http://127.0.0.1:8000/api/files/
```

For the checked-in `sample.kml`, the response includes one completed file and one measured polygon. The following examples show selected fields from that response. The real response also includes the checksum, timestamps, issue counters, measurement policy, warnings, and links.

```json
{
  "id": "<file-id>",
  "filename": "sample.kml",
  "format": "KML",
  "status": "COMPLETED",
  "feature_count": 1,
  "measured_count": 1,
  "crs": {
    "authority": "EPSG:4326",
    "name": "WGS 84"
  }
}
```

Use the returned ID to fetch the summary and measurement page:

```sh
curl http://127.0.0.1:8000/api/files/FILE_ID/
curl 'http://127.0.0.1:8000/api/files/FILE_ID/measurements/?limit=50&offset=0'
```

The file summary has the same shape as the selected fields above. A measurement page for the sample includes this row. The measured area is about `12003.06 m²`.

```json
{
  "count": 1,
  "limit": 50,
  "offset": 0,
  "next": null,
  "results": [
    {
      "feature_index": 0,
      "source_id": "sample-plot",
      "geometry_type": "Polygon",
      "status": "MEASURED",
      "kind": "area",
      "value": 12003.06,
      "unit": "m2",
      "policy": "geo-api-measurement-v1",
      "edge_model": "wgs84_shortest_geodesic"
    }
  ]
}
```

The response value can vary slightly with dependency versions and numeric precision. The API returns the computed value, and the independent fixture tests check it against the documented tolerance.

## Pagination

`GET /api/files/` accepts `limit` and `cursor` query parameters. The first request can omit `cursor`; the response supplies `next_cursor` when more files remain. Pass that value as the next request's `cursor`.

```sh
curl 'http://127.0.0.1:8000/api/files/?limit=20'
curl 'http://127.0.0.1:8000/api/files/?limit=20&cursor=NEXT_CURSOR'
```

Measurement and feature pages use `limit` and `offset` instead. Their `next` field contains the next URL or `null` when the page is the last one.

## Upload rules and result status

Accepted suffixes are `.zip` and `.kml`, case-insensitively. A ZIP must contain one coherent Shapefile dataset. Shapefiles need a readable `.prj`. KML parsing disables external entity, DTD, and network resolution.

Files have terminal status `COMPLETED` or `FAILED`. For a completed file, `feature_count` equals the sum of `measured_count`, `not_applicable_count`, `unsupported_count`, and `error_count`. A failed file has `null` feature counters and no feature rows. A valid empty dataset can complete with zero features and a warning.

Each feature has one measurement status:

- `MEASURED`: a supported polygon or line has one finite area or length value and measurement provenance.
- `NOT_APPLICABLE`: a valid Point or MultiPoint has no required measurement. The value is `null`.
- `UNSUPPORTED`: the geometry type or geographic extent falls outside the v1 measurement policy. The value is `null`.
- `ERROR`: invalid geometry, a failed transformation, ambiguous topology, or a feature resource limit prevented measurement. The value is `null`.

Multipart measurements are all-or-nothing. If one component fails, the feature has no partial total.

## Error status codes

| HTTP | Examples |
|---:|---|
| `400` | Malformed multipart body |
| `408` | Upload receive or overall request deadline |
| `413` | Upload, archive, record, coordinate, processing-work, or output limit |
| `415` | Unsupported filename extension |
| `422` | Invalid dataset, missing `.prj`, strict CRS failure, or processing deadline |
| `503` | Upload slot occupied, database unavailable, or publication failure |
| `500` | Unexpected child crash or invalid child protocol |
| `404` | Unknown file or feature |
| `409` | Measurements or features requested for a failed file |

Admission, multipart, filename, or database-readiness failures happen before processing and do not create a database row. When the processor reports a file-wide failure, the API saves a `FAILED` file record if PostgreSQL is available. A later result request for that file returns `409 FILE_PROCESSING_FAILED`. A PostgreSQL publication failure returns `503`; it may leave no saved file record.

## Feature detail and measurement provenance

Fetch one feature with:

```sh
curl http://127.0.0.1:8000/api/files/FILE_ID/features/0/
```

The response contains the feature index, source ID, geometry type, dimensions, source geometry, ordered properties, parser metadata, measurement, and issues. The `geometry_representation` field is `geojson-style-source-crs`. The service includes the source CRS, but it does not claim this representation follows RFC 7946 GeoJSON CRS and axis-order rules.

For a measured feature, `measurement` includes the value, unit, method identifier, policy, edge model, dimension, component count, and full provenance. Provenance records the source-to-WGS84 operation and the local projection used for each component. The measurement page omits the full provenance payload; use the feature detail endpoint when you need it.
