# Glossary

This glossary defines the terms used in the API and measurement documentation.

| Term | Meaning in geo-api |
|---|---|
| CRS | Coordinate reference system. It defines coordinate axes, units, and the Earth reference used by a dataset. |
| Component | One connected line or polygon part measured on its own local projection. A multipart feature can contain several components. |
| Dataset | The ordered set of source features read from one uploaded file. |
| Datum | The Earth reference model associated with a CRS. |
| Failed upload | An upload whose file-level processing failed. If PostgreSQL is available, geo-api stores a `FAILED` file summary without feature rows. |
| Feature | One KML Placemark or Shapefile record. geo-api assigns it a stable, zero-based `feature_index`. |
| File | The uploaded KML or ZIP archive, its metadata, and the database summary retained after processing. The original upload is deleted after processing. |
| Hole | A polygon ring whose area is excluded from its containing shell. |
| Measurement | A two-dimensional area or length, with a unit and method provenance. |
| Projection | A coordinate transformation that maps locations on the curved Earth to a plane. |
| Ring | A closed path of coordinates used as a polygon boundary. |
| Shell | The polygon ring that bounds the filled area. |
| Source geometry | The geometry and coordinates extracted from the input file, retained with source CRS information. |
| Unsupported geometry | A recognized geometry that v1 does not measure. The feature remains in the result with status `UNSUPPORTED`. |
| Warning | A bounded note about a non-fatal condition. Processing can complete while a file or feature carries a warning. |
