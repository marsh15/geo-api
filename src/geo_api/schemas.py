from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Issue(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"] = "warning"
    details: dict[str, Any] = Field(default_factory=dict)


class CRSInfo(BaseModel):
    authority: str | None = None
    name: str
    wkt: str | None = None


class ProcessingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    format: Literal["KML", "SHAPEFILE"]
    status: Literal["COMPLETED", "FAILED"]
    source_crs: CRSInfo | None = None
    feature_count: int | None = Field(default=None, ge=0)
    measured_count: int | None = Field(default=None, ge=0)
    not_applicable_count: int | None = Field(default=None, ge=0)
    unsupported_count: int | None = Field(default=None, ge=0)
    error_count: int | None = Field(default=None, ge=0)
    deleted_record_count: int = Field(ge=0, default=0)
    has_issues: bool = False
    warnings: list[Issue] = Field(default_factory=list, max_length=20)
    failure: dict[str, Any] | None = None
    measurement_policy: str = "geo-api-measurement-v1"

    @model_validator(mode="after")
    def check_terminal_state(self) -> "ProcessingManifest":
        counts = (
            self.feature_count,
            self.measured_count,
            self.not_applicable_count,
            self.unsupported_count,
            self.error_count,
        )
        if self.status == "COMPLETED":
            if (
                self.source_crs is None
                or self.failure is not None
                or any(v is None for v in counts)
            ):
                raise ValueError("completed manifest requires CRS and complete counters")
            assert self.feature_count is not None
            assert self.measured_count is not None
            assert self.not_applicable_count is not None
            assert self.unsupported_count is not None
            assert self.error_count is not None
            if self.feature_count != (
                self.measured_count
                + self.not_applicable_count
                + self.unsupported_count
                + self.error_count
            ):
                raise ValueError("feature counters do not sum to feature_count")
            if (
                self.warnings or self.unsupported_count or self.error_count
            ) and not self.has_issues:
                raise ValueError("completed manifest has issues but has_issues is false")
        elif self.failure is None or any(v is not None for v in counts):
            raise ValueError("failed manifest requires an error and no feature counters")
        return self


class FeatureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_index: int = Field(ge=0)
    source_id: str | None = None
    geometry_type: str | None = None
    dimensions: int | None = Field(default=None, ge=2, le=4)
    geometry: dict[str, Any] | None = None
    source_parts: list[Any] | None = None
    properties: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    measurement_status: Literal["MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "ERROR"]
    area_m2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    length_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provenance: dict[str, Any] | None = None
    issues: list[Issue] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_measurement(self) -> "FeatureResult":
        has_measurement = self.area_m2 is not None or self.length_m is not None
        if self.measurement_status == "MEASURED":
            if (self.area_m2 is None) == (self.length_m is None) or self.provenance is None:
                raise ValueError("measured features require exactly one value and provenance")
        elif has_measurement:
            raise ValueError("unmeasured features cannot contain values")
        if self.measurement_status == "ERROR":
            for issue in self.issues:
                issue.severity = "error"
        return self


class FileSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    format: str
    status: Literal["COMPLETED", "FAILED"]
    upload_size: int
    sha256: str
    created_at: datetime
    completed_at: datetime | None
    feature_count: int | None
    measured_count: int | None
    not_applicable_count: int | None
    unsupported_count: int | None
    error_count: int | None
    deleted_record_count: int
    has_issues: bool
    crs: CRSInfo | None
    measurement_policy: str
    warnings: list[dict[str, Any]]
    failure: dict[str, Any] | None
    links: dict[str, str]
