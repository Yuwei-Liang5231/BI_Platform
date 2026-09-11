"""仓储层：屏蔽存储细节，B1 起所有元数据读写经由 Repository 抽象。"""

from app.infra.repository.base import Repository

__all__ = ["Repository"]
