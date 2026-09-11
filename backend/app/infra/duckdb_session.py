"""DuckDB 连接管理（架构 7.5 infra/duckdb_session.py，B3）。

约定（与 B2 执行测试同款）：**视图名 = 数据集名**，指向该数据集的
Parquet 文件。编译产物的 SQL 直接引用数据集名，无需改写。

每次计算新建内存连接：DuckDB 建连为毫秒级，换来两点——
1) 线程安全（FastAPI 同步路由跑在线程池，共享连接非线程安全）；
2) 规避 DuckDB 写并发限制（本模块只读 Parquet，绝不写入）。
路径一律走参数绑定（read_parquet(?)），杜绝路径拼接注入。
"""

from __future__ import annotations

from contextlib import contextmanager

import duckdb


@contextmanager
def duckdb_views(datasets: dict[str, str]):
    """上下文管理器：datasets = {数据集名: parquet_path}。

    yield 一个已注册数据视图的 DuckDB 连接，退出时关闭。
    """
    con = duckdb.connect()
    try:
        for name, path in datasets.items():
            # CREATE VIEW 不支持 prepared parameter，改为字面量 + 单引号转义
            # （路径由服务端存储层生成，转义兜底防路径含引号的极端情况）
            safe_path = path.replace("'", "''")
            con.execute(
                f'CREATE VIEW "{name.replace(chr(34), chr(34) * 2)}" '
                f"AS SELECT * FROM read_parquet('{safe_path}')"
            )
        yield con
    finally:
        con.close()
