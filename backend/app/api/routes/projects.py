"""项目工作区路由（B9.3）：列表/创建/改名/删除。

权限：读登录即可（切换器需要）；写操作 admin 专用。默认项目不可删，
非空项目（仍有数据集/指标）删除被拒绝。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.project import service

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = ""


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None


@router.get("")
def list_projects(db: DbDep, _: CurrentUser):
    """项目列表（登录即可，供全局切换器）。"""
    return ok_response([service.project_to_dict(p) for p in service.list_projects(db)])


@router.post("")
def create_project(db: DbDep, body: ProjectCreateRequest, user: AdminUser):
    project = service.create_project(
        db, name=body.name, description=body.description, operator_id=user.username
    )
    return ok_response(service.project_to_dict(project), message="项目已创建")


@router.patch("/{project_id}")
def update_project(db: DbDep, project_id: int, body: ProjectUpdateRequest, _: AdminUser):
    payload = body.model_dump(exclude_none=True)
    if not payload:
        from app.core.response import BusinessError

        raise BusinessError("没有提供任何要修改的字段", 40000)
    project = service.update_project(db, project_id, payload)
    return ok_response(service.project_to_dict(project), message="项目已更新")


@router.delete("/{project_id}")
def delete_project(db: DbDep, project_id: int, _: AdminUser):
    return ok_response(service.delete_project(db, project_id), message="项目已删除")
