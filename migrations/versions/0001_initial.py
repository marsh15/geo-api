"""Create the geo-api file and feature tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("format", sa.String(12), nullable=False),
        sa.Column("upload_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source_crs_authority", sa.String(64)),
        sa.Column("source_crs_name", sa.String(255)),
        sa.Column("source_crs_wkt", sa.Text()),
        sa.Column("feature_count", sa.Integer()),
        sa.Column("measured_count", sa.Integer()),
        sa.Column("not_applicable_count", sa.Integer()),
        sa.Column("unsupported_count", sa.Integer()),
        sa.Column("error_count", sa.Integer()),
        sa.Column("deleted_record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_issues", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("failure", postgresql.JSONB()),
        sa.Column("processing_duration_ms", sa.Integer()),
        sa.Column(
            "measurement_policy",
            sa.String(64),
            nullable=False,
            server_default="geo-api-measurement-v1",
        ),
        sa.Column("parser_version", sa.String(32), nullable=False, server_default="0.1.0"),
        sa.CheckConstraint("format IN ('KML', 'SHAPEFILE')", name="ck_files_format"),
        sa.CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_files_status"),
        sa.CheckConstraint("upload_size > 0", name="ck_files_upload_size"),
        sa.CheckConstraint(
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
    op.create_index("ix_files_created_id", "files", ["created_at", "id"])
    op.create_table(
        "features",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_index", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(255)),
        sa.Column("geometry_type", sa.String(32)),
        sa.Column("dimensions", sa.Integer()),
        sa.Column("geometry", sa.JSON()),
        sa.Column("source_parts", sa.JSON()),
        sa.Column("properties", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("measurement_status", sa.String(24), nullable=False),
        sa.Column("area_m2", sa.Double()),
        sa.Column("length_m", sa.Double()),
        sa.Column("provenance", postgresql.JSONB()),
        sa.Column(
            "issues", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "feature_index"),
        sa.CheckConstraint("feature_index >= 0", name="ck_features_index_nonnegative"),
        sa.CheckConstraint(
            "measurement_status IN ('MEASURED', 'NOT_APPLICABLE', 'UNSUPPORTED', 'ERROR')",
            name="ck_features_measurement_status",
        ),
        sa.CheckConstraint(
            "(measurement_status = 'MEASURED' AND "
            "((area_m2 IS NOT NULL AND length_m IS NULL) OR "
            "(area_m2 IS NULL AND length_m IS NOT NULL)) AND provenance IS NOT NULL) OR "
            "(measurement_status <> 'MEASURED' AND area_m2 IS NULL AND length_m IS NULL)",
            name="ck_features_measurement_consistency",
        ),
        sa.CheckConstraint(
            "(area_m2 IS NULL OR (area_m2 >= 0 AND area_m2 < 'Infinity'::double precision)) "
            "AND (length_m IS NULL OR "
            "(length_m >= 0 AND length_m < 'Infinity'::double precision))",
            name="ck_features_finite_measurements",
        ),
    )


def downgrade() -> None:
    op.drop_table("features")
    op.drop_index("ix_files_created_id", table_name="files")
    op.drop_table("files")
