from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_created_id", "created_at", "id"),
        CheckConstraint("format IN ('KML', 'SHAPEFILE')", name="ck_files_format"),
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_files_status"),
        CheckConstraint("upload_size > 0", name="ck_files_upload_size"),
        CheckConstraint(
            "(status = 'COMPLETED' AND feature_count IS NOT NULL "
            "AND measured_count IS NOT NULL AND not_applicable_count IS NOT NULL "
            "AND unsupported_count IS NOT NULL AND error_count IS NOT NULL "
            "AND feature_count = measured_count + not_applicable_count "
            "+ unsupported_count + error_count AND failure IS NULL) OR "
            "(status = 'FAILED' AND feature_count IS NULL AND measured_count IS NULL "
            "AND not_applicable_count IS NULL AND unsupported_count IS NULL "
            "AND error_count IS NULL AND failure IS NOT NULL)",
            name="ck_files_terminal_counts",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[str] = mapped_column(String(12), nullable=False)
    upload_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_crs_authority: Mapped[str | None] = mapped_column(String(64))
    source_crs_name: Mapped[str | None] = mapped_column(String(255))
    source_crs_wkt: Mapped[str | None] = mapped_column(Text)
    feature_count: Mapped[int | None] = mapped_column(Integer)
    measured_count: Mapped[int | None] = mapped_column(Integer)
    not_applicable_count: Mapped[int | None] = mapped_column(Integer)
    unsupported_count: Mapped[int | None] = mapped_column(Integer)
    error_count: Mapped[int | None] = mapped_column(Integer)
    deleted_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_issues: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer)
    measurement_policy: Mapped[str] = mapped_column(
        String(64), nullable=False, default="geo-api-measurement-v1"
    )
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")


class FeatureRecord(Base):
    __tablename__ = "features"
    __table_args__ = (
        CheckConstraint("feature_index >= 0", name="ck_features_index_nonnegative"),
        CheckConstraint(
            "measurement_status IN ('MEASURED', 'NOT_APPLICABLE', 'UNSUPPORTED', 'ERROR')",
            name="ck_features_measurement_status",
        ),
        CheckConstraint(
            "(measurement_status = 'MEASURED' AND "
            "((area_m2 IS NOT NULL AND length_m IS NULL) OR "
            "(area_m2 IS NULL AND length_m IS NOT NULL)) AND provenance IS NOT NULL) OR "
            "(measurement_status <> 'MEASURED' AND area_m2 IS NULL AND length_m IS NULL)",
            name="ck_features_measurement_consistency",
        ),
        CheckConstraint(
            "(area_m2 IS NULL OR (area_m2 >= 0 AND area_m2 < 'Infinity'::double precision)) "
            "AND (length_m IS NULL OR "
            "(length_m >= 0 AND length_m < 'Infinity'::double precision))",
            name="ck_features_finite_measurements",
        ),
    )

    file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    feature_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255))
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    dimensions: Mapped[int | None] = mapped_column(Integer)
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source_parts: Mapped[list[Any] | None] = mapped_column(JSON)
    properties: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    measurement_status: Mapped[str] = mapped_column(String(24), nullable=False)
    area_m2: Mapped[float | None] = mapped_column(Double)
    length_m: Mapped[float | None] = mapped_column(Double)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
