"""Pydantic request/response models with OpenAPI examples."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReportIn(BaseModel):
    """一个实例在一个 UTC 窗口内上报的延迟直方图。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "service": "checkout-api",
                "instance": "checkout-1",
                "window_start": "2026-11-22T00:00:00Z",
                "window_end": "2026-11-22T00:05:00Z",
                "p95_target_us": 250000,
                "source_batch": "batch-2026-11-22-a",
                "bucket_boundaries": [100000, 200000, 300000, 500000],
                "bucket_counts": [12, 30, 20, 8, 3],
                "total_count": 73,
                "total_sum_us": 14250000.0,
                "collected_at": "2026-11-22T00:06:10Z",
            }
        },
    )

    service: str = Field(min_length=1, max_length=200, description="被观测 API 服务名")
    instance: str = Field(min_length=1, max_length=200, description="实例名")
    window_start: datetime = Field(description="窗口左端（含），带时区，UTC 归一化")
    window_end: datetime = Field(description="窗口右端（不含），带时区，UTC 归一化")
    p95_target_us: float | None = Field(
        default=None, ge=0, description="该实例视角的 p95 目标（微秒），仅记录"
    )
    source_batch: str = Field(min_length=1, max_length=200, description="来源批号")
    bucket_boundaries: list[float] = Field(
        description="桶上界（微秒），严格递增；桶数为 len+1，含 (+inf) 溢出桶"
    )
    bucket_counts: list[int] = Field(description="各桶计数，长度必须为 len(bucket_boundaries)+1")
    total_count: int = Field(description="观测总条数，必须等于 bucket_counts 之和")
    total_sum_us: float = Field(description="延迟总和（微秒）")
    collected_at: datetime | None = Field(default=None, description="实例侧采集完成时刻")

    @field_validator("service", "instance", "source_batch")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不得为空白字符串")
        return stripped

    @field_validator("window_start", "window_end", "collected_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须带时区，例如 2026-11-22T00:00:00Z")
        return value


class ReportAccepted(BaseModel):
    report_id: int
    duplicate: bool = Field(description="true 表示原样重传，未新增记录")
    service: str
    instance: str
    window_start: str
    window_end: str
    source_batch: str
    fingerprint: str
    received_at: str


class ErrorDetail(BaseModel):
    error: str
    detail: str | None = None
    existing_source_batch: str | None = None
    existing_fingerprint: str | None = None


class PublishIn(BaseModel):
    """按截止时刻发布窗口统计。仅纳入 received_at <= deadline 的上报。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "service": "checkout-api",
                "window_start": "2026-11-22T00:00:00Z",
                "window_end": "2026-11-22T00:05:00Z",
                "p95_target_us": 250000,
                "expected_instances": ["checkout-1", "checkout-2", "checkout-3"],
                "deadline": "2026-11-22T00:07:00Z",
            }
        },
    )

    service: str = Field(min_length=1, max_length=200)
    window_start: datetime
    window_end: datetime
    p95_target_us: float | None = Field(default=None, ge=0, description="p95 SLO 目标（微秒）")
    expected_instances: list[str] = Field(
        min_length=1, description="应到实例清单，非空、无重复"
    )
    deadline: datetime = Field(description="发布截止时刻，带时区")

    @field_validator("service", "expected_instances")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("不得为空白字符串")
            return stripped
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            name = item.strip()
            if not name:
                raise ValueError("实例名不得为空白字符串")
            if name in seen:
                raise ValueError(f"expected_instances 存在重复实例：{name}")
            seen.add(name)
            cleaned.append(name)
        return cleaned


class QuantileInterval(BaseModel):
    lower_us: float | int = Field(description="下界：真实分位数严格大于该值")
    upper_us: float | int | None = Field(
        description="上界（含）：真实分位数不超过该值；null 表示落在无上界溢出桶"
    )
    bucket_index: int
    bucket_label: str
    rank: int = Field(description="最近秩 ceil(q * C)")
    interval_note: str


class P95Evaluation(BaseModel):
    status: Literal["meets", "misses", "pending_review"]
    status_label: str
    reason: str
    target_us: float | int | None = None
    interval_status: Literal["meets", "misses", "pending_review"] | None = Field(
        default=None, description="仅按已收实例与 p95 区间得出的原始判定；整体状态可能因缺实例降为待核"
    )


class BucketCountRange(BaseModel):
    lower: int = Field(description="该桶至少包含的观测数（所有实例都确定落入）")
    upper: int = Field(description="该桶至多包含的观测数（跨实例边界无法再细分）")


class PublishResult(BaseModel):
    publish_id: int
    service: str
    window_start: str
    window_end: str
    deadline: str
    created_at: str
    expected_instances: list[str]
    reported_instances: list[str]
    missing_instances: list[str]
    unexpected_instances: list[str]
    excluded_late_instances: list[str]
    source_batches: list[str]
    common_bucket_boundaries: list[float | int]
    merged_bucket_count_ranges: list[BucketCountRange] = Field(
        description="按共同边界的合并桶计数区间；区间宽度来自跨实例桶边界不对齐，不虚构精确值"
    )
    total_count: int
    total_sum_us: float | int
    instance_count: int
    p50: QuantileInterval | None
    p95: QuantileInterval | None
    p99: QuantileInterval | None
    p95_target_us: float | int | None
    p95_evaluation: P95Evaluation
    status: Literal["meets", "misses", "pending_review"]
    status_label: str
    notes: list[str]
    reports_used: list[dict[str, Any]]


class PublishListEntry(BaseModel):
    publish_id: int
    service: str
    window_start: str
    window_end: str
    deadline: str
    p95_target_us: float | None
    created_at: str
    status: str | None
    missing_instances: list[str]


class ReportOut(BaseModel):
    report_id: int
    service: str
    instance: str
    window_start: str
    window_end: str
    source_batch: str
    fingerprint: str
    bucket_boundaries: list[float]
    bucket_counts: list[int]
    total_count: int
    total_sum_us: float
    collected_at: str | None
    received_at: str
    p95_target_us: float | None = None
    payload: dict[str, Any]


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: str
    time: str
