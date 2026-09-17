"""自动建模建议路由（B13-1 预研 spike）：只出建议，绝不自动入库。"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.modeling import service as modeling_service

router = APIRouter(prefix="/modeling", tags=["modeling"])


class SuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int | None = None


@router.post("/suggestions")
def modeling_suggestions(db: DbDep, body: SuggestionRequest, user: CurrentUser):
    """表关系 + 指标候选建议（行业无关、可解释、含证据分）。

    铁律：只读建议接口——本路由不写任何业务表。
    """
    return ok_response(modeling_service.all_suggestions(db, body.project_id))
