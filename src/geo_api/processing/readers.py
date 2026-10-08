"""Bounded readers for KML and zipped Shapefile inputs."""

from __future__ import annotations

import io
import json
import math
import posixpath
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import shapefile  # type: ignore[import-untyped]
from defusedxml import ElementTree as DefusedET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from fastkml.features import Placemark
from pydantic import ValidationError
from pyproj import CRS, Geod, Transformer, network
from shapely.geometry import Polygon

from geo_api.config import Settings
from geo_api.processing.errors import ProcessingFailure
from geo_api.processing.measurement import measure_geometry
from geo_api.schemas import FeatureResult, Issue, ProcessingManifest

_XML_MAX_DEPTH = 64
_XML_MAX_ELEMENTS = 250_000
_XML_MAX_TEXT = 64 * 1024
_GEOMETRY_MAX_BYTES = 1024 * 1024
_PROPERTIES_MAX_BYTES = 64 * 1024
_METADATA_MAX_BYTES = 1024 * 1024
_ISSUE_LIMIT = 20
_MAX_COMPONENTS_PER_FEATURE = 128
_ZIP_CHUNK = 64 * 1024
_MEASUREMENT_POLICY = "geo-api-measurement-v1"
_WGS84 = CRS.from_epsg(4326)
_GEOD: Geod = _WGS84.get_geod() or Geod(ellps="WGS84")
_RING_CONTAINMENT_SPACING_M = 500.0
network.set_network_enabled(False)  # type: ignore[attr-defined]

# DBF LDID values with stable, commonly encountered Python codec mappings.
_DBF_LDID_CODECS = {
    0x01: "cp437",
    0x02: "cp850",
    0x03: "cp1252",
    0x04: "mac_roman",
    0xC8: "cp1250",
    0xC9: "cp1251",
    0xCA: "cp1254",
    0xCB: "cp1253",
    0xCC: "cp1257",
    0xCD: "cp1258",
}
_CPG_CODECS = {
    "65001": "utf-8",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "437": "cp437",
    "850": "cp850",
    "852": "cp852",
    "866": "cp866",
    "874": "cp874",
    "932": "cp932",
    "936": "gbk",
    "949": "cp949",
    "950": "cp950",
    "10000": "mac_roman",
    "1250": "cp1250",
    "1251": "cp1251",
    "1252": "cp1252",
    "1253": "cp1253",
    "1254": "cp1254",
    "1255": "cp1255",
    "1256": "cp1256",
    "1257": "cp1257",
    "1258": "cp1258",
    "windows-1250": "cp1250",
    "windows-1251": "cp1251",
    "windows-1252": "cp1252",
    "windows-1253": "cp1253",
    "windows-1254": "cp1254",
    "windows-1255": "cp1255",
    "windows-1256": "cp1256",
    "windows-1257": "cp1257",
    "windows-1258": "cp1258",
    "ansi 1252": "cp1252",
    "iso-8859-1": "iso8859-1",
    "latin1": "iso8859-1",
}
_ZIP_COMPRESSION_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_ZIP_INDEX_SUFFIXES = {
    ".ain",
    ".aih",
    ".atx",
    ".fbn",
    ".fbx",
    ".fix",
    ".ixs",
    ".mxs",
    ".qix",
    ".sbn",
    ".sbx",
}
_SHAPE_KINDS = {
    1: "Point",
    3: "PolyLine",
    5: "Polygon",
    8: "MultiPoint",
    11: "PointZ",
    13: "PolyLineZ",
    15: "PolygonZ",
    18: "MultiPointZ",
    21: "PointM",
    23: "PolyLineM",
    25: "PolygonM",
    28: "MultiPointM",
    31: "MultiPatch",
}
_SHAPE_POINT_TYPES = {1, 11, 21}
_SHAPE_LINE_TYPES = {3, 13, 23}
_SHAPE_POLYGON_TYPES = {5, 15, 25}
_SHAPE_MULTIPOINT_TYPES = {8, 18, 28}
_SHAPE_Z_TYPES = {11, 13, 15, 18}
_SHAPE_M_TYPES = {21, 23, 25, 28}


def process_input(
    input_path: Path,
    dataset_format: str,
    workspace: Path,
    settings: Settings,
) -> tuple[ProcessingManifest, list[FeatureResult]]:
    """Parse and measure one bounded KML or zipped Shapefile input."""
    format_name = dataset_format.upper()
    if format_name not in {"KML", "SHAPEFILE"}:
        raise ProcessingFailure(
            "UNSUPPORTED_FORMAT", "Only KML and zipped Shapefiles are supported.", status_code=415
        )

    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    reader_workspace = workspace / "reader"
    try:
        reader_workspace.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ProcessingFailure(
            "WORKSPACE_NOT_EMPTY", "The processing workspace is not empty."
        ) from exc

    try:
        _check_input_size(input_path, settings.upload_limit_bytes)
        if format_name == "KML":
            source_crs = _wgs84_info()
            features, warnings = _read_kml(input_path, settings, source_crs.wkt or "")
            deleted_count = 0
        else:
            features, source_crs, warnings, deleted_count = _read_shapefile_zip(
                input_path, reader_workspace, settings
            )
        return _completed_manifest(
            format_name,
            source_crs,
            features,
            warnings,
            deleted_count,
            settings,
        )
    except ProcessingFailure:
        raise
    except (OSError, zipfile.BadZipFile, DefusedXmlException, ValidationError) as exc:
        raise ProcessingFailure(
            "INVALID_DATASET", "The uploaded dataset could not be read safely."
        ) from exc
    except Exception as exc:
        # Parser exception text may contain user-controlled content or local paths.
        raise ProcessingFailure(
            "INVALID_DATASET", "The uploaded dataset could not be read safely."
        ) from exc


def _check_input_size(path: Path, limit: int) -> None:
    if not path.is_file():
        raise ProcessingFailure("INVALID_DATASET", "The uploaded file is unavailable.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProcessingFailure("INVALID_DATASET", "The uploaded file is unavailable.") from exc
    if size > limit:
        raise ProcessingFailure(
            "UPLOAD_TOO_LARGE", "The uploaded file exceeds the byte limit.", status_code=413
        )


def _read_bounded(path: Path, limit: int) -> bytes:
    data = bytearray()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(min(_ZIP_CHUNK, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise ProcessingFailure(
                        "UPLOAD_TOO_LARGE",
                        "The uploaded file exceeds the byte limit.",
                        status_code=413,
                    )
    except ProcessingFailure:
        raise
    except OSError as exc:
        raise ProcessingFailure("INVALID_DATASET", "The uploaded file is unavailable.") from exc
    return bytes(data)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def _payload_too_large(value: Any, limit: int) -> bool:
    if value is None:
        return False
    try:
        return _json_size(value) > limit
    except (TypeError, ValueError, OverflowError):
        return True


def _issue(code: str, message: str, severity: str = "warning", **details: Any) -> Issue:
    return Issue(code=code, message=message[:512], severity=severity, details=details)


def _bounded_issues(issues: Iterable[Issue]) -> list[Issue]:
    values = list(issues)
    if len(values) <= _ISSUE_LIMIT:
        return values
    return [
        *values[: _ISSUE_LIMIT - 1],
        _issue(
            "ISSUES_TRUNCATED",
            "Additional feature issues were omitted.",
            omitted_count=len(values) - (_ISSUE_LIMIT - 1),
        ),
    ]


def _append_bounded_issue(issues: list[Issue], issue: Issue) -> None:
    if len(issues) < _ISSUE_LIMIT:
        issues.append(issue)
    elif issues[-1].code == "ISSUES_TRUNCATED":
        issues[-1].details["omitted_count"] = int(issues[-1].details["omitted_count"]) + 1
    else:
        issues[-1] = _issue(
            "ISSUES_TRUNCATED",
            "Additional feature issues were omitted.",
            omitted_count=2,
        )


def _completed_manifest(
    format_name: str,
    source_crs: Any,
    features: list[FeatureResult],
    warnings: list[Issue],
    deleted_record_count: int,
    settings: Any,
) -> tuple[ProcessingManifest, list[FeatureResult]]:
    counts = {
        "MEASURED": 0,
        "NOT_APPLICABLE": 0,
        "UNSUPPORTED": 0,
        "ERROR": 0,
    }
    output_bytes = 0
    final_generated = 0
    generated_work = 0
    for expected_index, feature in enumerate(features):
        if feature.feature_index != expected_index:
            raise ProcessingFailure(
                "INVALID_FEATURE_ORDER", "Feature results are not in source order."
            )
        counts[feature.measurement_status] += 1
        try:
            output_bytes += len(feature.model_dump_json().encode("utf-8")) + 1
        except (TypeError, ValueError) as exc:
            raise ProcessingFailure(
                "INVALID_FEATURE_RESULT", "A feature result could not be serialized."
            ) from exc
        provenance = feature.provenance or {}
        final_generated += _nonnegative_int(provenance.get("generated_coordinates_final"))
        generated_work += _nonnegative_int(provenance.get("generated_coordinates_total"))
    manifest = ProcessingManifest(
        protocol_version=1,
        format=format_name,
        status="COMPLETED",
        source_crs=source_crs,
        feature_count=len(features),
        measured_count=counts["MEASURED"],
        not_applicable_count=counts["NOT_APPLICABLE"],
        unsupported_count=counts["UNSUPPORTED"],
        error_count=counts["ERROR"],
        deleted_record_count=deleted_record_count,
        has_issues=bool(warnings or any(feature.issues for feature in features)),
        warnings=_bounded_issues(warnings),
        measurement_policy=_MEASUREMENT_POLICY,
    )
    output_bytes += len(manifest.model_dump_json().encode("utf-8")) + 1
    if output_bytes > settings.max_child_output_bytes:
        raise ProcessingFailure(
            "CHILD_OUTPUT_LIMIT", "The dataset result exceeds the output limit.", status_code=413
        )
    # These aggregate limits are applied before returning any feature rows to the worker.
    if final_generated > settings.max_generated_coordinates_file:
        raise ProcessingFailure(
            "GENERATED_COORDINATE_LIMIT",
            "The dataset exceeds the generated-coordinate limit.",
            status_code=413,
        )
    if generated_work > settings.max_generated_work:
        raise ProcessingFailure(
            "GENERATED_WORK_LIMIT",
            "The dataset exceeds the processing-work limit.",
            status_code=413,
        )
    return manifest, features


def _append_feature(
    features: list[FeatureResult], feature: FeatureResult, output_bytes: int, settings: Any
) -> int:
    feature = _fit_feature_output(feature, settings.max_feature_output_bytes)
    try:
        output_bytes += len(feature.model_dump_json().encode("utf-8")) + 1
    except (TypeError, ValueError) as exc:
        raise ProcessingFailure(
            "INVALID_FEATURE_RESULT", "A feature result could not be serialized."
        ) from exc
    if output_bytes > settings.max_child_output_bytes:
        raise ProcessingFailure(
            "CHILD_OUTPUT_LIMIT", "The dataset exceeds the output limit.", status_code=413
        )
    features.append(feature)
    return output_bytes


def _fit_feature_output(feature: FeatureResult, limit: int) -> FeatureResult:
    def serialized_size(value: FeatureResult) -> int:
        return len(value.model_dump_json().encode("utf-8")) + 1

    if serialized_size(feature) <= limit:
        return feature
    issue = _issue(
        "FEATURE_OUTPUT_LIMIT",
        "The feature result exceeded its serialized output limit.",
        "error",
    )
    feature = feature.model_copy(
        update={
            "measurement_status": "ERROR",
            "area_m2": None,
            "length_m": None,
            "provenance": _compact_generated_work(feature.provenance),
            "issues": _bounded_issues([*feature.issues, issue]),
        }
    )
    if serialized_size(feature) <= limit:
        return feature
    for field_name, value in (
        ("source_parts", None),
        ("geometry", None),
        ("properties", []),
        ("metadata", {}),
    ):
        feature = feature.model_copy(update={field_name: value})
        if serialized_size(feature) <= limit:
            return feature
    raise ProcessingFailure(
        "FEATURE_OUTPUT_LIMIT",
        "The feature identity and issue exceed the serialized output limit.",
        status_code=413,
    )


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _compact_generated_work(provenance: dict[str, Any] | None) -> dict[str, Any] | None:
    if provenance is None:
        return None
    compact: dict[str, Any] = {"policy": provenance.get("policy", _MEASUREMENT_POLICY)}
    generated_work = _nonnegative_int(provenance.get("generated_coordinates_total"))
    if generated_work:
        compact["generated_coordinates_total"] = generated_work
    return compact


def _wgs84_info() -> Any:
    from geo_api.schemas import CRSInfo

    return CRSInfo(authority="EPSG:4326", name="WGS 84", wkt=_WGS84.to_wkt())


def _normalized_feature(
    feature_index: int,
    source_id: str | None,
    geometry_type: str | None,
    dimensions: int | None,
    geometry: dict[str, Any] | None,
    properties: list[dict[str, Any]],
    metadata: dict[str, Any],
    measurement_status: str,
    area_m2: float | None = None,
    length_m: float | None = None,
    provenance: dict[str, Any] | None = None,
    issues: Iterable[Issue] = (),
    source_parts: list[Any] | None = None,
) -> FeatureResult:
    issue_list = list(issues)
    if source_id is not None and len(source_id) > 255:
        metadata = {**metadata, "source_id_omitted": True}
        source_id = None
        issue_list.append(
            _issue("SOURCE_ID_LIMIT", "The source identifier exceeded its storage limit.", "error")
        )
        measurement_status, area_m2, length_m = "ERROR", None, None
        provenance = _compact_generated_work(provenance)

    for field_name, value, limit in (
        ("geometry", geometry, _GEOMETRY_MAX_BYTES),
        ("source_parts", source_parts, _GEOMETRY_MAX_BYTES),
        ("properties", properties, _PROPERTIES_MAX_BYTES),
        ("metadata", metadata, _METADATA_MAX_BYTES),
    ):
        if value is None:
            continue
        try:
            size = _json_size(value)
        except (TypeError, ValueError, OverflowError):
            size = limit + 1
        if size > limit:
            code = {
                "geometry": "SOURCE_GEOMETRY_LIMIT",
                "source_parts": "SOURCE_PARTS_LIMIT",
                "properties": "PROPERTY_LIMIT_EXCEEDED",
                "metadata": "SOURCE_METADATA_LIMIT",
            }[field_name]
            message = {
                "geometry": "The source geometry exceeded its per-feature output limit.",
                "source_parts": "The source parts exceeded their per-feature output limit.",
                "properties": "The feature properties exceeded their per-feature output limit.",
                "metadata": "The source metadata exceeded its per-feature output limit.",
            }[field_name]
            issue_list.append(_issue(code, message, "error"))
            measurement_status, area_m2, length_m = "ERROR", None, None
            provenance = _compact_generated_work(provenance)
            if field_name == "geometry":
                geometry = None
                metadata = {**metadata, "geometry_omitted": True}
            elif field_name == "source_parts":
                source_parts = None
            elif field_name == "properties":
                properties = []
                metadata = {**metadata, "properties_omitted": True}
            else:
                metadata = {"source_metadata_omitted": True}

    return FeatureResult(
        feature_index=feature_index,
        source_id=source_id,
        geometry_type=geometry_type[:32] if geometry_type else None,
        dimensions=dimensions,
        geometry=geometry,
        source_parts=source_parts,
        properties=properties,
        metadata=metadata,
        measurement_status=measurement_status,
        area_m2=area_m2,
        length_m=length_m,
        provenance=provenance,
        issues=_bounded_issues(issue_list),
    )


def _measure_feature(
    feature_index: int,
    source_id: str | None,
    geometry_type: str | None,
    dimensions: int | None,
    geometry: dict[str, Any] | None,
    properties: list[dict[str, Any]],
    metadata: dict[str, Any],
    source_crs_wkt: str,
    settings: Any,
    feature_issues: Iterable[Issue] = (),
    source_parts: list[Any] | None = None,
) -> FeatureResult:
    issues = list(feature_issues)
    if any(issue.severity == "error" for issue in issues):
        return _normalized_feature(
            feature_index,
            source_id,
            geometry_type,
            dimensions,
            geometry,
            properties,
            metadata,
            "ERROR",
            issues=issues,
            source_parts=source_parts,
        )
    if any(issue.code in {"UNSUPPORTED_GEOMETRY", "UNSUPPORTED_SHAPE_TYPE"} for issue in issues):
        return _normalized_feature(
            feature_index,
            source_id,
            geometry_type,
            dimensions,
            geometry,
            properties,
            metadata,
            "UNSUPPORTED",
            issues=issues,
            source_parts=source_parts,
        )
    if (
        _payload_too_large(geometry, _GEOMETRY_MAX_BYTES)
        or _payload_too_large(source_parts, _GEOMETRY_MAX_BYTES)
        or _payload_too_large(properties, _PROPERTIES_MAX_BYTES)
        or _payload_too_large(metadata, _METADATA_MAX_BYTES)
    ):
        return _normalized_feature(
            feature_index,
            source_id,
            geometry_type,
            dimensions,
            geometry,
            properties,
            metadata,
            "ERROR",
            issues=issues,
            source_parts=source_parts,
        )
    component_count = _feature_component_count(geometry)
    if component_count > _MAX_COMPONENTS_PER_FEATURE:
        return _normalized_feature(
            feature_index,
            source_id,
            geometry_type,
            dimensions,
            None,
            properties,
            {**metadata, "component_count": component_count, "geometry_omitted": True},
            "ERROR",
            issues=[
                *issues,
                _issue("COMPONENT_LIMIT", "The feature exceeds the component limit.", "error"),
            ],
            source_parts=source_parts,
        )
    result = measure_geometry(geometry, source_crs_wkt)
    status = str(result.get("measurement_status", "ERROR"))
    raw_provenance = result.get("provenance")
    provenance = raw_provenance if isinstance(raw_provenance, dict) else None
    if status == "MEASURED" and isinstance(provenance, dict):
        generated_final = _nonnegative_int(provenance.get("generated_coordinates_final"))
        if generated_final > settings.max_generated_coordinates_per_feature:
            status = "ERROR"
            result = {
                "measurement_status": status,
                "area_m2": None,
                "length_m": None,
                "issues": [
                    {
                        "code": "GENERATED_COORDINATE_LIMIT",
                        "message": "The feature exceeded its generated-coordinate budget.",
                    }
                ],
            }
            provenance = _compact_generated_work(provenance)
    severity = "error" if status == "ERROR" else "warning"
    issues.extend(
        _issue(
            str(item.get("code", "MEASUREMENT_ISSUE")),
            str(item.get("message", "Measurement issue.")),
            severity,
        )
        for item in result.get("issues", [])
    )
    return _normalized_feature(
        feature_index,
        source_id,
        geometry_type,
        dimensions,
        geometry,
        properties,
        metadata,
        status,
        result.get("area_m2"),
        result.get("length_m"),
        provenance,
        issues,
        source_parts,
    )


def _feature_component_count(geometry: dict[str, Any] | None) -> int:
    if geometry is None:
        return 0
    if geometry.get("type") == "GeometryCollection":
        return len(geometry.get("geometries", []))
    if geometry.get("type") in {"MultiPoint", "MultiLineString", "MultiPolygon"}:
        return len(geometry.get("coordinates", []))
    return 1


def _read_kml(
    path: Path, settings: Any, source_crs_wkt: str
) -> tuple[list[FeatureResult], list[Issue]]:
    data = _read_bounded(path, settings.upload_limit_bytes)
    root, placemark_counts, total_coordinates = _parse_kml_safely(data, settings)
    kml_namespace = _namespace(root.tag)
    try:
        if _local_name(root.tag) != "kml" or kml_namespace not in {
            "",
            "http://www.opengis.net/kml/2.2",
        }:
            raise ValueError("root is not kml")
    except ProcessingFailure:
        raise
    except Exception as exc:
        raise ProcessingFailure(
            "INVALID_KML", "The KML document is not a supported KML document."
        ) from exc

    placemarks = list(_iter_elements(root, kml_namespace, "Placemark"))
    folder_paths = _placemark_folder_paths(root, placemarks, kml_namespace)
    features: list[FeatureResult] = []
    output_bytes = 0
    cumulative_final = 0
    cumulative_work = 0
    for feature_index, (element, count) in enumerate(
        zip(placemarks, placemark_counts, strict=True)
    ):
        source_id = element.get("id")
        name = _direct_text(element, "name", kml_namespace)
        description = _direct_text(element, "description", kml_namespace)
        properties, property_issues = _kml_properties(element, kml_namespace)
        metadata: dict[str, Any] = {
            "name": name,
            "description": description,
            "folder_path": folder_paths[feature_index],
            "rendering": _kml_rendering_metadata(element, kml_namespace),
        }
        if any(issue.code == "PROPERTY_LIMIT_EXCEEDED" for issue in property_issues):
            metadata["properties_omitted"] = True
        if source_id is not None:
            metadata["source_id"] = source_id
        if count > settings.max_coordinates_per_feature:
            feature = _normalized_feature(
                feature_index,
                source_id,
                _kml_declared_geometry_type(element, kml_namespace),
                3 if _placemark_has_altitude(element, kml_namespace) else 2,
                None,
                properties,
                {**metadata, "geometry_omitted": True},
                "ERROR",
                issues=[
                    *property_issues,
                    _issue(
                        "FEATURE_COORDINATE_LIMIT",
                        "The feature exceeds the coordinate limit.",
                        "error",
                    ),
                ],
            )
            output_bytes = _append_feature(features, feature, output_bytes, settings)
            continue
        # FastKML receives only an element from the tree already parsed by defusedxml.
        try:
            model = Placemark.class_from_element(
                ns=f"{{{kml_namespace}}}" if kml_namespace else "",
                element=element,
                strict=True,
            )
        except Exception:
            model = None
        if model is None:
            geometry, geometry_type, dimensions = (
                None,
                _kml_declared_geometry_type(element, kml_namespace),
                None,
            )
            issues = [
                *property_issues,
                _issue(
                    "MALFORMED_GEOMETRY", "The Placemark geometry could not be parsed.", "error"
                ),
            ]
            feature = _normalized_feature(
                feature_index,
                source_id,
                geometry_type,
                dimensions,
                geometry,
                properties,
                metadata,
                "ERROR",
                issues=issues,
            )
            output_bytes = _append_feature(features, feature, output_bytes, settings)
            continue
        geometry, geometry_type, dimensions, geometry_issues = _kml_geometry(
            model, element, kml_namespace
        )
        feature = _measure_feature(
            feature_index,
            source_id,
            geometry_type,
            dimensions,
            geometry,
            properties,
            metadata,
            source_crs_wkt,
            settings,
            [*property_issues, *geometry_issues],
        )
        if feature.provenance:
            cumulative_final += _nonnegative_int(
                feature.provenance.get("generated_coordinates_final")
            )
            cumulative_work += _nonnegative_int(
                feature.provenance.get("generated_coordinates_total")
            )
        if (
            cumulative_final > settings.max_generated_coordinates_file
            or cumulative_work > settings.max_generated_work
        ):
            raise ProcessingFailure(
                "GENERATED_COORDINATE_LIMIT",
                "The dataset exceeds its generated-coordinate budget.",
                status_code=413,
            )
        output_bytes = _append_feature(features, feature, output_bytes, settings)

    warnings: list[Issue] = []
    if not features:
        warnings.append(_issue("EMPTY_DATASET", "The KML document contains no Placemarks."))
    for element in root.iter():
        local = _local_name(element.tag)
        if _namespace(element.tag) == kml_namespace and local in {
            "GroundOverlay",
            "ScreenOverlay",
            "PhotoOverlay",
            "NetworkLinkControl",
            "Tour",
        }:
            warnings.append(
                _issue(
                    "UNSUPPORTED_DOCUMENT_CONTENT",
                    "Unsupported document-level KML content was ignored.",
                )
            )
            break
    if total_coordinates > settings.max_coordinates:
        # Normally rejected during the secure parse; retained as a defensive invariant.
        raise ProcessingFailure(
            "FILE_COORDINATE_LIMIT", "The dataset exceeds the coordinate limit.", status_code=413
        )
    return features, warnings


def _parse_kml_safely(data: bytes, settings: Any) -> tuple[Any, list[int], int]:
    stack: list[Any] = []
    placemark_counts: list[int] = []
    active_placemarks: list[list[int]] = []
    root = None
    root_namespace = ""
    element_count = 0
    placemark_total = 0
    total_coordinates = 0
    try:
        events = DefusedET.iterparse(
            io.BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        for event, element in events:
            if event == "start":
                element_count += 1
                if element_count > _XML_MAX_ELEMENTS:
                    raise ProcessingFailure(
                        "XML_ELEMENT_LIMIT",
                        "The KML document exceeds the XML element limit.",
                        status_code=413,
                    )
                if len(stack) + 1 > _XML_MAX_DEPTH:
                    raise ProcessingFailure(
                        "XML_DEPTH_LIMIT",
                        "The KML document exceeds the XML depth limit.",
                        status_code=413,
                    )
                if root is None:
                    root = element
                    root_namespace = _namespace(element.tag)
                if _namespace(element.tag) == "http://www.w3.org/2001/XInclude":
                    raise ProcessingFailure(
                        "XINCLUDE_UNSUPPORTED", "XInclude content is not supported."
                    )
                if _local_name(element.tag) == "NetworkLink":
                    raise ProcessingFailure(
                        "NETWORK_LINK_UNSUPPORTED", "KML NetworkLinks are not supported."
                    )
                if (
                    _local_name(element.tag) == "Placemark"
                    and _namespace(element.tag) != root_namespace
                ):
                    raise ProcessingFailure(
                        "INVALID_KML_NAMESPACE",
                        "A Placemark uses a namespace outside the KML document.",
                    )
                if any(len(value) > _XML_MAX_TEXT for value in element.attrib.values()):
                    raise ProcessingFailure(
                        "XML_TEXT_LIMIT",
                        "An XML attribute exceeds the text limit.",
                        status_code=413,
                    )
                if (
                    _namespace(element.tag) == root_namespace
                    and _local_name(element.tag) == "Placemark"
                ):
                    placemark_total += 1
                    if placemark_total > settings.max_features:
                        raise ProcessingFailure(
                            "FEATURE_LIMIT",
                            "The dataset exceeds the feature limit.",
                            status_code=413,
                        )
                    active_placemarks.append([0])
                stack.append(element)
                continue

            text_values = (element.text or "", element.tail or "")
            if any(len(value) > _XML_MAX_TEXT for value in text_values):
                raise ProcessingFailure(
                    "XML_TEXT_LIMIT", "An XML text value exceeds the text limit.", status_code=413
                )
            local = _local_name(element.tag)
            if local == "coordinates" and _namespace(element.tag) == root_namespace:
                count = len((element.text or "").split())
                total_coordinates += count
                if active_placemarks:
                    active_placemarks[-1][0] += count
            elif (
                local == "coord" and _namespace(element.tag) == "http://www.google.com/kml/ext/2.2"
            ):
                count = 1 if (element.text or "").strip() else 0
                total_coordinates += count
                if active_placemarks:
                    active_placemarks[-1][0] += count
            if total_coordinates > settings.max_coordinates:
                raise ProcessingFailure(
                    "FILE_COORDINATE_LIMIT",
                    "The dataset exceeds the coordinate limit.",
                    status_code=413,
                )
            if local == "Placemark" and _namespace(element.tag) == root_namespace:
                placemark_counts.append(active_placemarks.pop()[0])
            stack.pop()
        if root is None:
            raise ValueError("empty XML document")
        return root, placemark_counts, total_coordinates
    except ProcessingFailure:
        raise
    except (DefusedXmlException, DefusedET.ParseError, ValueError, TypeError) as exc:
        raise ProcessingFailure("INVALID_KML", "The KML document is malformed or unsafe.") from exc


def _namespace(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _iter_elements(root: Any, namespace: str, local_name: str) -> Iterable[Any]:
    for element in root.iter():
        if _namespace(element.tag) == namespace and _local_name(element.tag) == local_name:
            yield element


def _direct_children(element: Any, local_name: str, namespace: str) -> list[Any]:
    return [
        child
        for child in list(element)
        if _namespace(child.tag) == namespace and _local_name(child.tag) == local_name
    ]


def _direct_text(element: Any, local_name: str, namespace: str) -> str | None:
    children = _direct_children(element, local_name, namespace)
    if not children:
        return None
    return "".join(children[0].itertext())


def _placemark_folder_paths(root: Any, placemarks: list[Any], namespace: str) -> list[list[str]]:
    paths: list[list[str]] = []

    def visit(element: Any, folders: list[str]) -> None:
        for child in list(element):
            if _namespace(child.tag) != namespace:
                continue
            local = _local_name(child.tag)
            if local == "Folder":
                name = _direct_text(child, "name", namespace)
                visit(child, folders + ([name] if name else []))
            elif local == "Placemark":
                paths.append(folders)
            elif local in {"Document", "kml"}:
                visit(child, folders)

    visit(root, [])
    if len(paths) != len(placemarks):
        return [[] for _ in placemarks]
    return paths


def _kml_properties(element: Any, namespace: str) -> tuple[list[dict[str, Any]], list[Issue]]:
    properties: list[dict[str, Any]] = []
    issues: list[Issue] = []
    encoded_size = 2
    too_large = False

    def add(name: str, value: str) -> None:
        nonlocal encoded_size, too_large
        if too_large:
            return
        item = {"name": name, "value": value}
        item_size = _json_size(item) + 1
        if encoded_size + item_size > _PROPERTIES_MAX_BYTES:
            properties.clear()
            issues.clear()
            _append_bounded_issue(
                issues,
                _issue(
                    "PROPERTY_LIMIT_EXCEEDED",
                    "The feature properties exceeded their per-feature output limit.",
                    "error",
                ),
            )
            too_large = True
            return
        encoded_size += item_size
        properties.append(item)

    for extended in _direct_children(element, "ExtendedData", namespace):
        for child in list(extended):
            local = _local_name(child.tag)
            if local == "Data":
                name = child.get("name", "")
                values = _direct_children(child, "value", namespace)
                value = "".join(values[0].itertext()) if values else ""
                add(name, value)
            elif local == "SchemaData":
                for value in list(child):
                    if _local_name(value.tag) == "SimpleData":
                        add(value.get("name", ""), "".join(value.itertext()))
            else:
                _append_bounded_issue(
                    issues,
                    _issue(
                        "UNSUPPORTED_EXTENDED_DATA",
                        "An ExtendedData value was not interpreted.",
                    ),
                )
            if too_large:
                return properties, issues
    return properties, _bounded_issues(issues)


def _kml_rendering_metadata(element: Any, namespace: str) -> list[dict[str, str]]:
    allowed = {
        "address",
        "phoneNumber",
        "visibility",
        "open",
        "styleUrl",
        "extrude",
        "tessellate",
        "altitudeMode",
        "drawOrder",
    }
    values: list[dict[str, str]] = []
    encoded_size = 0
    omitted_count = 0
    for child in element.iter():
        if child is element or _local_name(child.tag) not in allowed:
            continue
        if _namespace(child.tag) not in {namespace, "http://www.google.com/kml/ext/2.2"}:
            continue
        text = "".join(child.itertext())
        if text:
            item = {"name": _local_name(child.tag), "value": text}
            item_size = _json_size(item)
            if len(values) < 128 and encoded_size + item_size <= 64 * 1024:
                values.append(item)
                encoded_size += item_size
            else:
                omitted_count += 1
    if omitted_count:
        if len(values) == 128:
            omitted_count += 1
            summary = {"name": "additional_metadata_omitted", "value": str(omitted_count)}
            values[-1] = summary
        else:
            values.append({"name": "additional_metadata_omitted", "value": str(omitted_count)})
    return values


def _kml_declared_geometry_type(element: Any, namespace: str) -> str | None:
    geometry_names = {
        "Point",
        "LineString",
        "Polygon",
        "MultiGeometry",
        "Model",
        "Track",
        "MultiTrack",
    }
    for child in list(element):
        if (
            _namespace(child.tag) in {namespace, "http://www.google.com/kml/ext/2.2"}
            and _local_name(child.tag) in geometry_names
        ):
            return _local_name(child.tag)
    return None


def _placemark_has_altitude(element: Any, namespace: str) -> bool:
    return any(
        _local_name(child.tag) in {"altitudeMode", "coord"}
        or _local_name(child.tag) == "coordinates"
        and any(token.count(",") == 2 for token in (child.text or "").split())
        for child in element.iter()
        if child is not element
        and _namespace(child.tag) in {namespace, "http://www.google.com/kml/ext/2.2"}
    )


def _kml_geometry(
    model: Any, element: Any, namespace: str
) -> tuple[dict[str, Any] | None, str | None, int | None, list[Issue]]:
    declared = _kml_declared_geometry_type(element, namespace)
    geometry_elements = _kml_direct_geometry_elements(element, namespace)
    polygon_issue = _kml_polygon_issue(element, namespace)
    if polygon_issue is not None:
        return None, declared, None, [polygon_issue]
    if any(
        _local_name(item.tag) in {"Model", "Track", "MultiTrack"}
        for geometry_element in geometry_elements
        for item in geometry_element.iter()
    ):
        return (
            None,
            declared or "UnsupportedGeometry",
            None,
            [
                _issue(
                    "UNSUPPORTED_GEOMETRY", "This KML geometry is not supported in v1.", "warning"
                )
            ],
        )
    if len(geometry_elements) > 1:
        return (
            None,
            "GeometryCollection",
            None,
            [
                _issue(
                    "UNSUPPORTED_GEOMETRY",
                    "A Placemark with multiple top-level geometries is unsupported.",
                    "warning",
                )
            ],
        )
    geometry = getattr(model, "geometry", None)
    if geometry is None:
        if declared in {"Model", "Track", "MultiTrack"}:
            return (
                None,
                declared,
                None,
                [_issue("UNSUPPORTED_GEOMETRY", "This KML geometry is not supported in v1.")],
            )
        return None, declared, None, []
    try:
        interface = geometry.__geo_interface__
        normalized = _plain_json_geometry(interface)
    except Exception:
        return (
            None,
            declared,
            None,
            [
                _issue(
                    "MALFORMED_GEOMETRY",
                    "The KML geometry could not be represented safely.",
                    "error",
                )
            ],
        )
    geometry_type = normalized.get("type")
    if geometry_type not in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        return (
            None,
            str(geometry_type) if geometry_type else declared,
            None,
            [
                _issue(
                    "UNSUPPORTED_GEOMETRY", "This KML geometry is not supported in v1.", "warning"
                )
            ],
        )
    raw_coordinate_count = _kml_geometry_coordinate_count(geometry_elements, namespace)
    if raw_coordinate_count is None or raw_coordinate_count != _geometry_coordinate_count(
        normalized
    ):
        return (
            None,
            str(geometry_type),
            None,
            [
                _issue(
                    "KML_COORDINATE_MISMATCH",
                    "The parsed KML geometry did not preserve all source coordinates.",
                    "error",
                )
            ],
        )
    if not _geometry_has_finite_coordinates(normalized):
        return (
            None,
            geometry_type,
            None,
            [_issue("NON_FINITE_COORDINATE", "Coordinates must be finite JSON numbers.", "error")],
        )
    return normalized, str(geometry_type), _dimensions(normalized), []


def _kml_polygon_issue(element: Any, namespace: str) -> Issue | None:
    for polygon in element.iter():
        if _namespace(polygon.tag) != namespace or _local_name(polygon.tag) != "Polygon":
            continue
        outers = _direct_children(polygon, "outerBoundaryIs", namespace)
        inners = _direct_children(polygon, "innerBoundaryIs", namespace)
        if len(outers) != 1:
            return _issue(
                "MALFORMED_POLYGON_BOUNDARIES",
                "A KML polygon requires exactly one explicit outer boundary.",
                "error",
            )
        for boundary in [*outers, *inners]:
            rings = _direct_children(boundary, "LinearRing", namespace)
            if len(rings) != 1:
                return _issue(
                    "MALFORMED_POLYGON_BOUNDARIES",
                    "A KML polygon boundary requires one LinearRing.",
                    "error",
                )
            values = _direct_children(rings[0], "coordinates", namespace)
            if len(values) != 1:
                return _issue(
                    "MALFORMED_POLYGON_BOUNDARIES",
                    "A KML LinearRing requires one coordinates element.",
                    "error",
                )
            positions = _parse_kml_coordinates(values[0].text or "")
            if positions is None:
                return _issue(
                    "INVALID_COORDINATE",
                    "A KML polygon ring has malformed or non-finite coordinates.",
                    "error",
                )
            if (
                len(positions) < 4
                or positions[0][:2] != positions[-1][:2]
                or len({tuple(position[:2]) for position in positions[:-1]}) < 3
            ):
                return _issue(
                    "INVALID_RING",
                    "KML polygon rings must be explicitly closed and have three distinct vertices.",
                    "error",
                )
    return None


def _kml_direct_geometry_elements(element: Any, namespace: str) -> list[Any]:
    names = {"Point", "LineString", "Polygon", "MultiGeometry", "Model", "Track", "MultiTrack"}
    return [
        child
        for child in list(element)
        if _namespace(child.tag) in {namespace, "http://www.google.com/kml/ext/2.2"}
        and _local_name(child.tag) in names
    ]


def _parse_kml_coordinates(text: str) -> list[list[float]] | None:
    positions: list[list[float]] = []
    for token in text.split():
        values = token.split(",")
        if len(values) not in {2, 3} or any(not value for value in values):
            return None
        try:
            position = [float(value) for value in values]
        except ValueError:
            return None
        if any(not math.isfinite(value) for value in position):
            return None
        positions.append(position)
    return positions


def _kml_geometry_coordinate_count(elements: list[Any], namespace: str) -> int | None:
    count = 0
    for geometry in elements:
        for element in geometry.iter():
            if _namespace(element.tag) == namespace and _local_name(element.tag) == "coordinates":
                positions = _parse_kml_coordinates(element.text or "")
                if positions is None:
                    return None
                count += len(positions)
    return count


def _geometry_coordinate_count(geometry: dict[str, Any]) -> int:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "GeometryCollection":
        return sum(_geometry_coordinate_count(item) for item in geometry.get("geometries", []))
    if geometry_type == "Point":
        return 1 if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2 else 0
    if geometry_type in {"LineString", "MultiPoint"}:
        return len(coordinates) if isinstance(coordinates, (list, tuple)) else 0
    if geometry_type in {"MultiLineString", "Polygon"}:
        return (
            sum(len(part) for part in coordinates if isinstance(part, (list, tuple)))
            if isinstance(coordinates, (list, tuple))
            else 0
        )
    if geometry_type == "MultiPolygon":
        return (
            sum(
                len(ring)
                for polygon in coordinates
                if isinstance(polygon, (list, tuple))
                for ring in polygon
                if isinstance(ring, (list, tuple))
            )
            if isinstance(coordinates, (list, tuple))
            else 0
        )
    return 0


def _plain_json_geometry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected a geometry mapping")
    geometry_type = value.get("type")
    result: dict[str, Any] = {"type": geometry_type}
    if geometry_type == "GeometryCollection":
        geometries = value.get("geometries")
        if not isinstance(geometries, (list, tuple)):
            raise ValueError("invalid geometry collection")
        result["geometries"] = [_plain_json_geometry(item) for item in geometries]
    else:
        result["coordinates"] = _plain_value(value.get("coordinates"))
    return result


def _plain_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return value
    raise ValueError("unsupported coordinate value")


def _geometry_has_finite_coordinates(geometry: dict[str, Any]) -> bool:
    if geometry.get("type") == "GeometryCollection":
        return all(
            _geometry_has_finite_coordinates(item) for item in geometry.get("geometries", [])
        )

    def finite(value: Any) -> bool:
        if isinstance(value, list):
            return all(finite(item) for item in value)
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(float(value))
        return False

    return finite(geometry.get("coordinates"))


def _dimensions(geometry: dict[str, Any]) -> int:
    coordinates = geometry.get("coordinates")
    while isinstance(coordinates, list) and coordinates:
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in coordinates
        ):
            return len(coordinates)
        coordinates = coordinates[0]
    if geometry.get("type") == "GeometryCollection":
        return max((_dimensions(item) for item in geometry.get("geometries", [])), default=2)
    return 2


def _read_shapefile_zip(
    path: Path,
    workspace: Path,
    settings: Any,
) -> tuple[list[FeatureResult], Any, list[Issue], int]:
    try:
        archive_data = _read_bounded(path, settings.upload_limit_bytes)
        archive = zipfile.ZipFile(io.BytesIO(archive_data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProcessingFailure("INVALID_ARCHIVE", "The uploaded ZIP archive is invalid.") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > settings.max_archive_entries:
            raise ProcessingFailure(
                "ARCHIVE_ENTRY_LIMIT", "The archive exceeds the entry-count limit.", status_code=413
            )
        normalized: dict[str, zipfile.ZipInfo] = {}
        for info in entries:
            safe_name, is_directory = _safe_zip_name(info)
            key = safe_name.casefold()
            if key in normalized:
                raise ProcessingFailure(
                    "DUPLICATE_ARCHIVE_PATH", "The archive contains duplicate normalized paths."
                )
            _validate_zip_entry(info, is_directory)
            normalized[key] = info

        required = _select_shapefile_components(entries)
        missing = {".shp", ".shx", ".dbf", ".prj"} - set(required)
        if ".prj" in missing:
            raise ProcessingFailure(
                "MISSING_SHAPEFILE_CRS",
                "The archive must include a readable .prj file.",
            )
        if missing:
            raise ProcessingFailure(
                "MISSING_SHAPEFILE_COMPONENT",
                "The archive must contain .shp, .shx, .dbf, and .prj files.",
            )
        component_paths: dict[str, Path] = {}
        actual_expanded = 0
        for info in entries:
            safe_name, is_directory = _safe_zip_name(info)
            if is_directory:
                # Directory payloads are unusual but still count toward the expanded-byte ceiling.
                target = None
            else:
                suffix = PurePosixPath(safe_name).name.casefold()
                ext = PurePosixPath(suffix).suffix
                target = None
                if ext in required and safe_name.casefold() == required[ext][0]:
                    target = workspace / f"component{ext}"
                    component_paths[ext] = target
            try:
                with archive.open(info, "r") as source:
                    destination = target.open("xb") if target else None
                    try:
                        entry_size = 0
                        while chunk := source.read(_ZIP_CHUNK):
                            entry_size += len(chunk)
                            actual_expanded += len(chunk)
                            if actual_expanded > settings.expanded_limit_bytes:
                                raise ProcessingFailure(
                                    "EXPANDED_ARCHIVE_LIMIT",
                                    "The archive exceeds the expanded-byte limit.",
                                    status_code=413,
                                )
                            if destination:
                                destination.write(chunk)
                        if info.file_size != entry_size:
                            raise ProcessingFailure(
                                "CORRUPT_ARCHIVE_ENTRY",
                                "An archive entry has an inconsistent size.",
                            )
                    finally:
                        if destination:
                            destination.close()
            except ProcessingFailure:
                raise
            except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
                raise ProcessingFailure(
                    "CORRUPT_ARCHIVE_ENTRY", "An archive entry could not be read safely."
                ) from exc

    prj_bytes = _read_component(
        component_paths[".prj"], 64 * 1024, "The Shapefile projection file is too large."
    )
    try:
        source_crs = CRS.from_wkt(prj_bytes.decode("utf-8", errors="strict"))
    except Exception as exc:
        raise ProcessingFailure(
            "INVALID_SHAPEFILE_CRS", "The Shapefile projection file is not valid WKT."
        ) from exc
    if (
        source_crs.is_compound
        or source_crs.is_geocentric
        or not (source_crs.is_geographic or source_crs.is_projected)
        or (source_crs.datum is not None and source_crs.datum.type_name.startswith("Dynamic"))
    ):
        raise ProcessingFailure(
            "UNSUPPORTED_SHAPEFILE_CRS",
            "Only static horizontal geographic or projected CRSs are supported.",
        )
    authority = source_crs.to_authority()
    from geo_api.schemas import CRSInfo

    crs_info = CRSInfo(
        authority=f"{authority[0]}:{authority[1]}" if authority else None,
        name=source_crs.name[:255],
        wkt=source_crs.to_wkt(),
    )
    source_crs_wkt = source_crs.to_wkt()
    encoding, encoding_assumed = _dbf_encoding(component_paths[".dbf"], component_paths.get(".cpg"))
    features, deleted_count = _read_shapefile_records(
        component_paths, source_crs, source_crs_wkt, encoding, settings
    )
    warnings: list[Issue] = []
    if encoding_assumed:
        warnings.append(
            _issue("UTF8_ENCODING_ASSUMED", "The DBF encoding was assumed to be UTF-8.")
        )
    if deleted_count:
        warnings.append(
            _issue("DELETED_DBF_RECORDS", "Deleted DBF records were excluded.", count=deleted_count)
        )
    if not features:
        warnings.append(
            _issue("EMPTY_DATASET", "The Shapefile dataset contains no active records.")
        )
    return features, crs_info, warnings, deleted_count


def _safe_zip_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    original = getattr(info, "orig_filename", info.filename)
    if "\x00" in original or original != info.filename:
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "The archive contains an invalid path.")
    raw = original.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "The archive contains an absolute path.")
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "The archive contains a traversal path.")
    if any(len(part) > 255 for part in parts):
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "An archive path component is too long.")
    if len(raw) > 4096:
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "An archive path is too long.")
    normalized = posixpath.normpath(unicodedata.normalize("NFC", raw))
    if normalized in {"", "."}:
        if info.is_dir():
            return ".", True
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "The archive contains an empty path.")
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise ProcessingFailure("UNSAFE_ARCHIVE_PATH", "The archive path escapes its root.")
    is_directory = info.is_dir() or raw.endswith("/")
    return normalized, is_directory


def _validate_zip_entry(info: zipfile.ZipInfo, is_directory: bool) -> None:
    if info.flag_bits & 0x1:
        raise ProcessingFailure(
            "ENCRYPTED_ARCHIVE_ENTRY", "Encrypted archive entries are not supported."
        )
    if info.compress_type not in _ZIP_COMPRESSION_METHODS:
        raise ProcessingFailure(
            "UNSUPPORTED_COMPRESSION", "The archive uses an unsupported compression method."
        )
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type in {stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK}:
        raise ProcessingFailure(
            "UNSAFE_ARCHIVE_ENTRY", "Links and special archive entries are not supported."
        )
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ProcessingFailure(
            "UNSAFE_ARCHIVE_ENTRY", "The archive contains an unsupported filesystem entry."
        )
    if file_type == stat.S_IFDIR and not is_directory:
        raise ProcessingFailure(
            "UNSAFE_ARCHIVE_ENTRY", "The archive contains an inconsistent directory entry."
        )
    if file_type == stat.S_IFREG and is_directory:
        raise ProcessingFailure(
            "UNSAFE_ARCHIVE_ENTRY", "The archive contains an inconsistent file entry."
        )


def _select_shapefile_components(
    entries: list[zipfile.ZipInfo],
) -> dict[str, tuple[str, zipfile.ZipInfo]]:
    files: list[tuple[str, zipfile.ZipInfo]] = []
    directories: set[str] = set()
    for info in entries:
        name, is_directory = _safe_zip_name(info)
        if is_directory:
            directories.add(name.casefold())
        else:
            files.append((name, info))
    candidates = [
        (name, info) for name, info in files if PurePosixPath(name).suffix.casefold() == ".shp"
    ]
    if len(candidates) != 1:
        code = "MULTIPLE_SHAPEFILES" if candidates else "MISSING_SHAPEFILE"
        message = "The archive must contain exactly one Shapefile dataset."
        raise ProcessingFailure(code, message)
    shp_name, _ = candidates[0]
    shp_path = PurePosixPath(shp_name)
    base = shp_path.stem.casefold()
    parent = shp_path.parent.as_posix().casefold()
    required: dict[str, tuple[str, zipfile.ZipInfo]] = {}
    recognized = {".shp", ".shx", ".dbf", ".prj", ".cpg", *_ZIP_INDEX_SUFFIXES}
    for name, info in files:
        path = PurePosixPath(name)
        parent_key = path.parent.as_posix().casefold()
        filename = path.name.casefold()
        extension = path.suffix.casefold()
        same_dataset = parent_key == parent and path.stem.casefold() == base
        if same_dataset and extension == ".xml":
            continue
        if same_dataset and extension in recognized:
            if extension in required:
                raise ProcessingFailure(
                    "DUPLICATE_SHAPEFILE_COMPONENT",
                    "The archive has duplicate Shapefile components.",
                )
            required[extension] = (name.casefold(), info)
            continue
        if same_dataset and filename == f"{base}.shp.xml":
            continue
        if filename == ".ds_store" or filename.startswith("._"):
            continue
        if (
            path.parts
            and path.parts[0].casefold() == "__macosx"
            and (filename.startswith("._") or filename == ".ds_store")
        ):
            continue
        raise ProcessingFailure(
            "UNRECOGNIZED_ARCHIVE_PAYLOAD", "The archive contains an unrecognized payload file."
        )

    # Only directory wrappers that contain the dataset and macOS metadata directories are allowed.
    for directory in directories:
        if directory == "." or directory == "__macosx" or directory.startswith("__macosx/"):
            continue
        if parent == ".":
            raise ProcessingFailure(
                "UNRECOGNIZED_ARCHIVE_DIRECTORY", "The archive contains an unrelated directory."
            )
        if (
            parent == directory
            or parent.startswith(f"{directory}/")
            or directory.startswith(f"{parent}/")
        ):
            continue
        raise ProcessingFailure(
            "UNRECOGNIZED_ARCHIVE_DIRECTORY", "The archive contains an unrelated directory."
        )
    return required


def _read_component(path: Path, limit: int, message: str) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise ProcessingFailure("COMPONENT_SIZE_LIMIT", message, status_code=413)
        data = path.read_bytes()
    except ProcessingFailure:
        raise
    except OSError as exc:
        raise ProcessingFailure(
            "MISSING_SHAPEFILE_COMPONENT", "A Shapefile component could not be read."
        ) from exc
    if len(data) > limit:
        raise ProcessingFailure("COMPONENT_SIZE_LIMIT", message, status_code=413)
    return data


def _dbf_encoding(dbf_path: Path, cpg_path: Path | None) -> tuple[str, bool]:
    try:
        with dbf_path.open("rb") as stream:
            header = stream.read(32)
    except OSError as exc:
        raise ProcessingFailure("CORRUPT_DBF", "The DBF header could not be read.") from exc
    if len(header) < 32:
        raise ProcessingFailure("CORRUPT_DBF", "The DBF header is incomplete.")
    if cpg_path is not None:
        cpg = _read_component(cpg_path, 128, "The CPG encoding declaration is too large.")
        try:
            declared = (
                cpg.removeprefix(b"\xef\xbb\xbf")
                .decode("ascii", errors="strict")
                .strip()
                .casefold()
            )
            if declared.startswith("cp") and declared[2:].isdecimal():
                declared = declared[2:]
            codec = _CPG_CODECS.get(declared)
        except UnicodeDecodeError as exc:
            raise ProcessingFailure(
                "UNSUPPORTED_DBF_ENCODING", "The CPG encoding declaration is unsupported."
            ) from exc
        if codec is None:
            raise ProcessingFailure(
                "UNSUPPORTED_DBF_ENCODING", "The CPG encoding declaration is unsupported."
            )
        return codec, False
    ldid = header[29]
    if ldid == 0:
        return "utf-8", True
    codec = _DBF_LDID_CODECS.get(ldid)
    if codec is None:
        raise ProcessingFailure(
            "UNSUPPORTED_DBF_ENCODING", "The DBF code-page declaration is unsupported."
        )
    return codec, False


def _read_shapefile_records(
    component_paths: dict[str, Path],
    source_crs: CRS,
    source_crs_wkt: str,
    encoding: str,
    settings: Any,
) -> tuple[list[FeatureResult], int]:
    try:
        reader = shapefile.Reader(
            shp=component_paths[".shp"],
            shx=component_paths[".shx"],
            dbf=component_paths[".dbf"],
            encoding=encoding,
            encodingErrors="strict",
        )
    except Exception as exc:
        raise ProcessingFailure(
            "CORRUPT_SHAPEFILE", "The Shapefile components could not be opened."
        ) from exc
    features: list[FeatureResult] = []
    output_bytes = 0
    deleted_count = 0
    original_coordinate_count = 0
    cumulative_final = 0
    cumulative_work = [0]
    try:
        record_count = reader.numRecords
        if not isinstance(record_count, int) or record_count < 0:
            raise ProcessingFailure("CORRUPT_DBF", "The DBF record count is invalid.")
        if record_count > settings.max_features:
            raise ProcessingFailure(
                "FEATURE_LIMIT", "The dataset exceeds the feature limit.", status_code=413
            )
        shape_iter = reader.iterShapes()
        record_iter = reader.iterRecords(deleted_as_None=True)
        for record_index, (shape, record) in enumerate(zip(shape_iter, record_iter, strict=True)):
            coordinates = shape.points if shape is not None else []
            coordinate_count = len(coordinates)
            original_coordinate_count += coordinate_count
            if original_coordinate_count > settings.max_coordinates:
                raise ProcessingFailure(
                    "FILE_COORDINATE_LIMIT",
                    "The dataset exceeds the coordinate limit.",
                    status_code=413,
                )
            if record is None:
                deleted_count += 1
                continue
            if shape is None:
                raise ProcessingFailure("CORRUPT_SHAPEFILE", "A Shapefile record has no geometry.")
            properties, property_issues = _shapefile_properties(reader.fields[1:], record)
            if coordinate_count > settings.max_coordinates_per_feature:
                shape_type = int(shape.shapeType)
                feature = _normalized_feature(
                    len(features),
                    str(record_index),
                    _SHAPE_KINDS.get(shape_type),
                    2 + int(shape_type in _SHAPE_Z_TYPES) + int(shape_type in _SHAPE_M_TYPES),
                    None,
                    properties,
                    {"coordinate_count": coordinate_count, "geometry_omitted": True},
                    "ERROR",
                    issues=[
                        *property_issues,
                        _issue(
                            "FEATURE_COORDINATE_LIMIT",
                            "The feature exceeds the coordinate limit.",
                            "error",
                        ),
                    ],
                )
                output_bytes = _append_feature(features, feature, output_bytes, settings)
                continue
            geometry_type, geometry, source_parts, dimensions, structure_issues = _shape_geometry(
                shape, source_crs, settings, cumulative_work
            )
            metadata: dict[str, Any] = {}
            measure_values = _shape_array(shape, "m", coordinate_count)
            if measure_values is not None and int(shape.shapeType) in (
                _SHAPE_M_TYPES | _SHAPE_Z_TYPES
            ):
                normalized_measures = [_finite_or_none(value) for value in measure_values]
                metadata["source_measure_values"] = normalized_measures
                if any(value is None for value in normalized_measures):
                    _append_bounded_issue(
                        property_issues,
                        _issue(
                            "NONFINITE_MEASURE_NULL",
                            "A non-finite Shapefile measure value was returned as null.",
                        ),
                    )
            if len(features) >= settings.max_features:
                raise ProcessingFailure(
                    "FEATURE_LIMIT", "The dataset exceeds the feature limit.", status_code=413
                )
            feature = _measure_feature(
                len(features),
                str(record_index),
                geometry_type,
                dimensions,
                geometry,
                properties,
                metadata,
                source_crs_wkt,
                settings,
                [*property_issues, *structure_issues],
                source_parts,
            )
            if feature.provenance:
                cumulative_final += _nonnegative_int(
                    feature.provenance.get("generated_coordinates_final")
                )
                cumulative_work[0] += _nonnegative_int(
                    feature.provenance.get("generated_coordinates_total")
                )
            if (
                cumulative_final > settings.max_generated_coordinates_file
                or cumulative_work[0] > settings.max_generated_work
            ):
                raise ProcessingFailure(
                    "GENERATED_COORDINATE_LIMIT",
                    "The dataset exceeds its generated-coordinate budget.",
                    status_code=413,
                )
            output_bytes = _append_feature(features, feature, output_bytes, settings)
    except ProcessingFailure:
        raise
    except (UnicodeDecodeError, shapefile.dbfFileException) as exc:
        raise ProcessingFailure(
            "DBF_DECODE_ERROR", "A DBF value could not be decoded with its declared encoding."
        ) from exc
    except Exception as exc:
        raise ProcessingFailure(
            "CORRUPT_SHAPEFILE", "The Shapefile records are malformed or inconsistent."
        ) from exc
    finally:
        reader.close()
    return features, deleted_count


def _shapefile_properties(
    fields: list[Any], record: Any
) -> tuple[list[dict[str, Any]], list[Issue]]:
    properties: list[dict[str, Any]] = []
    issues: list[Issue] = []
    for field, value in zip(fields, record, strict=True):
        name = str(field[0])
        normalized = value
        if isinstance(value, (datetime, date)):
            normalized = value.isoformat()
        elif isinstance(value, bool) or value is None or isinstance(value, str):
            pass
        elif isinstance(value, int):
            if abs(value) > 9_007_199_254_740_991:
                normalized = str(value)
                _append_bounded_issue(
                    issues,
                    _issue(
                        "INTEGER_PROPERTY_STRINGIFIED",
                        "A large integer property was returned as text.",
                    ),
                )
        elif isinstance(value, float):
            if not math.isfinite(value):
                normalized = None
                _append_bounded_issue(
                    issues,
                    _issue(
                        "NONFINITE_PROPERTY_NULL",
                        "A non-finite property value was returned as null.",
                    ),
                )
        else:
            normalized = str(value)
            _append_bounded_issue(
                issues,
                _issue("PROPERTY_STRINGIFIED", "A property value was returned as text."),
            )
        properties.append({"name": name, "value": normalized})
    return properties, _bounded_issues(issues)


class _RingBudgetError(ValueError):
    pass


def _shape_geometry(
    shape: Any, source_crs: CRS, settings: Any, cumulative_work: list[int]
) -> tuple[str | None, dict[str, Any] | None, list[Any] | None, int | None, list[Issue]]:
    shape_type = int(shape.shapeType)
    kind = _SHAPE_KINDS.get(shape_type)
    source_parts: list[Any] | None = None
    if shape_type == 0:
        return None, None, None, 2, []
    if kind is None:
        return (
            kind,
            None,
            None,
            None,
            [
                _issue(
                    "UNSUPPORTED_SHAPE_TYPE",
                    "The Shapefile geometry type is unsupported.",
                    "warning",
                )
            ],
        )
    if shape_type == 31:
        multi_patch_parts = _shape_raw_parts(shape)
        return (
            "MultiPatch",
            None,
            multi_patch_parts,
            3,
            [
                _issue(
                    "UNSUPPORTED_SHAPE_TYPE",
                    "MultiPatch geometry is not supported in v1.",
                    "warning",
                )
            ],
        )
    points = list(shape.points)
    if any(
        not isinstance(point, (list, tuple))
        or len(point) < 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in point[:2]
        )
        for point in points
    ):
        return (
            kind,
            None,
            None,
            None,
            [_issue("NON_FINITE_COORDINATE", "Coordinates must be finite source values.", "error")],
        )
    if shape_type in _SHAPE_LINE_TYPES | _SHAPE_POLYGON_TYPES:
        parts = _validated_parts(shape, len(points))
    else:
        parts = [0] if points else []
    if parts is None:
        return (
            kind,
            None,
            _shape_raw_parts(shape),
            None,
            [_issue("INVALID_PART_OFFSETS", "Shapefile part offsets are invalid.", "error")],
        )
    if shape_type in _SHAPE_MULTIPOINT_TYPES and len(points) > _MAX_COMPONENTS_PER_FEATURE:
        return (
            kind,
            None,
            None,
            2 + int(shape_type in _SHAPE_Z_TYPES) + int(shape_type in _SHAPE_M_TYPES),
            [_issue("COMPONENT_LIMIT", "The feature exceeds the component limit.", "error")],
        )
    if shape_type in _SHAPE_LINE_TYPES and len(parts) > _MAX_COMPONENTS_PER_FEATURE:
        return (
            kind,
            None,
            _shape_raw_parts(shape),
            None,
            [_issue("COMPONENT_LIMIT", "The feature exceeds the component limit.", "error")],
        )
    z_values = _shape_array(shape, "z", len(points)) if shape_type in _SHAPE_Z_TYPES else None
    m_values = (
        _shape_array(shape, "m", len(points))
        if shape_type in _SHAPE_M_TYPES or shape_type in _SHAPE_Z_TYPES
        else None
    )
    if shape_type in _SHAPE_Z_TYPES and z_values is None:
        return (
            kind,
            None,
            _shape_raw_parts(shape),
            None,
            [
                _issue(
                    "INVALID_Z_VALUES", "The Shapefile Z coordinate array is incomplete.", "error"
                )
            ],
        )
    if z_values is not None and any(_finite_or_none(value) is None for value in z_values):
        return (
            kind,
            None,
            _shape_raw_parts(shape),
            None,
            [_issue("NON_FINITE_COORDINATE", "Z coordinates must be finite.", "error")],
        )
    has_z = shape_type in _SHAPE_Z_TYPES and z_values is not None
    has_m = shape_type in _SHAPE_M_TYPES or shape_type in _SHAPE_Z_TYPES and m_values is not None
    dimensions = 2 + int(has_z) + int(has_m)
    coordinates: list[list[Any]] = [
        [
            float(point[0]),
            float(point[1]),
            *([_finite_or_none(z_values[index])] if has_z and z_values else []),
        ]
        for index, point in enumerate(points)
    ]
    if shape_type in _SHAPE_POINT_TYPES:
        if len(coordinates) != 1:
            return (
                kind,
                None,
                None,
                dimensions,
                [_issue("INVALID_POINT", "A point record must contain one coordinate.", "error")],
            )
        geometry: dict[str, Any] = {"type": "Point", "coordinates": coordinates[0]}
    elif shape_type in _SHAPE_MULTIPOINT_TYPES:
        geometry = {"type": "MultiPoint", "coordinates": coordinates}
    elif shape_type in _SHAPE_LINE_TYPES:
        lines = [
            coordinates[start:end]
            for start, end in zip(parts, [*parts[1:], len(points)], strict=True)
        ]
        if any(len(line) < 2 for line in lines):
            return (
                "MultiLineString" if len(lines) > 1 else "LineString",
                None,
                None,
                dimensions,
                [
                    _issue(
                        "INVALID_LINE_PART",
                        "A line part must contain at least two coordinates.",
                        "error",
                    )
                ],
            )
        if len(lines) == 1:
            geometry = {"type": "LineString", "coordinates": lines[0]}
        else:
            geometry = {"type": "MultiLineString", "coordinates": lines}
    else:
        rings = [
            coordinates[start:end]
            for start, end in zip(parts, [*parts[1:], len(points)], strict=True)
        ]
        source_parts = rings
        if any(
            len(ring) < 4
            or ring[0][:2] != ring[-1][:2]
            or len({tuple(p[:2]) for p in ring[:-1]}) < 3
            for ring in rings
        ):
            return (
                "Polygon",
                None,
                source_parts,
                dimensions,
                [
                    _issue(
                        "INVALID_RING",
                        "Shapefile rings must be closed and contain three distinct vertices.",
                        "error",
                    )
                ],
            )
        roles: list[tuple[str, list[list[Any]]]] = []
        for ring in rings:
            if source_crs.is_geographic and _ring_wraps_pole(ring):
                return (
                    "Polygon",
                    None,
                    source_parts,
                    dimensions,
                    [
                        _issue(
                            "AMBIGUOUS_RING_ROLE",
                            "A geographic ring winds around a pole and has ambiguous Shapefile "
                            "winding.",
                            "error",
                        )
                    ],
                )
            area = _stable_signed_area(ring)
            if area is None:
                return (
                    "Polygon",
                    None,
                    source_parts,
                    dimensions,
                    [
                        _issue(
                            "AMBIGUOUS_RING_ROLE",
                            "A ring has degenerate or unstable winding.",
                            "error",
                        )
                    ],
                )
            # ESRI Shapefile convention: clockwise shells (negative area), counter-clockwise holes.
            roles.append(("shell" if area < 0 else "hole", ring))
        if sum(role == "shell" for role, _ in roles) > _MAX_COMPONENTS_PER_FEATURE:
            return (
                "MultiPolygon",
                None,
                source_parts,
                dimensions,
                [_issue("COMPONENT_LIMIT", "The feature exceeds the component limit.", "error")],
            )
        try:
            geometry = _group_shapefile_rings(roles, source_crs, settings, cumulative_work)
        except ProcessingFailure:
            raise
        except _RingBudgetError:
            return (
                "Polygon",
                None,
                source_parts,
                dimensions,
                [
                    _issue(
                        "GENERATED_COORDINATE_LIMIT",
                        "Ring containment exceeded the feature work limit.",
                        "error",
                    )
                ],
            )
        except ValueError:
            return (
                "Polygon",
                None,
                source_parts,
                dimensions,
                [
                    _issue(
                        "AMBIGUOUS_RING_ROLE", "Shapefile ring containment is ambiguous.", "error"
                    )
                ],
            )
        except Exception:
            return (
                "Polygon",
                None,
                source_parts,
                dimensions,
                [
                    _issue(
                        "RING_TRANSFORM_FAILED",
                        "Shapefile rings could not be projected for containment checks.",
                        "error",
                    )
                ],
            )
        source_parts = None
    return str(geometry["type"]), geometry, source_parts, dimensions, []


def _shape_raw_parts(shape: Any) -> list[Any]:
    points = [list(point[:2]) for point in shape.points]
    shape_type = int(shape.shapeType)
    z_values = _shape_array(shape, "z", len(points)) if shape_type in _SHAPE_Z_TYPES else None
    if z_values is not None:
        points = [point + [_finite_or_none(z_values[index])] for index, point in enumerate(points)]
    parts = list(shape.parts)
    if not parts:
        return [points] if points else []
    return [points[start:end] for start, end in zip(parts, [*parts[1:], len(points)], strict=True)]


def _validated_parts(shape: Any, point_count: int) -> list[int] | None:
    parts = [int(value) for value in shape.parts]
    if point_count == 0:
        return [] if not parts else None
    if (
        not parts
        or parts[0] != 0
        or any(a >= b for a, b in zip(parts, parts[1:], strict=False))
        or parts[-1] >= point_count
    ):
        return None
    return parts


def _shape_array(shape: Any, name: str, size: int) -> list[Any] | None:
    try:
        values = list(getattr(shape, name))
    except (AttributeError, TypeError):
        return None
    if len(values) != size:
        return None
    return values


def _finite_or_none(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _stable_signed_area(ring: list[list[Any]]) -> float | None:
    try:
        origin_x, origin_y = float(ring[0][0]), float(ring[0][1])
        terms = []
        for first, second in zip(ring[:-1], ring[1:], strict=True):
            x1, y1 = float(first[0]) - origin_x, float(first[1]) - origin_y
            x2, y2 = float(second[0]) - origin_x, float(second[1]) - origin_y
            terms.append(x1 * y2 - x2 * y1)
        area = math.fsum(terms) / 2.0
        scale = max(
            (abs(float(p[0]) - origin_x) + abs(float(p[1]) - origin_y) for p in ring),
            default=0.0,
        )
        tolerance = math.ulp(max(scale * scale, 1.0)) * max(8, len(terms))
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(area) or abs(area) <= tolerance:
        return None
    return area


def _ring_wraps_pole(ring: list[list[Any]]) -> bool:
    longitudes = [float(point[0]) for point in ring]
    unwrapped = [longitudes[0]]
    for longitude in longitudes[1:]:
        delta = (longitude - unwrapped[-1] + 180.0) % 360.0 - 180.0
        unwrapped.append(unwrapped[-1] + delta)
    return abs(unwrapped[-1] - unwrapped[0]) >= 359.999


def _spherical_center(points: list[tuple[float, float]]) -> tuple[float, float]:
    x = math.fsum(
        math.cos(math.radians(lat)) * math.cos(math.radians(lon)) for lon, lat in points
    ) / len(points)
    y = math.fsum(
        math.cos(math.radians(lat)) * math.sin(math.radians(lon)) for lon, lat in points
    ) / len(points)
    z = math.fsum(math.sin(math.radians(lat)) for _, lat in points) / len(points)
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        raise ValueError("unstable spherical center")
    return math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))


def _group_shapefile_rings(
    roles: list[tuple[str, list[list[Any]]]],
    source_crs: CRS,
    settings: Any,
    cumulative_work: list[int],
) -> dict[str, Any]:
    shells = [ring for role, ring in roles if role == "shell"]
    holes = [ring for role, ring in roles if role == "hole"]
    if not shells:
        raise ValueError("orphan holes")
    projected_shells: list[tuple[list[list[Any]], Polygon, float]] = []
    to_wgs84 = Transformer.from_crs(
        source_crs, _WGS84, always_xy=True, allow_ballpark=False, only_best=True
    )
    cumulative_ring_work = 0

    def local_points(
        ring: Sequence[Sequence[Any]], center: tuple[float, float]
    ) -> list[list[float]]:
        nonlocal cumulative_ring_work
        densified = [[float(ring[0][0]), float(ring[0][1])]]
        for start, end in zip(ring, ring[1:], strict=False):
            azimuth, _, distance = _GEOD.inv(start[0], start[1], end[0], end[1])
            segments = max(1, math.ceil(distance / _RING_CONTAINMENT_SPACING_M))
            if cumulative_ring_work + segments > settings.max_generated_coordinates_per_feature:
                raise _RingBudgetError
            if cumulative_work[0] + segments > settings.max_generated_work:
                raise ProcessingFailure(
                    "GENERATED_WORK_LIMIT",
                    "The dataset exceeds its processing-work budget.",
                    status_code=413,
                )
            cumulative_ring_work += segments
            cumulative_work[0] += segments
            for index in range(1, segments):
                lon, lat, _ = _GEOD.fwd(start[0], start[1], azimuth, distance * index / segments)
                densified.append([float(lon), float(lat)])
            densified.append([float(end[0]), float(end[1])])
        projection = CRS.from_proj4(
            f"+proj=laea +lat_0={center[1]:.15g} +lon_0={center[0]:.15g} "
            "+datum=WGS84 +units=m +no_defs +type=crs"
        )
        transformer = Transformer.from_crs(
            _WGS84, projection, always_xy=True, allow_ballpark=False, only_best=True
        )
        xs, ys = transformer.transform(
            [p[0] for p in densified], [p[1] for p in densified], errcheck=True
        )
        projected = [[float(x), float(y)] for x, y in zip(xs, ys, strict=True)]
        if any(not math.isfinite(x) or not math.isfinite(y) for x, y in projected):
            raise ValueError("non-finite projection")
        return projected

    def geographic(ring: Sequence[Sequence[Any]]) -> list[tuple[float, float]]:
        xs, ys = to_wgs84.transform([p[0] for p in ring], [p[1] for p in ring], errcheck=True)
        points = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
        if any(
            not math.isfinite(lon)
            or not math.isfinite(lat)
            or not -180 <= lon <= 180
            or not -90 <= lat <= 90
            for lon, lat in points
        ):
            raise ValueError("invalid geographic coordinate")
        return points

    shell_geo = [geographic(ring) for ring in shells]
    hole_geo = [geographic(ring) for ring in holes]
    for ring, geo in zip(shells, shell_geo, strict=True):
        center = _spherical_center(geo[:-1])
        projected = local_points(geo, center)
        polygon = Polygon(projected)
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError("invalid shell ring")
        projected_shells.append((ring, polygon, polygon.area))
    assigned_holes: list[list[list[list[Any]]]] = [[] for _ in shells]
    for ring, geo in zip(holes, hole_geo, strict=True):
        candidates: list[tuple[float, int, list[list[Any]]]] = []
        for shell_index, (_, shell_polygon, shell_area) in enumerate(projected_shells):
            shell_geo_points = shell_geo[shell_index]
            shell_center = _spherical_center(shell_geo_points[:-1])
            hole_center = _spherical_center(geo[:-1])
            delta_lon = math.radians(hole_center[0] - shell_center[0])
            lat1, lat2 = math.radians(shell_center[1]), math.radians(hole_center[1])
            hav = (
                math.sin((lat2 - lat1) / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
            )
            if 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(hav))) > 250_000:
                continue
            projected_hole = local_points(geo, shell_center)
            hole_polygon = Polygon(projected_hole)
            if not hole_polygon.is_valid or hole_polygon.area <= 0:
                raise ValueError("invalid hole ring")
            # The shell's own ring is projected around its own center; repeat for exact same chart.
            if shell_polygon.contains(hole_polygon):
                candidates.append((shell_area, shell_index, ring))
        if not candidates:
            raise ValueError("orphan or crossing hole")
        candidates.sort(key=lambda item: item[0])
        if len(candidates) > 1 and math.isclose(
            candidates[0][0], candidates[1][0], rel_tol=1e-12, abs_tol=1e-6
        ):
            raise ValueError("ambiguous hole owner")
        assigned_holes[candidates[0][1]].append(ring)
    polygons: list[list[list[list[Any]]]] = []
    for shell_index, shell in enumerate(shells):
        rings = [shell, *assigned_holes[shell_index]]
        # GEOS containment and validity in a chart establish shell/hole relationships.
        center = _spherical_center(shell_geo[shell_index][:-1])
        projected = [
            Polygon(local_points(geographic(ring), center)).exterior.coords[:] for ring in rings
        ]
        polygon = Polygon(projected[0], projected[1:])
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError("invalid shell and hole grouping")
        polygons.append(rings)
    for index, first in enumerate(polygons):
        first_geo = geographic(first[0])
        center = _spherical_center(first_geo[:-1])
        first_shape = Polygon(
            local_points(first_geo, center),
            [local_points(geographic(r), center) for r in first[1:]],
        )
        for second in polygons[index + 1 :]:
            second_geo = geographic(second[0])
            second_center = _spherical_center(second_geo[:-1])
            delta_lon = math.radians(second_center[0] - center[0])
            lat1, lat2 = math.radians(center[1]), math.radians(second_center[1])
            hav = (
                math.sin((lat2 - lat1) / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
            )
            if 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(hav))) > 250_000:
                continue
            second_shape = Polygon(
                local_points(second_geo, center),
                [local_points(geographic(r), center) for r in second[1:]],
            )
            if first_shape.relate_pattern(second_shape, "T********"):
                raise ValueError("overlapping shell interiors")
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}
