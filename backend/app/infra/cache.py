"""指标结果缓存（架构 7.4 infra/cache.py，B3）。

进程内 LRU。缓存键含 指标 ver + dataset_ver + 时间范围（不变式 3：
口径变更或数据重导入 → 版本号变化 → 旧键自然失效，无需主动清理）。
另拼入 SQL 摘要兜底，杜绝"同版本号不同语义"的极端漂移。

D8 单进程部署下进程内缓存与 DuckDB 直查天然一致；未来多 worker
部署时仅替换本模块实现（接口不变：get/put/clear）。

周期不完整的结果**不入缓存**：数据补录后它会变成完整周期，
缓存会让"暂时 null"变成"永远 null"。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class CacheStats:
    """命中率观测（/health 或运维接口可读）。"""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    maxsize: int = field(default=0)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "maxsize": self.maxsize,
            "hit_rate": self.hit_rate,
        }


class MetricResultCache:
    """线程安全 LRU 缓存。值以浅拷贝存取，防止调用方原地篡改缓存内容。"""

    def __init__(self, maxsize: int = 512):
        self._maxsize = maxsize
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats(maxsize=maxsize)

    @staticmethod
    def make_key(*parts) -> str:
        return ":".join(str(p) for p in parts)

    def get(self, key: str) -> dict | None:
        with self._lock:
            if key not in self._store:
                self.stats.misses += 1
                return None
            self._store.move_to_end(key)
            self.stats.hits += 1
            return deepcopy(self._store[key])

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._store[key] = deepcopy(value)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)
                self.stats.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# 模块级单例：uvicorn 单 worker（D8）下全进程共享
metric_cache = MetricResultCache(maxsize=512)
