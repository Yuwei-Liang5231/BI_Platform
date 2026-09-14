"""统一计算 API（B3；B9 增问数理解卡/执行）。

- POST /api/query/metric-value — 单值计算（含周期完整性、缓存、环比/同比）
- POST /api/query/export      — 指标按日序列导出 CSV（文件下载，不走 JSON envelope）
- POST /api/query/ask         — 问句 → 理解卡（只解析意图，不产数值）
- POST /api/query/ask/execute — 理解卡确认后执行（与看板同一计算出口）

B4 权限：需登录；受限指标在 service 层强制 403（不变式 2，与 metric 元数据
接口同一套判定——权限双出口）。问数权限继承同源（B9）。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.ask import service as ask_service
from app.domain.query import service

router = APIRouter(prefix="/query", tags=["query"])


class MetricValueRequest(BaseModel):
    """单值计算请求。metric 兼容指标 id 与 code；compare = none / mom / yoy。"""

    metric: str | int
    start: str
    end: str
    compare: str = "none"


class MetricExportRequest(BaseModel):
    metric: str | int
    start: str
    end: str


@router.post("/metric-value")
def metric_value(db: DbDep, body: MetricValueRequest, user: CurrentUser):
    """指标单值。

    契约 v2（2026-09-11）：覆盖区间与请求区间相交即返回真实值（实际是多少
    就是多少）。周期未被完整覆盖时 period_complete=false 且 data_through=
    数据实际截止日（前端标注"数据截至"，环比基期对齐到该日）；仅当区间与
    数据覆盖无交集或聚合结果为空时 value=null。旧约"不完整周期显示—"已废止。
    """
    data = service.compute_metric_value(
        db,
        metric_ref=body.metric,
        start=body.start,
        end=body.end,
        compare=body.compare,
        user=user,
    )
    return ok_response(data)


@router.post("/export")
def export_metric(db: DbDep, body: MetricExportRequest, user: CurrentUser):
    """指标按日序列导出 CSV（UTF-8 BOM，Excel 直接打开不乱码）。

    导出为文件下载，响应体是 CSV 字节流而非 ApiResponse JSON（契约例外，
    前端以 <a download> / Blob 处理）。
    """
    csv_text, filename = service.export_metric_csv(
        db, metric_ref=body.metric, start=body.start, end=body.end, user=user
    )
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- 问数（B9）


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class AskExecuteRequest(BaseModel):
    """理解卡确认后的执行请求：字段与理解卡一一对应，用户可修改后重算。"""

    model_config = ConfigDict(extra="forbid")

    metric: str | int
    start: str
    end: str
    compare: str = "none"


@router.post("/ask")
def ask(db: DbDep, body: AskRequest, user: CurrentUser):
    """问句 → 理解卡。

    LLM 只产意图（在候选指标清单内选指标、解析时间），数值一律由指标中心
    统一计算；候选只含当前用户可见的 active 指标（权限继承）；无匹配指标时
    明确返回"算不了"原因，不现场拼装查询。
    """
    return ok_response(ask_service.build_card(db, user, body.question))


@router.post("/ask/execute")
def ask_execute(db: DbDep, body: AskExecuteRequest, user: CurrentUser):
    """理解卡确认后执行。与看板完全同一计算出口（含缓存/周期完整性/环比/权限）。"""
    data = ask_service.execute_card(
        db,
        user,
        metric=body.metric,
        start=body.start,
        end=body.end,
        compare=body.compare,
    )
    return ok_response(data)
