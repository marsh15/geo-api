"""Generate independent WGS84 references for the checked-in measurement cases."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

from geographiclib.geodesic import Geodesic

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "geographiclib_reference.json"
CASES: list[dict[str, Any]] = [
    {
        "name": "bengaluru-square",
        "kind": "area",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[77.58, 12.96], [77.59, 12.96], [77.59, 12.97], [77.58, 12.97], [77.58, 12.96]]
            ],
        },
    },
    {
        "name": "antimeridian-polygon-with-hole",
        "kind": "area",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[179.9, -0.05], [-179.9, -0.05], [-179.9, 0.05], [179.9, 0.05], [179.9, -0.05]],
                [
                    [179.95, -0.02],
                    [-179.95, -0.02],
                    [-179.95, 0.02],
                    [179.95, 0.02],
                    [179.95, -0.02],
                ],
            ],
        },
    },
    {
        "name": "polar-explicit-boundary",
        "kind": "area",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-135, 89.5], [-45, 89.5], [45, 89.5], [135, 89.5], [-135, 89.5]]],
        },
    },
    {
        "name": "southern-high-latitude",
        "kind": "area",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[140, -75], [140.1, -75], [140.1, -74.9], [140, -74.9], [140, -75]]],
        },
    },
    {
        "name": "long-thin-polygon-with-narrow-hole",
        "kind": "area",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-0.8, -0.001], [0.8, -0.001], [0.8, 0.001], [-0.8, 0.001], [-0.8, -0.001]],
                [
                    [-0.0000005, -0.0005],
                    [0.0000005, -0.0005],
                    [0.0000005, 0.0005],
                    [-0.0000005, 0.0005],
                    [-0.0000005, -0.0005],
                ],
            ],
        },
    },
    {
        "name": "component-near-100km-radius",
        "kind": "length",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1.78, 0]]},
    },
    {
        "name": "off-center-tangential-line",
        "kind": "length",
        "geometry": {"type": "LineString", "coordinates": [[77.58, 12.96], [77.62, 12.99]]},
    },
    {
        "name": "antimeridian-line",
        "kind": "length",
        "geometry": {"type": "LineString", "coordinates": [[179.9, 0], [-179.9, 0]]},
    },
]


def _ring_area(ring: list[list[float]]) -> float:
    polygon = Geodesic.WGS84.Polygon()
    for lon, lat, *_ in ring[:-1]:
        polygon.AddPoint(lat, lon)
    return abs(float(polygon.Compute(False, True)[2]))


def _area(geometry: dict[str, Any]) -> float:
    coordinates = geometry["coordinates"]
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    return sum(
        _ring_area(polygon[0]) - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in polygons
    )


def _length(geometry: dict[str, Any]) -> float:
    paths = (
        [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]
    )
    return sum(
        Geodesic.WGS84.Inverse(start[1], start[0], end[1], end[0])["s12"]
        for path in paths
        for start, end in zip(path, path[1:], strict=False)
    )


def main() -> None:
    references = []
    for case in CASES:
        value = _area(case["geometry"]) if case["kind"] == "area" else _length(case["geometry"])
        references.append(
            {**case, "reference_value": value, "unit": "m2" if case["kind"] == "area" else "m"}
        )
    data = {
        "generator": "scripts/generate_reference_fixtures.py",
        "library": "GeographicLib",
        "library_version": version("geographiclib"),
        "ellipsoid": "WGS84",
        "edge_model": "wgs84_shortest_geodesic",
        "area_method": "Geodesic.WGS84.Polygon.Compute",
        "length_method": "sum of Geodesic.WGS84.Inverse segment distances",
        "references": references,
    }
    OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
