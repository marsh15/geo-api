from __future__ import annotations

import stat
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import shapefile  # type: ignore[import-untyped]

from geo_api.config import Settings
from geo_api.processing.errors import ProcessingFailure
from geo_api.processing.readers import process_input
from geo_api.schemas import FeatureResult, ProcessingManifest

WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


def run_input(
    path: Path, format_name: str, tmp_path: Path, **settings_overrides: Any
) -> tuple[ProcessingManifest, list[FeatureResult]]:
    settings_values: Any = {"_env_file": None, **settings_overrides}
    settings = Settings(**settings_values)
    return process_input(path, format_name, tmp_path / f"workspace-{uuid4().hex}", settings)


def write_kml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "input.kml"
    path.write_text(body, encoding="utf-8")
    return path


def write_point_shapefile(tmp_path: Path, *, rows: int = 1) -> dict[str, bytes]:
    base = tmp_path / "points"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POINT)
    writer.field("parcel", "C", size=20)
    for index in range(rows):
        writer.point(77.5 + index * 0.001, 12.9)
        writer.record(f"P-{index}")
    writer.close()
    (tmp_path / "points.prj").write_text(WGS84_WKT, encoding="ascii")
    return {
        suffix: (tmp_path / f"points{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }


def write_zip(
    tmp_path: Path, entries: dict[str, bytes], *, extra: tuple[str, bytes] | None = None
) -> Path:
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
        if extra:
            archive.writestr(*extra)
    return path


def test_kml_nested_placemark_preserves_z_and_ordered_string_properties(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>North</name>'
            '<Placemark id="point-7"><name>Marker</name>'
            "<description>untrusted description</description>"
            "<Point><coordinates>77.5,12.9,123.4</coordinates></Point><ExtendedData>"
            '<Data name="key"><value>001</value></Data>'
            '<Data name="key"><value>true</value></Data>'
            "</ExtendedData></Placemark></Folder></Document></kml>"
        ),
    )

    manifest, features = run_input(path, "KML", tmp_path)

    assert manifest.status == "COMPLETED"
    assert manifest.feature_count == 1
    feature = features[0]
    assert feature.source_id == "point-7"
    assert feature.geometry == {"type": "Point", "coordinates": [77.5, 12.9, 123.4]}
    assert feature.measurement_status == "NOT_APPLICABLE"
    assert feature.properties == [
        {"name": "key", "value": "001"},
        {"name": "key", "value": "true"},
    ]
    assert feature.metadata["folder_path"] == ["North"]
    assert feature.metadata["description"] == "untrusted description"


def test_kml_preserves_unicode_property_names_and_values(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>'
            '<ExtendedData><Data name="café"><value>東京</value></Data></ExtendedData>'
            "<Point><coordinates>77.5,12.9</coordinates></Point>"
            "</Placemark></kml>"
        ),
    )

    _, features = run_input(path, "KML", tmp_path)

    assert features[0].properties == [{"name": "café", "value": "東京"}]


def test_unparsed_kml_metadata_warns_without_suppressing_geometry_measurement(
    tmp_path: Path,
) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><ExtendedData>'
            "<ApplicationData>opaque</ApplicationData></ExtendedData><Polygon>"
            "<outerBoundaryIs><LinearRing><coordinates>"
            "77.59,12.97 77.592,12.97 77.592,12.972 77.59,12.972 77.59,12.97"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>"
        ),
    )

    manifest, features = run_input(path, "KML", tmp_path)

    assert features[0].measurement_status == "MEASURED"
    assert any(issue.code == "UNSUPPORTED_EXTENDED_DATA" for issue in features[0].issues)
    assert manifest.has_issues


def test_kml_polygon_keeps_explicit_hole_and_measures_it(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Polygon>'
            "<outerBoundaryIs><LinearRing><coordinates>"
            "77.59,12.97 77.592,12.97 77.592,12.972 77.59,12.972 77.59,12.97"
            "</coordinates></LinearRing></outerBoundaryIs>"
            "<innerBoundaryIs><LinearRing><coordinates>"
            "77.5905,12.9705 77.591,12.9705 77.591,12.971 "
            "77.5905,12.971 77.5905,12.9705"
            "</coordinates></LinearRing></innerBoundaryIs>"
            "</Polygon></Placemark></kml>"
        ),
    )

    manifest, features = run_input(path, "KML", tmp_path)

    assert manifest.feature_count == 1
    assert features[0].measurement_status == "MEASURED"
    assert features[0].geometry is not None
    assert features[0].geometry["type"] == "Polygon"
    assert len(features[0].geometry["coordinates"]) == 2
    assert features[0].area_m2 is not None and features[0].area_m2 > 0


def test_kml_open_ring_is_not_silently_closed_by_format_reader(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Polygon>'
            "<outerBoundaryIs><LinearRing><coordinates>"
            "77.59,12.97 77.592,12.97 77.592,12.972 77.59,12.972"
            "</coordinates></LinearRing></outerBoundaryIs>"
            "</Polygon></Placemark></kml>"
        ),
    )

    _, features = run_input(path, "KML", tmp_path)

    assert features[0].measurement_status == "ERROR"
    assert features[0].geometry is None
    assert features[0].issues[0].code == "INVALID_RING"


def test_kml_point_with_extra_positions_is_rejected_without_dropping_coordinates(
    tmp_path: Path,
) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point>'
            "<coordinates>77.5,12.9 77.501,12.9</coordinates>"
            "</Point></Placemark></kml>"
        ),
    )

    _, features = run_input(path, "KML", tmp_path)

    assert features[0].measurement_status == "ERROR"
    assert features[0].geometry is None
    assert features[0].issues[0].code == "KML_COORDINATE_MISMATCH"


def test_mixed_kml_multigeometry_is_preserved_but_not_measured(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><MultiGeometry>
        <Point><coordinates>77.5,12.9</coordinates></Point>
        <LineString><coordinates>77.5,12.9 77.501,12.9</coordinates></LineString>
        </MultiGeometry></Placemark></kml>""",
    )

    manifest, features = run_input(path, "KML", tmp_path)

    assert features[0].geometry is not None
    assert features[0].geometry["type"] == "GeometryCollection"
    assert features[0].measurement_status == "UNSUPPORTED"
    assert manifest.has_issues


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            '<!DOCTYPE kml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>&xxe;</name></Placemark></kml>',
            "INVALID_KML",
        ),
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><NetworkLink><Link><href>https://example.invalid/x.kml</href></Link></NetworkLink></kml>',
            "NETWORK_LINK_UNSUPPORTED",
        ),
    ],
)
def test_kml_rejects_entities_and_network_links(tmp_path: Path, body: str, code: str) -> None:
    path = write_kml(tmp_path, body)

    with pytest.raises(ProcessingFailure) as raised:
        run_input(path, "KML", tmp_path)

    assert raised.value.code == code


def test_kml_xml_depth_and_text_are_bounded(tmp_path: Path) -> None:
    deep = "<Folder>" * 65 + "</Folder>" * 65
    path = write_kml(
        tmp_path,
        f'<kml xmlns="http://www.opengis.net/kml/2.2">{deep}</kml>',
    )

    with pytest.raises(ProcessingFailure, match="depth limit"):
        run_input(path, "KML", tmp_path)


def test_kml_per_feature_coordinates_exceed_budget_without_truncation(tmp_path: Path) -> None:
    coords = " ".join(f"{index / 10000},0" for index in range(30))
    path = write_kml(
        tmp_path,
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>{coords}</coordinates></LineString></Placemark></kml>',
    )

    _, features = run_input(path, "KML", tmp_path, max_coordinates_per_feature=10)

    assert features[0].measurement_status == "ERROR"
    assert features[0].geometry is None
    assert features[0].issues[0].code == "FEATURE_COORDINATE_LIMIT"


def test_serialized_feature_limit_becomes_a_feature_error(tmp_path: Path) -> None:
    path = write_kml(
        tmp_path,
        (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark id="small">'
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            "77.59,12.97 77.592,12.97 77.592,12.972 77.59,12.972 77.59,12.97"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            "</Placemark></kml>"
        ),
    )

    manifest, features = run_input(path, "KML", tmp_path, max_feature_output_bytes=512)

    assert manifest.feature_count == 1
    assert manifest.error_count == 1
    assert features[0].measurement_status == "ERROR"
    assert features[0].source_id == "small"
    assert features[0].issues[0].code == "FEATURE_OUTPUT_LIMIT"


def test_failed_measurement_work_counts_toward_the_file_budget(tmp_path: Path) -> None:
    ring = "77.59,12.97 77.60,12.98 77.59,12.98 77.60,12.97 77.59,12.97"
    path = write_kml(
        tmp_path,
        '<kml xmlns="http://www.opengis.net/kml/2.2">'
        + "".join(
            f'<Placemark id="bad-{index}"><Polygon><outerBoundaryIs><LinearRing>'
            f"<coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
            for index in range(2)
        )
        + "</kml>",
    )

    with pytest.raises(ProcessingFailure) as raised:
        run_input(path, "KML", tmp_path, max_generated_work=1)

    assert raised.value.status_code == 413
    assert raised.value.code == "GENERATED_COORDINATE_LIMIT"


def test_shapefile_zip_reads_records_and_preserves_source_crs(tmp_path: Path) -> None:
    entries = write_point_shapefile(tmp_path, rows=2)
    path = write_zip(tmp_path, {f"wrapper/roads{suffix}": data for suffix, data in entries.items()})

    manifest, features = run_input(path, "SHAPEFILE", tmp_path)

    assert manifest.status == "COMPLETED"
    assert manifest.feature_count == 2
    assert manifest.source_crs is not None
    assert manifest.source_crs.authority == "EPSG:4326"
    assert [feature.source_id for feature in features] == ["0", "1"]
    assert features[0].measurement_status == "NOT_APPLICABLE"
    assert features[1].properties == [{"name": "parcel", "value": "P-1"}]


def test_shapefile_honors_deleted_dbf_records_and_physical_ids(tmp_path: Path) -> None:
    entries = write_point_shapefile(tmp_path, rows=2)
    dbf = bytearray(entries[".dbf"])
    header_length = int.from_bytes(dbf[8:10], "little")
    dbf[header_length] = ord("*")
    entries[".dbf"] = bytes(dbf)
    path = write_zip(tmp_path, {f"roads{suffix}": data for suffix, data in entries.items()})

    manifest, features = run_input(path, "SHAPEFILE", tmp_path)

    assert manifest.deleted_record_count == 1
    assert manifest.feature_count == 1
    assert features[0].feature_index == 0
    assert features[0].source_id == "1"


def test_shapefile_groups_clockwise_shell_and_counterclockwise_hole(tmp_path: Path) -> None:
    base = tmp_path / "polygon"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=8, decimal=0)
    shell = [(0, 0), (0, 0.02), (0.02, 0.02), (0.02, 0), (0, 0)]
    hole = [(0.005, 0.005), (0.015, 0.005), (0.015, 0.015), (0.005, 0.015), (0.005, 0.005)]
    writer.poly([shell, hole])
    writer.record(1)
    writer.close()
    (tmp_path / "polygon.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (tmp_path / f"polygon{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    path = write_zip(tmp_path, {f"polygon{suffix}": data for suffix, data in entries.items()})

    manifest, features = run_input(path, "SHAPEFILE", tmp_path)

    assert features[0].measurement_status == "MEASURED"
    assert features[0].geometry is not None
    assert features[0].geometry["type"] == "Polygon"
    assert len(features[0].geometry["coordinates"]) == 2
    assert features[0].area_m2 is not None and features[0].area_m2 > 0


def test_shapefile_ring_containment_uses_file_wide_work_budget(tmp_path: Path) -> None:
    base = tmp_path / "polygons"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=8, decimal=0)
    for identifier, longitude in enumerate((10.0, 11.0)):
        ring = [
            (longitude, 0),
            (longitude, 0.001),
            (longitude + 0.001, 0.001),
            (longitude + 0.001, 0),
            (longitude, 0),
        ]
        writer.poly([ring])
        writer.record(identifier)
    writer.close()
    (tmp_path / "polygons.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (tmp_path / f"polygons{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    path = write_zip(tmp_path, {f"polygons{suffix}": data for suffix, data in entries.items()})

    with pytest.raises(ProcessingFailure) as failure:
        run_input(path, "SHAPEFILE", tmp_path, max_generated_work=30)

    assert failure.value.code == "GENERATED_WORK_LIMIT"
    assert failure.value.status_code == 413


def test_kml_and_shapefile_equivalent_rings_measure_equivalently(tmp_path: Path) -> None:
    coordinates = [
        (77.59, 12.97),
        (77.592, 12.97),
        (77.592, 12.972),
        (77.59, 12.972),
        (77.59, 12.97),
    ]
    kml = write_kml(
        tmp_path,
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Polygon>'
        "<outerBoundaryIs><LinearRing><coordinates>"
        + " ".join(f"{lon},{lat}" for lon, lat in coordinates)
        + "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>",
    )
    shape_dir = tmp_path / "shape"
    shape_dir.mkdir()
    base = shape_dir / "plot"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=8, decimal=0)
    writer.poly([list(reversed(coordinates))])
    writer.record(1)
    writer.close()
    (shape_dir / "plot.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (shape_dir / f"plot{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    zipped = write_zip(tmp_path, {f"plot{suffix}": data for suffix, data in entries.items()})

    _, kml_features = run_input(kml, "KML", tmp_path)
    _, shape_features = run_input(zipped, "SHAPEFILE", tmp_path)
    kml_area = kml_features[0].area_m2
    shape_area = shape_features[0].area_m2

    assert kml_features[0].measurement_status == "MEASURED"
    assert shape_features[0].measurement_status == "MEASURED"
    assert kml_area is not None and shape_area is not None
    assert abs(kml_area - shape_area) <= max(0.01, 0.001 * kml_area)


def test_dateline_shapefile_shell_and_hole_match_kml(tmp_path: Path) -> None:
    shell = [[179.9, -0.05], [-179.9, -0.05], [-179.9, 0.05], [179.9, 0.05], [179.9, -0.05]]
    hole = [[179.95, -0.02], [-179.95, -0.02], [-179.95, 0.02], [179.95, 0.02], [179.95, -0.02]]

    def signed_area(ring: list[list[float]]) -> float:
        return (
            sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring[:-1], ring[1:], strict=True))
            / 2
        )

    if signed_area(shell) > 0:
        shell.reverse()
    if signed_area(hole) * signed_area(shell) > 0:
        hole.reverse()
    coordinates = " ".join(f"{lon},{lat}" for lon, lat in shell)
    hole_coordinates = " ".join(f"{lon},{lat}" for lon, lat in hole)
    kml = write_kml(
        tmp_path,
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Polygon>'
        f"<outerBoundaryIs><LinearRing><coordinates>{coordinates}</coordinates></LinearRing></outerBoundaryIs>"
        f"<innerBoundaryIs><LinearRing><coordinates>{hole_coordinates}</coordinates></LinearRing></innerBoundaryIs>"
        "</Polygon></Placemark></kml>",
    )

    shape_dir = tmp_path / "dateline-shape"
    shape_dir.mkdir()
    base = shape_dir / "dateline"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=8, decimal=0)
    writer.poly([shell, hole])
    writer.record(1)
    writer.close()
    (shape_dir / "dateline.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (shape_dir / f"dateline{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    zipped = write_zip(tmp_path, {f"dateline{suffix}": data for suffix, data in entries.items()})

    _, kml_features = run_input(kml, "KML", tmp_path)
    _, shapefile_features = run_input(zipped, "SHAPEFILE", tmp_path)
    kml_area = kml_features[0].area_m2
    shapefile_area = shapefile_features[0].area_m2

    assert kml_features[0].measurement_status == "MEASURED"
    assert shapefile_features[0].measurement_status == "MEASURED"
    assert shapefile_features[0].geometry is not None
    assert len(shapefile_features[0].geometry["coordinates"]) == 2
    assert kml_area is not None and shapefile_area is not None
    assert abs(kml_area - shapefile_area) <= max(0.01, 0.001 * kml_area)


def test_shapefile_rejects_undecodable_declared_dbf_text(tmp_path: Path) -> None:
    entries = write_point_shapefile(tmp_path)
    dbf = bytearray(entries[".dbf"])
    header_length = int.from_bytes(dbf[8:10], "little")
    dbf[header_length + 1] = 0xFF
    entries[".dbf"] = bytes(dbf)
    path = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items()} | {"roads.cpg": b"UTF-8"},
    )

    with pytest.raises(ProcessingFailure) as raised:
        run_input(path, "SHAPEFILE", tmp_path)

    assert raised.value.code == "DBF_DECODE_ERROR"


def test_shapefile_rejects_unmapped_cpg_declaration(tmp_path: Path) -> None:
    entries = write_point_shapefile(tmp_path)
    path = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items()} | {"roads.cpg": b"cp9999"},
    )

    with pytest.raises(ProcessingFailure) as raised:
        run_input(path, "SHAPEFILE", tmp_path)

    assert raised.value.code == "UNSUPPORTED_DBF_ENCODING"


def test_shapefile_decodes_declared_utf8_properties(tmp_path: Path) -> None:
    base = tmp_path / "unicode"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POINT, encoding="utf-8")
    writer.field("city", "C", size=20)
    writer.point(77.5, 12.9)
    writer.record("東京")
    writer.close()
    (tmp_path / "unicode.prj").write_text(WGS84_WKT, encoding="ascii")
    (tmp_path / "unicode.cpg").write_text("UTF-8", encoding="ascii")
    entries = {
        suffix: (tmp_path / f"unicode{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg")
    }
    path = write_zip(tmp_path, {f"unicode{suffix}": data for suffix, data in entries.items()})

    _, features = run_input(path, "SHAPEFILE", tmp_path)

    assert features[0].properties == [{"name": "city", "value": "東京"}]


def test_shapefile_keeps_z_coordinates_and_m_values_separate(tmp_path: Path) -> None:
    base = tmp_path / "pointzm"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POINTZ)
    writer.field("id", "N", size=20, decimal=0)
    writer.pointz(77.5, 12.9, 123.4, 987.6)
    writer.record(9_007_199_254_740_992)
    writer.close()
    (tmp_path / "pointzm.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (tmp_path / f"pointzm{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    path = write_zip(tmp_path, {f"pointzm{suffix}": data for suffix, data in entries.items()})

    manifest, features = run_input(path, "SHAPEFILE", tmp_path)

    assert features[0].geometry == {"type": "Point", "coordinates": [77.5, 12.9, 123.4]}
    assert features[0].dimensions == 4
    assert features[0].metadata["source_measure_values"] == [987.6]
    assert features[0].properties == [{"name": "id", "value": "9007199254740992"}]
    assert any(issue.code == "INTEGER_PROPERTY_STRINGIFIED" for issue in features[0].issues)
    assert manifest.has_issues


def test_shapefile_warning_does_not_suppress_valid_measurement(tmp_path: Path) -> None:
    base = tmp_path / "warning_polygon"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=20, decimal=0)
    writer.poly([[(0, 0), (0, 0.01), (0.01, 0.01), (0.01, 0), (0, 0)]])
    writer.record(9_007_199_254_740_992)
    writer.close()
    (tmp_path / "warning_polygon.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (tmp_path / f"warning_polygon{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    path = write_zip(
        tmp_path,
        {f"warning_polygon{suffix}": data for suffix, data in entries.items()},
    )

    manifest, features = run_input(path, "SHAPEFILE", tmp_path)

    assert features[0].measurement_status == "MEASURED"
    assert any(issue.code == "INTEGER_PROPERTY_STRINGIFIED" for issue in features[0].issues)
    assert manifest.has_issues


def test_shapefile_polar_geographic_ring_with_undefined_planar_winding_is_ambiguous(
    tmp_path: Path,
) -> None:
    base = tmp_path / "polar"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("id", "N", size=8, decimal=0)
    writer.poly([[(-135, 89.5), (-45, 89.5), (45, 89.5), (135, 89.5), (-135, 89.5)]])
    writer.record(1)
    writer.close()
    (tmp_path / "polar.prj").write_text(WGS84_WKT, encoding="ascii")
    entries = {
        suffix: (tmp_path / f"polar{suffix}").read_bytes()
        for suffix in (".shp", ".shx", ".dbf", ".prj")
    }
    path = write_zip(tmp_path, {f"polar{suffix}": data for suffix, data in entries.items()})

    _, features = run_input(path, "SHAPEFILE", tmp_path)

    assert features[0].measurement_status == "ERROR"
    assert features[0].geometry is None
    assert features[0].issues[0].code == "AMBIGUOUS_RING_ROLE"


def test_kml_multigeometry_component_limit_is_a_feature_error(tmp_path: Path) -> None:
    points = "".join(
        f"<Point><coordinates>{77.5 + index / 100000},12.9</coordinates></Point>"
        for index in range(129)
    )
    path = write_kml(
        tmp_path,
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><MultiGeometry>{points}</MultiGeometry></Placemark></kml>',
    )

    _, features = run_input(path, "KML", tmp_path)

    assert features[0].measurement_status == "ERROR"
    assert features[0].geometry is None
    assert features[0].issues[0].code == "COMPONENT_LIMIT"


@pytest.mark.parametrize("bad_name", ["../evil", "/absolute", "C:/drive", "folder\\..\\evil"])
def test_zip_rejects_traversal_and_absolute_paths(tmp_path: Path, bad_name: str) -> None:
    entries = write_point_shapefile(tmp_path)
    path = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items()},
        extra=(bad_name, b"x"),
    )

    with pytest.raises(ProcessingFailure) as raised:
        run_input(path, "SHAPEFILE", tmp_path)

    assert raised.value.code == "UNSAFE_ARCHIVE_PATH"


def test_zip_rejects_symlinks_and_casefold_duplicates(tmp_path: Path) -> None:
    entries = write_point_shapefile(tmp_path)
    path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for suffix, data in entries.items():
            archive.writestr(f"roads{suffix}", data)
        link = zipfile.ZipInfo("shortcut")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "roads.shp")
    with pytest.raises(ProcessingFailure) as symlink_error:
        run_input(path, "SHAPEFILE", tmp_path)
    assert symlink_error.value.code == "UNSAFE_ARCHIVE_ENTRY"

    path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for suffix, data in entries.items():
            archive.writestr(f"roads{suffix}", data)
        archive.writestr("ROADS.SHP", entries[".shp"])
    with pytest.raises(ProcessingFailure) as duplicate_error:
        run_input(path, "SHAPEFILE", tmp_path)
    assert duplicate_error.value.code == "DUPLICATE_ARCHIVE_PATH"


def test_zip_rejects_extra_payload_missing_projection_and_expansion_overflow(
    tmp_path: Path,
) -> None:
    entries = write_point_shapefile(tmp_path)
    extra = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items()},
        extra=("notes.txt", b"no"),
    )
    with pytest.raises(ProcessingFailure) as extra_error:
        run_input(extra, "SHAPEFILE", tmp_path)
    assert extra_error.value.code == "UNRECOGNIZED_ARCHIVE_PAYLOAD"

    missing = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items() if suffix != ".prj"},
    )
    with pytest.raises(ProcessingFailure) as missing_error:
        run_input(missing, "SHAPEFILE", tmp_path)
    assert missing_error.value.code == "MISSING_SHAPEFILE_CRS"

    large = write_zip(
        tmp_path,
        {f"roads{suffix}": data for suffix, data in entries.items()},
        extra=("__MACOSX/._roads", b"A" * 1024),
    )
    with pytest.raises(ProcessingFailure) as size_error:
        run_input(large, "SHAPEFILE", tmp_path, expanded_limit_bytes=100)
    assert size_error.value.status_code == 413
