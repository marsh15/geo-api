# API reference

All application and validation errors use the form:

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

Responses include `X-Request-ID`. Error messages do not contain tracebacks or temporary paths.

## Endpoints

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/files/` | Accept one multipart field named `file`; synchronously process and persist the terminal result; return `201` and `Location` after commit. |
| `GET` | `/api/files/` | List files newest first, using an opaque cursor (`limit` default 20, maximum 100). |
| `GET` | `/api/files/{file_id}/` | Return metadata, terminal status, counters, CRS, warnings, and links. |
| `GET` | `/api/files/{file_id}/measurements/` | Return ordered compact measurement/status rows (`limit` default 50, maximum 200). |
| `GET` | `/api/files/{file_id}/features/` | Return ordered feature summaries (`limit` default 50, maximum 200). |
| `GET` | `/api/files/{file_id}/features/{feature_index}/` | Return source geometry, properties, metadata, issues, measurement, and provenance. |
| `DELETE` | `/api/files/{file_id}/` | Delete a file and its feature rows; absent IDs return `404`. |
| `GET` | `/health/live` | Return `{"status":"live"}`. |
| `GET` | `/health/ready` | Check the PostgreSQL connection; return `503` when unavailable. |

Every page includes `count`, `limit`, `offset`, `next`, and `results`; offset pages beyond the end are empty. File listing returns `results` and `next_cursor`. All feature ordering uses `feature_index ASC`.

## Upload and result status

Accepted filename suffixes are `.zip` and `.kml`, case-insensitively. MIME type is not trusted for format detection. ZIP content must contain one coherent Shapefile dataset; KML is parsed with external entity, DTD, and network resolution disabled.

Files have terminal `COMPLETED` or `FAILED` status. A completed file's `feature_count` equals the sum of `measured_count`, `not_applicable_count`, `unsupported_count`, and `error_count`. Failed files have `null` counters and no feature rows. A valid empty dataset may complete with zero features and a warning.

Feature measurement statuses:

- `MEASURED`: valid supported area or length with a finite value and provenance.
- `NOT_APPLICABLE`: valid Point or MultiPoint; value is `null`.
- `UNSUPPORTED`: geometry or geographic extent outside the v1 measurement policy; value is `null`.
- `ERROR`: invalid geometry, transformation failure, ambiguous topology, or feature resource limit; value is `null`.

Multipart aggregation is all-or-nothing. No partial sum is returned when one component fails.

## Error status codes

| HTTP | Examples |
|---:|---|
| `400` | Malformed multipart body |
| `408` | Upload receive or overall request deadline |
| `413` | Byte, archive, record, coordinate, or output limit |
| `415` | Unsupported filename extension |
| `422` | Invalid dataset, missing `.prj`, strict CRS failure, or processing deadline |
| `503` | Upload slot occupied, database unavailable, or publication failure |
| `500` | Unexpected child crash or invalid child protocol |
| `404` | Unknown file or feature |
| `409` | Measurements/features requested for a failed file |

An upload rejected before processing creates no database row. Once processing begins, file-wide failures are retained as `FAILED` metadata when PostgreSQL is available. A failed-file result request returns `409 FILE_PROCESSING_FAILED`.

## Measurement detail

The feature detail endpoint labels source coordinates with `geometry_representation: "geojson-style-source-crs"` and includes the source CRS. This representation is not claimed to be RFC 7946 GeoJSON. Measured features include a method identifier, policy version, 2D edge model, units, component projection WKT and centers, transformation metadata, and refinement counts.
