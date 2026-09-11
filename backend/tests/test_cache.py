"""LRU 缓存单元测试：命中/驱逐/线程安全 basics + 键因子稳定性。"""

from __future__ import annotations

import threading

from app.infra.cache import MetricResultCache


class TestMetricResultCache:
    def test_put_get_and_lru_eviction(self):
        cache = MetricResultCache(maxsize=2)
        cache.put("a", {"value": 1})
        cache.put("b", {"value": 2})
        assert cache.get("a") == {"value": 1}      # a 变为最近使用
        cache.put("c", {"value": 3})                # 驱逐 b
        assert cache.get("b") is None
        assert cache.get("a") == {"value": 1}
        assert cache.get("c") == {"value": 3}
        assert cache.stats.evictions == 1
        assert cache.stats.hits == 3
        assert cache.stats.misses == 1

    def test_value_is_isolated_copy(self):
        cache = MetricResultCache()
        payload = {"value": 1, "nested": {"x": 1}}
        cache.put("k", payload)
        payload["nested"]["x"] = 999                # 改调用方副本不影响缓存
        assert cache.get("k")["nested"]["x"] == 1
        got = cache.get("k")
        got["value"] = 42                            # 改返回副本不影响缓存
        assert cache.get("k")["value"] == 1

    def test_make_key_stability(self):
        assert MetricResultCache.make_key("metric", 1, 2, "orders:1", "2026-01-01") == \
            "metric:1:2:orders:1:2026-01-01"

    def test_concurrent_access(self):
        cache = MetricResultCache(maxsize=8)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(200):
                    cache.put(f"k{n}-{i % 16}", {"value": i})
                    cache.get(f"k{n}-{i % 16}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(cache) <= 8
