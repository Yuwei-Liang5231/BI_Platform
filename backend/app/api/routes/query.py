"""统一计算 API（B3）。

- POST /api/query/metric-value — 单值计算（含周期完整性、缓存、环比/同比）
- POST /api/query/export      — 指标按日序列导出 CSV（文件下载，不走 JSON envelope）

B4 权限：需登录；受限指标在 service 层强制 403（不变式 2，与 metric 元数据
接口同一套判定——权限双出口）。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbDep
from app.core.response import ok_response
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

    周期完整性约定：请求范围超出数据覆盖区间时 value=null 且
    period_complete=false（前端显示"—"）；完整但区间无数据 / 比率分母为 0
    时 value=null 且 period_complete=true。两种 null 语义不同，前端按标志区分。
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
