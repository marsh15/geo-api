# Glossary

| Term | Meaning in geo-api |
|---|---|
| File | The uploaded KML or ZIP archive and its retained database summary. |
| Dataset | The set of source features extracted from one uploaded file. |
| Feature | One source record or KML Placemark, identified by a stable zero-based `feature_index`. |
| Component | One connected polygon or line part measured within a feature. |
| Ring | A closed coordinate path that bounds a polygon. |
| Shell | A polygon ring that bounds the filled area. |
| Hole | A polygon ring excluded from its containing shell. |
| CRS | Coordinate reference system: the coordinate axes, units, and Earth reference used by source data. |
| Datum | The Earth reference model associated with a CRS. |
| Projection | A coordinate transformation from the curved Earth to a plane. |
| Measurement | A 2D projected area or length with units and method provenance. |
| Warning | A bounded note that processing completed but encountered a non-fatal condition. |
| Unsupported geometry | A recognized input geometry that v1 does not measure. |
| Failed upload | A file-wide processing failure persisted without partial feature results. |
