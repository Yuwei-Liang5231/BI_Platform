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
from app.domain.ask import conversations as ask_conversations
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
    # B9.2-6 会话持久化：会话 id（首问缺省自动建会话，理解卡返回 id 供后续轮携带）
    conversation_id: int | None = None


class AskExecuteRequest(BaseModel):
    """理解卡确认后的执行请求：字段与理解卡一一对应，用户可修改后重算。

    B9.2-2：dimension 非空 → 拆解出口（filters/order_by/order/top_n 生效）；
    否则单值出口。两条出口共用同一编译器/权限/缓存纪律（口径同源）。
    """

    model_config = ConfigDict(extra="forbid")

    metric: str | int
    start: str
    end: str
    compare: str = "none"
    dimension: str | None = None
    filters: list[BreakdownFilterItem] = []
    order_by: str = "value"
    order: str = "desc"
    top_n: int | None = None
    # B9.2-6：非空时结果快照回写该会话最新一轮（历史恢复可看当时真值）
    conversation_id: int | None = None


class AskDimensionValuesRequest(BaseModel):
    """问数理解卡的筛选值候选：metric + 维度列 → 该列真实取值清单。"""

    metric: str | int
    column: str


@router.post("/ask")
def ask(db: DbDep, body: AskRequest, user: CurrentUser):
    """问句 → 理解卡。

    LLM 只产意图（在候选指标清单内选指标、解析时间），数值一律由指标中心
    统一计算；候选只含当前用户可见的 active 指标（权限继承）；无匹配指标时
    明确返回"算不了"原因，不现场拼装查询。
    B9.2-3：问句命中多个可见指标时附 multi_metrics 并列清单（前端可勾选同时计算）。
    """
    return ok_response(
        ask_service.build_card(db, user, body.question, conversation_id=body.conversation_id)
    )


@router.get("/ask/conversations")
def list_ask_conversations(db: DbDep, user: CurrentUser):
    """历史会话列表（B9.2-6）：当前用户最近 50 次对话，按最近使用排序。"""
    return ok_response(ask_conversations.list_conversations(db, user))


@router.get("/ask/conversations/{conversation_id}/messages")
def ask_conversation_messages(conversation_id: int, db: DbDep, user: CurrentUser):
    """恢复会话消息（B9.2-6）：问句 + 理解卡/结果快照按序返回（只读历史）。"""
    return ok_response(ask_conversations.get_messages(db, user, conversation_id))


@router.delete("/ask/conversations/{conversation_id}")
def ask_conversation_delete(conversation_id: int, db: DbDep, user: CurrentUser):
    """删除会话及消息（B9.2-6）：仅会话归属人可删。"""
    ask_conversations.delete_conversation(db, user, conversation_id)
    return ok_response({"deleted": conversation_id})


@router.get("/ask/suggestions")
def ask_suggestions(db: DbDep, user: CurrentUser):
    """空态推荐问题（B9.2-3）：登录用户可见指标自动生成的示例问法（≤5 条）。"""
    return ok_response(ask_service.build_suggestions(db, user))


@router.post("/ask/execute")
def ask_execute(db: DbDep, body: AskExecuteRequest, user: CurrentUser):
    """理解卡确认后执行。与看板完全同一计算出口（含缓存/周期完整性/环比/权限）。

    B9.2-2：dimension 非空时走维度拆解出口（GROUP BY + 筛选 + 排序 + TopN，
    比率按组重算分子分母），数值仍由平台单点计算——AI 只产意图不产数值。
    """
    data = ask_service.execute_card(
        db,
        user,
        metric=body.metric,
        start=body.start,
        end=body.end,
        compare=body.compare,
        dimension=body.dimension,
        filters=[item.model_dump() for item in body.filters],
        order_by=body.order_by,
        order=body.order,
        top_n=body.top_n,
        conversation_id=body.conversation_id,
    )
    return ok_response(data)


@router.post("/ask/dimension-values")
def ask_dimension_values(db: DbDep, body: AskDimensionValuesRequest, user: CurrentUser):
    """理解卡筛选值候选（B9.2-2）：维度列在真实数据中的去重取值（≤50）。"""
    return ok_response(service.list_dimension_values(
        db, metric_ref=body.metric, column=body.column, user=user
    ))


# ---------------------------------------------------------------- 维度拆解（B9.2-1）


class BreakdownFilterItem(BaseModel):
    """请求级拆解过滤：op 白名单与口径 filter 文法一致（越纲由编译器判定 400）。"""

    column: str
    op: str
    value: str | int | float | bool | None = None


class BreakdownRequest(BaseModel):
    """维度拆解请求：dimension 为列名（主表直取或经显式表关系一跳解析）。"""

    metric: str | int
    start: str
    end: str
    compare: str = "none"
    dimension: str
    filters: list[BreakdownFilterItem] = []
    order_by: str = "value"   # value / change_abs / change_pct
    order: str = "desc"       # asc / desc（空值组恒排末尾）
    top_n: int | None = None  # 缺省 10，上限 50


class BreakdownDimensionsRequest(BaseModel):
    metric: str | int


@router.post("/breakdown")
def breakdown(db: DbDep, body: BreakdownRequest, user: CurrentUser):
    """维度拆解计算（B9.2-1）。

    与 metric-value 完全同源：同一编译器/权限/周期完整性契约/缓存失效机制。
    比率类指标按组重算分子分母（编译器保证，禁止对率求平均）；加性指标的
    各组值合计 ≈ 同口径单值（golden 测试守恒）。
    """
    return ok_response(service.compute_metric_breakdown(
        db,
        metric_ref=body.metric,
        start=body.start,
        end=body.end,
        compare=body.compare,
        dimension=body.dimension,
        filters=[item.model_dump() for item in body.filters],
        order_by=body.order_by,
        order=body.order,
        top_n=body.top_n,
        user=user,
    ))


@router.post("/breakdown-dimensions")
def breakdown_dimensions(db: DbDep, body: BreakdownDimensionsRequest, user: CurrentUser):
    """候选拆解维度列：主数据集与一跳可达维度表的文本列 + 现算基数
    （低基数判定线 50），供问数理解卡与前端下拉展示。"""
    return ok_response(service.list_breakdown_dimensions(db, metric_ref=body.metric, user=user))
