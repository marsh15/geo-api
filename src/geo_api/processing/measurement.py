"""Bounded 2D measurements for local WGS84 geodesic geometry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from geographiclib.geodesic import Geodesic
from pyproj import CRS, Geod, Transformer, network
from shapely.geometry import LineString, Polygon

POLICY = "geo-api-measurement-v1"
MAX_RADIUS_M = 100_000.0
START_SEGMENT_M = 500.0
MAX_REFINEMENTS = 5
MAX_INPUT_COORDINATES = 20_000
MAX_COORDINATES_PER_REFINEMENT = 100_000
MAX_CUMULATIVE_COORDINATES = 2_000_000
MAX_EXTENT_SUBDIVISIONS = 100_000
_WGS84 = Geodesic.WGS84
_GEOD: Geod = CRS.from_epsg(4326).get_geod() or Geod(ellps="WGS84")

# Measurement workers must not fetch transformation grids during processing.
network.set_network_enabled(False)  # type: ignore[attr-defined]


class _MeasurementFailure(Exception):
    def __init__(self, status: str, code: str, message: str, generated_work: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.generated_work = generated_work


@dataclass
class _Component:
    kind: str
    paths: list[list[tuple[float, float]]]
    center: tuple[float, float]
    projection: CRS


def _failure(status: str, code: str, message: str, generated_work: int = 0) -> dict[str, Any]:
    provenance: dict[str, Any] = {"policy": POLICY}
    if generated_work:
        provenance["generated_coordinates_total"] = generated_work
    return {
        "measurement_status": status,
        "area_m2": None,
        "length_m": None,
        "provenance": provenance,
        "issues": [{"code": code, "message": message}],
    }


def _position(value: Any) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise _MeasurementFailure("ERROR", "INVALID_COORDINATE", "A coordinate must contain XY.")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise _MeasurementFailure("ERROR", "INVALID_COORDINATE", "Coordinates must be numeric.")
    if any(not math.isfinite(float(item)) for item in value):
        raise _MeasurementFailure("ERROR", "NON_FINITE_COORDINATE", "Coordinates must be finite.")
    return float(value[0]), float(value[1])


def _positions(path: Any, *, ring: bool = False) -> list[tuple[float, float]]:
    if not isinstance(path, (list, tuple)):
        raise _MeasurementFailure("ERROR", "INVALID_GEOMETRY", "Coordinates must be arrays.")
    result = [_position(item) for item in path]
    if ring:
        if len(result) < 4 or result[0] != result[-1]:
            raise _MeasurementFailure("ERROR", "INVALID_RING", "Polygon rings must be closed.")
        if len(set(result[:-1])) < 3:
            raise _MeasurementFailure(
                "ERROR", "INVALID_RING", "A polygon ring needs three distinct vertices."
            )
    elif len(set(result)) < 2:
        raise _MeasurementFailure("ERROR", "DEGENERATE_LINE", "A line needs two distinct vertices.")
    return result


def _validate_crs(source_crs_wkt: str) -> tuple[CRS, Transformer]:
    try:
        source = CRS.from_wkt(source_crs_wkt)
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR", "INVALID_CRS", "The source CRS is not valid WKT."
        ) from exc
    if (
        source.is_compound
        or source.is_geocentric
        or not (source.is_geographic or source.is_projected)
        or (source.datum is not None and source.datum.type_name.startswith("Dynamic"))
    ):
        raise _MeasurementFailure(
            "ERROR",
            "UNSUPPORTED_CRS",
            "Only static horizontal geographic or projected CRSs are supported.",
        )
    try:
        transformer = Transformer.from_crs(
            source,
            CRS.from_epsg(4326),
            always_xy=True,
            allow_ballpark=False,
            only_best=True,
        )
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR",
            "CRS_TRANSFORMATION_UNAVAILABLE",
            "A strict local source-to-WGS84 transformation is unavailable.",
        ) from exc
    return source, transformer


def _transform_paths(
    paths: list[list[tuple[float, float]]], transformer: Transformer
) -> list[list[tuple[float, float]]]:
    transformed: list[list[tuple[float, float]]] = []
    try:
        for path in paths:
            xs, ys = transformer.transform(
                [point[0] for point in path], [point[1] for point in path], errcheck=True
            )
            points = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
            if any(
                not math.isfinite(lon)
                or not math.isfinite(lat)
                or not -180.0 <= lon <= 180.0
                or not -90.0 <= lat <= 90.0
                for lon, lat in points
            ):
                raise _MeasurementFailure(
                    "ERROR",
                    "INVALID_WGS84_COORDINATE",
                    "The transformed coordinates are outside WGS84 bounds.",
                )
            transformed.append(points)
    except _MeasurementFailure:
        raise
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR",
            "CRS_TRANSFORMATION_FAILED",
            "The source geometry could not be transformed to WGS84.",
        ) from exc
    return transformed


def _center(points: list[tuple[float, float]]) -> tuple[float, float]:
    vectors = [
        (
            math.cos(math.radians(lat)) * math.cos(math.radians(lon)),
            math.cos(math.radians(lat)) * math.sin(math.radians(lon)),
            math.sin(math.radians(lat)),
        )
        for lon, lat in points
    ]
    x = math.fsum(vector[0] for vector in vectors) / len(vectors)
    y = math.fsum(vector[1] for vector in vectors) / len(vectors)
    z = math.fsum(vector[2] for vector in vectors) / len(vectors)
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        raise _MeasurementFailure(
            "ERROR", "UNSTABLE_COMPONENT_CENTER", "The component center is indeterminate."
        )
    x, y, z = x / norm, y / norm, z / norm
    lon = math.degrees(math.atan2(y, x)) if math.hypot(x, y) > 1e-14 else 0.0
    lat = math.degrees(math.atan2(z, math.hypot(x, y)))
    return lon, lat


def _local_projection(kind: str, center: tuple[float, float]) -> CRS:
    lon, lat = center
    proj = "laea" if kind == "Polygon" else "aeqd"
    definition = (
        f"+proj={proj} +lat_0={lat:.15g} +lon_0={lon:.15g} +datum=WGS84 +units=m +no_defs +type=crs"
    )
    try:
        return CRS.from_proj4(definition)
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR", "PROJECTION_FAILED", "A local measurement projection could not be created."
        ) from exc


def _distance(center: tuple[float, float], point: tuple[float, float]) -> float:
    return float(_GEOD.inv(center[0], center[1], point[0], point[1])[2])


def _verify_extent(
    paths: list[list[tuple[float, float]]],
    center: tuple[float, float],
    subdivision_budget: list[int],
) -> None:
    def check(start: tuple[float, float], end: tuple[float, float], depth: int) -> None:
        azimuth, _, distance = _GEOD.inv(start[0], start[1], end[0], end[1])
        if distance > 2.0 * MAX_RADIUS_M:
            raise _MeasurementFailure(
                "UNSUPPORTED",
                "COMPONENT_EXTENT_EXCEEDED",
                "The component exceeds the 100 km radius limit.",
            )
        d_start = _distance(center, start)
        d_end = _distance(center, end)
        if max(d_start, d_end) > MAX_RADIUS_M:
            raise _MeasurementFailure(
                "UNSUPPORTED",
                "COMPONENT_EXTENT_EXCEEDED",
                "The component exceeds the 100 km radius limit.",
            )
        # Triangle inequality bounds every point on the segment; subdivision tightens it.
        if max(d_start, d_end) + distance / 2.0 <= MAX_RADIUS_M - 1e-6:
            return
        if depth >= 18 or distance <= 0.001 or subdivision_budget[0] >= MAX_EXTENT_SUBDIVISIONS:
            raise _MeasurementFailure(
                "ERROR",
                "EXTENT_UNVERIFIED",
                "The 100 km component radius could not be verified within the work limit.",
            )
        subdivision_budget[0] += 1
        midpoint = _GEOD.fwd(start[0], start[1], azimuth, distance / 2.0)[:2]
        mid = (float(midpoint[0]), float(midpoint[1]))
        if _distance(center, mid) > MAX_RADIUS_M:
            raise _MeasurementFailure(
                "UNSUPPORTED",
                "COMPONENT_EXTENT_EXCEEDED",
                "The component exceeds the 100 km radius limit.",
            )
        check(start, mid, depth + 1)
        check(mid, end, depth + 1)

    for path in paths:
        for start, end in zip(path, path[1:], strict=False):
            check(start, end, 0)


def _densify(
    paths: list[list[tuple[float, float]]], max_segment_m: float
) -> tuple[list[list[tuple[float, float]]], int]:
    result: list[list[tuple[float, float]]] = []
    count = 0
    for path in paths:
        sampled = [path[0]]
        for start, end in zip(path, path[1:], strict=False):
            azimuth, _, distance = _GEOD.inv(start[0], start[1], end[0], end[1])
            segments = max(1, math.ceil(distance / max_segment_m))
            for index in range(1, segments):
                lon, lat, _ = _GEOD.fwd(start[0], start[1], azimuth, distance * index / segments)
                sampled.append((float(lon), float(lat)))
            sampled.append(end)
        count += len(sampled)
        result.append(sampled)
    return result, count


def _project_paths(
    paths: list[list[tuple[float, float]]], projection: CRS
) -> list[list[tuple[float, float]]]:
    try:
        transformer = Transformer.from_crs(
            CRS.from_epsg(4326), projection, always_xy=True, allow_ballpark=False, only_best=True
        )
        result = []
        for path in paths:
            xs, ys = transformer.transform(
                [point[0] for point in path], [point[1] for point in path], errcheck=True
            )
            points = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
            if any(not math.isfinite(x) or not math.isfinite(y) for x, y in points):
                raise ValueError("non-finite projection output")
            result.append(points)
        return result
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR", "PROJECTION_FAILED", "Coordinates could not be projected safely."
        ) from exc


def _component_measure(
    component: _Component, projected_paths: list[list[tuple[float, float]]]
) -> float:
    try:
        if component.kind == "Polygon":
            shape = Polygon(projected_paths[0], projected_paths[1:])
            value = float(shape.area)
            valid = shape.is_valid and value > 0.0
        else:
            shape = LineString(projected_paths[0])
            value = float(shape.length)
            valid = shape.is_valid and value > 0.0
    except Exception as exc:
        raise _MeasurementFailure(
            "ERROR", "INVALID_PROJECTED_GEOMETRY", "Projected geometry could not be constructed."
        ) from exc
    if not valid or not math.isfinite(value):
        raise _MeasurementFailure(
            "ERROR", "INVALID_GEOMETRY", "The projected geometry is invalid or degenerate."
        )
    return value


def _components(geometry_type: str, coordinates: Any, transformer: Transformer) -> list[_Component]:
    if geometry_type == "LineString":
        raw_paths = [[_positions(coordinates)]]
        kinds = ["LineString"]
    elif geometry_type == "MultiLineString":
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise _MeasurementFailure("ERROR", "EMPTY_GEOMETRY", "The line geometry is empty.")
        raw_paths = [[_positions(line)] for line in coordinates]
        kinds = ["LineString"] * len(raw_paths)
    elif geometry_type == "Polygon":
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise _MeasurementFailure("ERROR", "EMPTY_GEOMETRY", "The polygon geometry is empty.")
        raw_paths = [[_positions(ring, ring=True) for ring in coordinates]]
        kinds = ["Polygon"]
    else:
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise _MeasurementFailure("ERROR", "EMPTY_GEOMETRY", "The polygon geometry is empty.")
        raw_paths = []
        for polygon in coordinates:
            if not isinstance(polygon, (list, tuple)) or not polygon:
                raise _MeasurementFailure(
                    "ERROR", "EMPTY_GEOMETRY", "A polygon component is empty."
                )
            raw_paths.append([_positions(ring, ring=True) for ring in polygon])
        kinds = ["Polygon"] * len(raw_paths)

    raw_count = sum(len(path) for paths in raw_paths for path in paths)
    if raw_count > MAX_INPUT_COORDINATES:
        raise _MeasurementFailure(
            "ERROR", "INPUT_COORDINATE_LIMIT", "The feature exceeds the coordinate limit."
        )
    components: list[_Component] = []
    extent_work = [0]
    for paths, kind in zip(raw_paths, kinds, strict=True):
        transformed = _transform_paths(paths, transformer)
        center_points = transformed[0][:-1] if kind == "Polygon" else transformed[0]
        center = _center(center_points)
        try:
            _verify_extent(transformed, center, extent_work)
        except _MeasurementFailure as exc:
            exc.generated_work += extent_work[0]
            raise
        components.append(_Component(kind, transformed, center, _local_projection(kind, center)))
    return components


def _measure_components(
    components: list[_Component],
) -> tuple[list[dict[str, Any]], int, int, list[list[list[tuple[float, float]]]]]:
    previous: list[float] | None = None
    stable: list[int] = [0] * len(components)
    previous_total: float | None = None
    total_stable = 0
    final_values: list[float] = []
    final_sample_counts: list[int] = []
    final_paths: list[list[list[tuple[float, float]]]] = []
    generated_total = 0

    for refinement in range(MAX_REFINEMENTS + 1):
        spacing = START_SEGMENT_M / (2**refinement)
        values: list[float] = []
        sample_counts: list[int] = []
        level_paths: list[list[list[tuple[float, float]]]] = []
        level_count = 0
        for component in components:
            try:
                dense, count = _densify(component.paths, spacing)
            except _MeasurementFailure as exc:
                exc.generated_work += generated_total + level_count
                raise
            level_count += count
            if level_count > MAX_COORDINATES_PER_REFINEMENT:
                raise _MeasurementFailure(
                    "ERROR",
                    "GENERATED_COORDINATE_LIMIT",
                    "A refinement exceeds the generated-coordinate limit.",
                    generated_total + level_count,
                )
            try:
                projected = _project_paths(dense, component.projection)
                values.append(_component_measure(component, projected))
            except _MeasurementFailure as exc:
                exc.generated_work += generated_total + level_count
                raise
            sample_counts.append(count)
            level_paths.append(dense)
        generated_total += level_count
        if generated_total > MAX_CUMULATIVE_COORDINATES:
            raise _MeasurementFailure(
                "ERROR",
                "CUMULATIVE_COORDINATE_LIMIT",
                "The feature exceeds the cumulative refinement-work limit.",
                generated_total,
            )
        if refinement:
            assert previous is not None
            for index, (old, current, component) in enumerate(
                zip(previous, values, components, strict=True)
            ):
                if component.kind == "Polygon":
                    threshold = max(0.01, 1e-5 * current)
                else:
                    threshold = max(0.001, 1e-6 * current)
                stable[index] = stable[index] + 1 if abs(current - old) <= threshold else 0
            current_total = math.fsum(values)
            assert previous_total is not None
            total_threshold = (
                max(0.01, 1e-5 * current_total)
                if components[0].kind == "Polygon"
                else max(0.001, 1e-6 * current_total)
            )
            total_stable = (
                total_stable + 1 if abs(current_total - previous_total) <= total_threshold else 0
            )
        else:
            current_total = math.fsum(values)
        previous = values
        previous_total = current_total
        final_values = values
        final_sample_counts = sample_counts
        final_paths = level_paths
        if refinement >= 2 and total_stable >= 2 and all(value >= 2 for value in stable):
            break
    if total_stable < 2 or not all(value >= 2 for value in stable):
        raise _MeasurementFailure(
            "ERROR",
            "NUMERICAL_CONVERGENCE_FAILED",
            "The measurement did not stabilize within five refinements.",
            generated_total,
        )
    details = []
    for component, value, count in zip(components, final_values, final_sample_counts, strict=True):
        details.append(
            {
                "kind": "area" if component.kind == "Polygon" else "length",
                "value": value,
                "component_count": 1,
                "center": {"longitude": component.center[0], "latitude": component.center[1]},
                "projection_wkt": component.projection.to_wkt(),
                "edge_model": "wgs84_shortest_geodesic",
                "maximum_segment_m": START_SEGMENT_M / (2**refinement),
                "refinements": refinement,
                "generated_coordinates": count,
                "stable_transitions": 2,
            }
        )
    return details, generated_total, sum(final_sample_counts), final_paths


def _reject_multipolygon_overlaps(
    components: list[_Component], paths: list[list[list[tuple[float, float]]]]
) -> None:
    for first_index, first in enumerate(components):
        for second_index in range(first_index + 1, len(components)):
            second = components[second_index]
            if _distance(first.center, second.center) > 2.0 * MAX_RADIUS_M:
                continue
            center = _center(paths[first_index][0][:-1] + paths[second_index][0][:-1])
            projection = _local_projection("Polygon", center)
            projected = [
                _project_paths(paths[index], projection) for index in (first_index, second_index)
            ]
            shapes = [Polygon(item[0], item[1:]) for item in projected]
            if any(not shape.is_valid for shape in shapes):
                raise _MeasurementFailure(
                    "ERROR",
                    "INVALID_SHARED_TOPOLOGY",
                    "Multipart topology is invalid in a shared local chart.",
                )
            if shapes[0].relate_pattern(shapes[1], "T********"):
                raise _MeasurementFailure(
                    "ERROR",
                    "OVERLAPPING_MULTIPOLYGON_COMPONENTS",
                    "Multipolygon components have overlapping interiors.",
                )


def measure_geometry(geometry: dict[str, Any] | None, source_crs_wkt: str) -> dict[str, Any]:
    """Measure a normalized GeoJSON-style geometry using the v1 local model."""
    if geometry is None:
        return _failure("ERROR", "MISSING_GEOMETRY", "The feature has no geometry.")
    if not isinstance(geometry, dict):
        return _failure("ERROR", "INVALID_GEOMETRY", "Geometry must be a GeoJSON-style object.")
    geometry_type = geometry.get("type")
    if not isinstance(geometry_type, str):
        return _failure("ERROR", "INVALID_GEOMETRY", "Geometry must have a string type.")
    if geometry_type in {"Point", "MultiPoint"}:
        try:
            coordinates = geometry.get("coordinates")
            if geometry_type == "Point":
                points = [_position(coordinates)]
            elif not isinstance(coordinates, (list, tuple)) or not coordinates:
                return _failure("ERROR", "EMPTY_GEOMETRY", "The point geometry is empty.")
            else:
                points = [_position(point) for point in coordinates]
            _, source_to_wgs84 = _validate_crs(source_crs_wkt)
            _transform_paths([points], source_to_wgs84)
        except _MeasurementFailure as exc:
            return _failure(exc.status, exc.code, str(exc))
        return {
            "measurement_status": "NOT_APPLICABLE",
            "area_m2": None,
            "length_m": None,
            "provenance": {"policy": POLICY, "dimension": 2},
            "issues": [],
        }
    if geometry_type == "GeometryCollection":
        return _failure(
            "UNSUPPORTED",
            "MIXED_GEOMETRY_COLLECTION",
            "Geometry collections are not measured in v1.",
        )
    if geometry_type not in {"LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
        return _failure(
            "UNSUPPORTED", "UNSUPPORTED_GEOMETRY", "This geometry type is not measured in v1."
        )
    generated_total = 0
    try:
        _, transformer = _validate_crs(source_crs_wkt)
        components = _components(geometry_type, geometry.get("coordinates"), transformer)
        if not components:
            raise _MeasurementFailure(
                "ERROR", "EMPTY_GEOMETRY", "The geometry contains no components."
            )
        details, generated_total, final_count, final_paths = _measure_components(components)
        if geometry_type == "MultiPolygon":
            _reject_multipolygon_overlaps(components, final_paths)
        is_area = geometry_type in {"Polygon", "MultiPolygon"}
        total = math.fsum(detail["value"] for detail in details)
        return {
            "measurement_status": "MEASURED",
            "area_m2": total if is_area else None,
            "length_m": None if is_area else total,
            "provenance": {
                "policy": POLICY,
                "dimension": 2,
                "edge_model": "wgs84_shortest_geodesic",
                "source_crs_wkt": source_crs_wkt,
                "source_to_wgs84": {
                    "definition": transformer.definition,
                    "description": transformer.description,
                    "accuracy_m": transformer.accuracy if transformer.accuracy >= 0 else None,
                },
                "components": details,
                "generated_coordinates_total": generated_total,
                "generated_coordinates_final": final_count,
            },
            "issues": [],
        }
    except _MeasurementFailure as exc:
        return _failure(exc.status, exc.code, str(exc), generated_total + exc.generated_work)
    except Exception:
        return _failure("ERROR", "MEASUREMENT_FAILED", "The geometry could not be measured safely.")
