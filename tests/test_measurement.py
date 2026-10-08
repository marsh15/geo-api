from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from geographiclib.geodesic import Geodesic
from pyproj import CRS, Transformer

from geo_api.processing.measurement import measure_geometry

WGS84 = CRS.from_epsg(4326).to_wkt()
REFERENCE_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "geographiclib_reference.json").read_text(
        encoding="utf-8"
    )
)["references"]


def ring_area(ring: list[list[float]]) -> float:
    polygon = Geodesic.WGS84.Polygon()
    for lon, lat, *_ in ring[:-1]:
        polygon.AddPoint(lat, lon)
    return abs(polygon.Compute(False, True)[2])


def assert_area_matches(actual: float, expected: float) -> None:
    assert abs(actual - expected) <= max(0.01, 0.001 * expected)


def assert_length_matches(actual: float, expected: float) -> None:
    assert abs(actual - expected) <= max(0.001, 0.001 * expected)


def test_planar_345_line_uses_projected_input() -> None:
    source = CRS.from_epsg(32631)
    geometry = {"type": "LineString", "coordinates": [[500_000, 0], [500_003, 4]]}
    result = measure_geometry(geometry, source.to_wkt())
    # The reference is the transformed WGS84 edge model, not source-plane distance.
    to_wgs84 = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True)
    start = to_wgs84.transform(500_000, 0)
    end = to_wgs84.transform(500_003, 4)
    expected = Geodesic.WGS84.Inverse(start[1], start[0], end[1], end[0])["s12"]

    assert result["measurement_status"] == "MEASURED"
    assert result["area_m2"] is None
    assert_length_matches(result["length_m"], expected)


def test_projected_us_survey_feet_are_converted_before_measurement() -> None:
    source = CRS.from_proj4("+proj=utm +zone=15 +datum=WGS84 +units=us-ft +type=crs")
    geometry = {"type": "LineString", "coordinates": [[1_640_416.67, 0], [1_641_416.67, 1_000]]}
    to_wgs84 = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True)
    start = to_wgs84.transform(*geometry["coordinates"][0])
    end = to_wgs84.transform(*geometry["coordinates"][1])
    expected = Geodesic.WGS84.Inverse(start[1], start[0], end[1], end[0])["s12"]

    result = measure_geometry(geometry, source.to_wkt())

    assert source.axis_info[0].unit_name == "US survey foot"
    assert result["measurement_status"] == "MEASURED"
    assert_length_matches(result["length_m"], expected)


def test_missing_local_datum_grid_fails_without_a_weaker_fallback() -> None:
    source = CRS.from_epsg(4269)
    geometry = {"type": "LineString", "coordinates": [[-96.0, 38.0], [-95.999, 38.0]]}

    result = measure_geometry(geometry, source.to_wkt())

    assert result["measurement_status"] == "ERROR"
    assert result["length_m"] is None
    assert result["issues"][0]["code"] == "CRS_TRANSFORMATION_FAILED"


def test_square_with_hole_subtracts_hole_area() -> None:
    outer = [[77.59, 12.97], [77.592, 12.97], [77.592, 12.972], [77.59, 12.972], [77.59, 12.97]]
    hole = [
        [77.5905, 12.9705],
        [77.5915, 12.9705],
        [77.5915, 12.9715],
        [77.5905, 12.9715],
        [77.5905, 12.9705],
    ]
    result = measure_geometry({"type": "Polygon", "coordinates": [outer, hole]}, WGS84)

    assert result["measurement_status"] == "MEASURED"
    assert result["length_m"] is None
    assert_area_matches(result["area_m2"], ring_area(outer) - ring_area(hole))


def test_bengaluru_polygon_matches_ellipsoidal_reference() -> None:
    ring = [[77.58, 12.96], [77.59, 12.96], [77.59, 12.97], [77.58, 12.97], [77.58, 12.96]]
    result = measure_geometry({"type": "Polygon", "coordinates": [ring]}, WGS84)

    assert result["measurement_status"] == "MEASURED"
    assert_area_matches(result["area_m2"], ring_area(ring))
    component = result["provenance"]["components"][0]
    assert component["center"]["latitude"] == pytest.approx(12.965, abs=0.01)
    assert "Lambert Azimuthal Equal Area" in component["projection_wkt"]


def test_antimeridian_line_uses_shortest_geodesic() -> None:
    result = measure_geometry(
        {"type": "LineString", "coordinates": [[179.9, 0.0], [-179.9, 0.0]]}, WGS84
    )
    expected = Geodesic.WGS84.Inverse(0.0, 179.9, 0.0, -179.9)["s12"]

    assert result["measurement_status"] == "MEASURED"
    assert_length_matches(result["length_m"], expected)
    assert result["length_m"] < 23_000


def test_sparse_dense_and_reversed_geodesic_lines_agree() -> None:
    start = (179.5, 80.0)
    end = (-179.5, 80.1)
    inverse = Geodesic.WGS84.Inverse(start[1], start[0], end[1], end[0])
    midpoint = Geodesic.WGS84.Direct(start[1], start[0], inverse["azi1"], inverse["s12"] / 2)
    sparse = measure_geometry(
        {"type": "LineString", "coordinates": [list(start), list(end)]}, WGS84
    )
    dense = measure_geometry(
        {
            "type": "LineString",
            "coordinates": [
                list(start),
                [midpoint["lon2"], midpoint["lat2"]],
                list(end),
            ],
        },
        WGS84,
    )
    reversed_line = measure_geometry(
        {"type": "LineString", "coordinates": [list(end), list(start)]}, WGS84
    )

    for result in (sparse, dense, reversed_line):
        assert result["measurement_status"] == "MEASURED"
        assert_length_matches(result["length_m"], inverse["s12"])


def test_antimeridian_polygon_matches_geodesic_reference() -> None:
    ring = [[179.9, -0.05], [-179.9, -0.05], [-179.9, 0.05], [179.9, 0.05], [179.9, -0.05]]
    result = measure_geometry({"type": "Polygon", "coordinates": [ring]}, WGS84)

    assert result["measurement_status"] == "MEASURED"
    assert_area_matches(result["area_m2"], ring_area(ring))
    assert result["area_m2"] < 300_000_000


def test_polar_explicit_boundary_polygon_matches_reference() -> None:
    ring = [[-135, 89.5], [-45, 89.5], [45, 89.5], [135, 89.5], [-135, 89.5]]
    result = measure_geometry({"type": "Polygon", "coordinates": [ring]}, WGS84)

    assert result["measurement_status"] == "MEASURED"
    assert_area_matches(result["area_m2"], ring_area(ring))
    assert result["provenance"]["components"][0]["center"]["latitude"] > 89.9


def test_component_beyond_100_km_is_unsupported() -> None:
    result = measure_geometry(
        {"type": "LineString", "coordinates": [[0.0, 0.0], [2.0, 0.0]]}, WGS84
    )

    assert result["measurement_status"] == "UNSUPPORTED"
    assert result["length_m"] is None
    assert result["issues"][0]["code"] == "COMPONENT_EXTENT_EXCEEDED"


def test_extent_that_cannot_be_proven_inside_100_km_fails_closed() -> None:
    end = Geodesic.WGS84.Direct(0.0, 0.0, 90.0, 199_999.999)
    result = measure_geometry(
        {"type": "LineString", "coordinates": [[0.0, 0.0], [end["lon2"], end["lat2"]]]},
        WGS84,
    )

    assert result["measurement_status"] == "ERROR"
    assert result["length_m"] is None
    assert result["issues"][0]["code"] == "EXTENT_UNVERIFIED"


def test_overlapping_multipolygon_components_are_rejected() -> None:
    first = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    second = [[0.5, 0], [1.5, 0], [1.5, 1], [0.5, 1], [0.5, 0]]
    result = measure_geometry({"type": "MultiPolygon", "coordinates": [[first], [second]]}, WGS84)

    assert result["measurement_status"] == "ERROR"
    assert result["issues"][0]["code"] == "OVERLAPPING_MULTIPOLYGON_COMPONENTS"


def test_distant_multipolygon_components_keep_independent_measurements() -> None:
    first = [[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]
    second = [[140, -70], [140.001, -70], [140.001, -69.999], [140, -69.999], [140, -70]]
    result = measure_geometry({"type": "MultiPolygon", "coordinates": [[first], [second]]}, WGS84)

    assert result["measurement_status"] == "MEASURED"
    assert_area_matches(result["area_m2"], ring_area(first) + ring_area(second))
    assert len(result["provenance"]["components"]) == 2


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [1, 2]},
        {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
    ],
)
def test_points_are_not_applicable(geometry: dict[str, object]) -> None:
    assert measure_geometry(geometry, WGS84)["measurement_status"] == "NOT_APPLICABLE"


def test_mixed_collection_is_unsupported() -> None:
    result = measure_geometry({"type": "GeometryCollection", "geometries": []}, WGS84)
    assert result["measurement_status"] == "UNSUPPORTED"
    assert result["area_m2"] is None
    assert result["length_m"] is None


def test_points_are_not_applicable_only_after_crs_and_bounds_validation() -> None:
    outside = measure_geometry({"type": "Point", "coordinates": [181.0, 0.0]}, WGS84)
    invalid_crs = measure_geometry({"type": "Point", "coordinates": [0.0, 0.0]}, "not WKT")

    assert outside["measurement_status"] == "ERROR"
    assert outside["issues"][0]["code"] == "INVALID_WGS84_COORDINATE"
    assert invalid_crs["measurement_status"] == "ERROR"
    assert invalid_crs["issues"][0]["code"] == "INVALID_CRS"


def test_invalid_crs_and_open_ring_are_explicit_errors() -> None:
    open_ring = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0.5]]]}
    invalid = measure_geometry({"type": "LineString", "coordinates": [[0, 0], [1, 0]]}, "not WKT")
    ring_result = measure_geometry(open_ring, WGS84)

    assert invalid["measurement_status"] == "ERROR"
    assert invalid["issues"][0]["code"] == "INVALID_CRS"
    assert ring_result["measurement_status"] == "ERROR"


def test_provenance_keeps_local_projection_and_center() -> None:
    ring = [[77.59, 12.97], [77.591, 12.97], [77.591, 12.971], [77.59, 12.971], [77.59, 12.97]]
    result = measure_geometry({"type": "Polygon", "coordinates": [ring]}, WGS84)

    component = result["provenance"]["components"][0]
    assert component["projection_wkt"]
    assert component["maximum_segment_m"] <= 500
    assert component["stable_transitions"] == 2
    assert math.isfinite(component["center"]["longitude"])


@pytest.mark.parametrize("case", REFERENCE_CASES, ids=lambda case: case["name"])
def test_independent_geographiclib_reference_fixtures(case: dict[str, object]) -> None:
    result = measure_geometry(case["geometry"], WGS84)
    expected = case["reference_value"]
    actual = result["area_m2"] if case["kind"] == "area" else result["length_m"]
    assert result["measurement_status"] == "MEASURED"
    assert actual is not None
    if case["kind"] == "area":
        assert_area_matches(actual, expected)
    else:
        assert_length_matches(actual, expected)
