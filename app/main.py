"""FastAPI application factory and routes."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import clock
from .aggregate import ValidationProblem
from .config import Settings, settings
from .models import (
    ErrorDetail,
    HealthOut,
    P95TargetIn,
    P95TargetOut,
    PublishIn,
    PublishListEntry,
    PublishResult,
    ReportAccepted,
    ReportIn,
    ReportOut,
)
from .service import (
    NoEligibleTarget,
    ReportConflict,
    TargetConflict,
    ingest_report,
    publish,
    register_p95_target,
)
from .store import ReportRow, Store, TargetRow

logger = logging.getLogger("slo_api")

DESCRIPTION = """
线上 API 延迟 SLO 评审服务。

**核心规则**

- 每个实例独立上报直方图，桶边界可以不同；`bucket_counts` 长度必须为
  `len(bucket_boundaries)+1`（末桶为 `(最后边界, +inf)`），且
  `total_count == sum(bucket_counts)`——**缺桶不能当零**。
- 同一 `(service, instance, window)` 原样重传只收一次（`200`, `duplicate=true`）；
  内容不同则 `409` 拒收，原记录不变。
- **p95 目标由性能数据员按服务登记版本**（`POST /api/v1/p95-targets`）：
  每版带版本号与 UTC 生效时刻；登记成功的版本不可修改，同版本原样重传返回原版本、
  内容有变返回 `409`。
- 发布请求**不再接受 p95 目标入参**：系统从评审提交时已登记、且在窗口结束
  （含）前生效的版本中选出**最后生效的一版**（生效时刻最新；同时刻取最近登记）。
  没有符合条件的版本时拒绝发布（`409`），**不生成发布记录**。
- 发布按 `deadline` 只纳入 `received_at <= deadline` 的上报；未收齐的应到实例
  列入 `missing_instances`，整体状态为**待核**。缺失实例绝不按零参与合并。
- 合并时按所有实例桶边界的并集计算合并 CDF 的必含上下界；跨实例的桶不会被劈开，
  `p50/p95/p99` 返回**必含闭区间** `[lower_us, upper_us]`（`upper_us=null` 表示落在无上界溢出桶）。
- p95 目标判定：区间上界 ≤ 目标为**达标**；区间下界 > 目标为**未达标**；
  目标落在区间内或区间无上界为**待核**。所用目标版本号、目标值与达标结论随发布快照持久化。
- 发布结果不可变，带全局递增 `publish_id`，可随时按编号回查；迟到实例进入新批次。
"""


def create_app(app_settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    config = app_settings or settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "store", None) is None:
            directory = os.path.dirname(config.database_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            app.state.store = Store(config.database_path)
            app.state.owns_store = True
        yield
        if getattr(app.state, "owns_store", False):
            app.state.store.close()

    app = FastAPI(
        title=config.title,
        version=config.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        contact={"name": "Performance Data Team"},
        openapi_tags=[
            {"name": "reports", "description": "实例上报接收"},
            {"name": "p95-targets", "description": "p95 目标版本登记与查询"},
            {"name": "publish", "description": "按截止时刻发布与回查"},
            {"name": "ops", "description": "运维与探针"},
        ],
    )
    if store is not None:
        app.state.store = store
        app.state.owns_store = False

    def get_store(request: Request) -> Store:
        current: Store | None = getattr(request.app.state, "store", None)
        if current is None:  # pragma: no cover - lifespan always sets it
            raise HTTPException(status_code=503, detail="store 未初始化")
        return current

    @app.exception_handler(ValidationProblem)
    async def _validation_problem_handler(_: Request, exc: ValidationProblem) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "validation_failed", "detail": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "request_validation_failed",
                "detail": "请求体校验失败",
                "errors": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(ReportConflict)
    async def _conflict_handler(_: Request, exc: ReportConflict) -> JSONResponse:
        existing = exc.existing
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorDetail(
                error="conflicting_report",
                detail=(
                    "同一 (service, instance, window) 已存在内容不同的上报；"
                    "异文重传被拒收，原记录保持不变。该窗口的统计不会因本条被改写；"
                    "如需更正，请由发布方重新发布该窗口（新 publish_id）或使用新的窗口。"
                ),
                existing_source_batch=existing.source_batch,
                existing_fingerprint=existing.fingerprint,
            ).model_dump(),
        )

    @app.exception_handler(TargetConflict)
    async def _target_conflict_handler(_: Request, exc: TargetConflict) -> JSONResponse:
        existing = exc.existing
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorDetail(
                error="conflicting_p95_target_version",
                detail=(
                    f"服务 {existing.service} 的目标版本 {existing.version} 已登记且不可修改；"
                    f"本次提交的目标值或生效时刻与原登记不一致，已拒收，原版本保持不变。"
                    f"如需调整请使用新的版本号。"
                ),
                existing_fingerprint=existing.fingerprint,
            ).model_dump(),
        )

    @app.exception_handler(NoEligibleTarget)
    async def _no_eligible_target_handler(_: Request, exc: NoEligibleTarget) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorDetail(
                error="no_eligible_p95_target_version",
                detail=(
                    f"服务 {exc.service} 在窗口结束时刻 {exc.window_end}（含）之前"
                    "没有任何已登记且生效的 p95 目标版本，拒绝发布，未生成发布记录。"
                    "请先调用 POST /api/v1/p95-targets 登记目标版本。"
                ),
            ).model_dump(),
        )

    def _report_out(row: ReportRow) -> ReportOut:
        return ReportOut(
            report_id=row.id,
            service=row.service,
            instance=row.instance,
            window_start=row.window_start,
            window_end=row.window_end,
            source_batch=row.source_batch,
            fingerprint=row.fingerprint,
            bucket_boundaries=row.boundaries,
            bucket_counts=row.counts,
            total_count=row.total_count,
            total_sum_us=row.total_sum_us,
            collected_at=row.collected_at,
            received_at=row.received_at,
            p95_target_us=row.payload.get("p95_target_us"),
            payload=row.payload,
        )

    @app.get("/healthz", response_model=HealthOut, tags=["ops"], summary="存活探针")
    def healthz(db: Store = Depends(get_store)) -> HealthOut:
        db.list_publishes(limit=1)  # touches the DB; raises on corruption
        return HealthOut(status="ok", database=db.path, time=clock.now().isoformat())

    @app.post(
        "/api/v1/reports",
        response_model=ReportAccepted,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": ReportAccepted, "description": "原样重传，幂等去重"},
            409: {"model": ErrorDetail, "description": "同键异文，拒收"},
            422: {"model": ErrorDetail, "description": "校验失败"},
        },
        tags=["reports"],
        summary="接收一个实例的窗口直方图上报",
    )
    def submit_report(
        report: ReportIn, response: Response, db: Store = Depends(get_store)
    ) -> ReportAccepted:
        row, outcome = ingest_report(db, report)
        if outcome == "duplicate":
            response.status_code = status.HTTP_200_OK
        return ReportAccepted(
            report_id=row.id,
            duplicate=outcome == "duplicate",
            service=row.service,
            instance=row.instance,
            window_start=row.window_start,
            window_end=row.window_end,
            source_batch=row.source_batch,
            fingerprint=row.fingerprint,
            received_at=row.received_at,
        )

    def _target_out(row: TargetRow, duplicate: bool = False) -> P95TargetOut:
        return P95TargetOut(
            target_id=row.id,
            duplicate=duplicate,
            service=row.service,
            version=row.version,
            p95_target_us=row.p95_target_us,
            effective_at=row.effective_at,
            registered_at=row.registered_at,
            fingerprint=row.fingerprint,
        )

    @app.post(
        "/api/v1/p95-targets",
        response_model=P95TargetOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": P95TargetOut, "description": "同版本原样重传，返回原版本"},
            409: {"model": ErrorDetail, "description": "同版本内容有变，拒收且原版本不变"},
            422: {"model": ErrorDetail, "description": "校验失败"},
        },
        tags=["p95-targets"],
        summary="登记一版 p95 目标（按服务，带版本号与 UTC 生效时刻；登记后不可修改）",
    )
    def submit_p95_target(
        payload: P95TargetIn, response: Response, db: Store = Depends(get_store)
    ) -> P95TargetOut:
        row, outcome = register_p95_target(db, payload)
        if outcome == "duplicate":
            response.status_code = status.HTTP_200_OK
        return _target_out(row, duplicate=outcome == "duplicate")

    @app.get(
        "/api/v1/p95-targets",
        response_model=list[P95TargetOut],
        tags=["p95-targets"],
        summary="查询已登记的 p95 目标版本（可按服务过滤，按登记顺序倒序）",
    )
    def list_p95_targets(
        service: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
        db: Store = Depends(get_store),
    ) -> list[P95TargetOut]:
        return [_target_out(row) for row in db.list_p95_targets(service=service, limit=limit)]

    @app.post(
        "/api/v1/publish",
        response_model=PublishResult,
        status_code=status.HTTP_201_CREATED,
        responses={
            409: {"model": ErrorDetail, "description": "窗口结束前无已生效的目标版本，拒绝发布"},
            422: {"model": ErrorDetail, "description": "校验失败"},
        },
        tags=["publish"],
        summary="按截止时刻发布窗口 SLO 结果（目标版本自动选取，快照不可变）",
    )
    def publish_window(payload: PublishIn, db: Store = Depends(get_store)) -> PublishResult:
        result = publish(db, payload)
        logger.info(
            "published %s service=%s window=%s..%s status=%s missing=%s",
            result["publish_id"],
            result["service"],
            result["window_start"],
            result["window_end"],
            result["status"],
            result["missing_instances"],
        )
        return PublishResult(**result)

    @app.get(
        "/api/v1/published/{publish_id}",
        response_model=PublishResult,
        responses={404: {"model": ErrorDetail}},
        tags=["publish"],
        summary="按发布编号回查已发布结果（含迟到实例进入的新批次）",
    )
    def get_published(publish_id: int, db: Store = Depends(get_store)) -> PublishResult:
        record = db.get_publish(publish_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"发布编号 {publish_id} 不存在",
            )
        return PublishResult(**record["result"])

    @app.get(
        "/api/v1/published",
        response_model=list[PublishListEntry],
        tags=["publish"],
        summary="列出已发布批次（按编号倒序）",
    )
    def list_published(
        limit: int = Query(default=50, ge=1, le=500), db: Store = Depends(get_store)
    ) -> list[PublishListEntry]:
        return [PublishListEntry(**row) for row in db.list_publishes(limit=limit)]

    @app.get(
        "/api/v1/reports",
        response_model=list[ReportOut],
        tags=["reports"],
        summary="查询已接收上报（按服务 / 来源批号过滤）",
    )
    def list_reports(
        service: str | None = Query(default=None),
        source_batch: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
        db: Store = Depends(get_store),
    ) -> list[ReportOut]:
        rows = db.list_reports(service=service, source_batch=source_batch, limit=limit)
        return [_report_out(row) for row in rows]

    return app


app = create_app()
