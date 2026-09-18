"""项目工作区服务（B9.3）：默认项目、CRUD、归属校验。

方案 A（单库命名空间）：projects 为顶层命名空间，datasets/metrics/
ask_conversations 等主表挂 project_id。默认项目自动创建（存量数据回填），
project_id 缺省一律落默认项目——存量调用方零改动。
权限：本项目级权限留 B15；当前角色仍为全局（admin 管项目，analyst/viewer 切换浏览）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.infra.models import Dataset, KeyValue, Metric, Project
from app.infra.repository import Repository

DEFAULT_PROJECT_NAME = "默认项目"
DEFAULT_PROJECT_KEY = "default_project_id"


def ensure_default_project(db: Session) -> Project:
    """默认项目幂等创建 + 存量 NULL 回填（lifespan/迁移后调用）。

    标记存 key_value（default_project_id）而非按名字判定——默认项目允许
    admin 改名（如改为「历史数据」），改名后不得再创建第二个默认项目。
    回填只处理 project_id IS NULL 的行；幂等：标记存在且无 NULL 行时空操作。
    """
    repo_kv = Repository(KeyValue, db)
    marker = repo_kv.get(DEFAULT_PROJECT_KEY)
    project = None
    if marker is not None and marker.value:
        project = db.get(Project, int(marker.value))
    if project is None:
        project = (
            db.query(Project).filter(Project.name == DEFAULT_PROJECT_NAME).first()
            or Repository(Project, db).add(
                Project(name=DEFAULT_PROJECT_NAME, created_by="system")
            )
        )
        repo_kv.update(marker, value=str(project.id)) if marker is not None else repo_kv.add(
            KeyValue(key=DEFAULT_PROJECT_KEY, value=str(project.id))
        )
    for model in (Dataset, Metric):
        db.query(model).filter(model.project_id.is_(None)).update(
            {model.project_id: project.id}, synchronize_session=False
        )
    from app.infra.models import AskConversation

    db.query(AskConversation).filter(AskConversation.project_id.is_(None)).update(
        {AskConversation.project_id: project.id}, synchronize_session=False
    )
    db.flush()
    return project


def default_project_id(db: Session) -> int:
    """缺省项目 id（调用方未显式指定项目时使用）。"""
    return ensure_default_project(db).id


def resolve_project_id(db: Session, project_id: int | None) -> int:
    """入参 project_id 归一：None → 默认项目；非空校验存在性。"""
    if project_id is None:
        return default_project_id(db)
    if Repository(Project, db).get(project_id) is None:
        raise BusinessError(f"项目 {project_id} 不存在", 40400)
    return project_id


def project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "created_by": p.created_by,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def list_projects(db: Session) -> list[Project]:
    return list(db.query(Project).order_by(Project.id))


def create_project(db: Session, *, name: str, description: str = "", operator_id: str = "system") -> Project:
    if not isinstance(name, str) or not name.strip():
        raise BusinessError("项目名称必须是非空字符串", 40000)
    name = name.strip()
    if db.query(Project).filter(Project.name == name).first() is not None:
        raise BusinessError(f"项目 {name!r} 已存在", 40900)
    return Repository(Project, db).add(
        Project(name=name, description=(description or "").strip(), created_by=operator_id)
    )


def update_project(db: Session, project_id: int, changes: dict) -> Project:
    repo = Repository(Project, db)
    project = repo.get(project_id)
    if project is None:
        raise BusinessError(f"项目 {project_id} 不存在", 40400)
    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise BusinessError("项目名称必须是非空字符串", 40000)
        dup = db.query(Project).filter(Project.name == name, Project.id != project_id).first()
        if dup is not None:
            raise BusinessError(f"项目 {name!r} 已存在", 40900)
        project.name = name
    if "description" in changes:
        project.description = (changes["description"] or "").strip()
    repo.update(project)  # flush
    return project


def delete_project(db: Session, project_id: int) -> dict:
    """删除空项目（仍有数据集或指标时拒绝——防误删整条业务线）。默认项目不可删。"""
    repo = Repository(Project, db)
    project = repo.get(project_id)
    if project is None:
        raise BusinessError(f"项目 {project_id} 不存在", 40400)
    marker = Repository(KeyValue, db).get(DEFAULT_PROJECT_KEY)
    if marker is not None and marker.value and int(marker.value) == project_id:
        # 默认项目承载存量数据且不可删（可改名；改名后仍按标记识别）
        raise BusinessError("默认项目不可删除", 40000)
    # 占用计数排除软删指标（status='deleted' 仅留痕不占项目，与指标编码
    # 唯一性排除 deleted 同一语义）；数据集为硬删除无需排除。
    ds_count = db.query(Dataset).filter(Dataset.project_id == project_id).count()
    metric_count = (
        db.query(Metric)
        .filter(Metric.project_id == project_id, Metric.status != "deleted")
        .count()
    )
    if ds_count or metric_count:
        raise BusinessError(
            f"项目 {project.name!r} 仍有 {ds_count} 个数据集、{metric_count} 个指标，"
            "请先删除或迁移后再删除项目",
            40000,
        )
    repo.delete(project_id)
    return {"id": project_id}
