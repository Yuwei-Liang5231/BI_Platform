"""编码识别：UTF-8（含 BOM）/ GBK / GB18030（架构 D12 阶段 1 最小质检）。

策略（fail-fast，明确拒绝而非静默猜错）：
1. BOM 直接判定
2. 严格 UTF-8 解码成功 → utf-8（纯 ASCII 亦归此类）
3. 严格 GBK 解码成功 → gbk
4. 严格 GB18030 解码成功 → gb18030（GBK 超集，兜底）
5. 全部失败 → 明确报错，不落库
"""

from __future__ import annotations

from pathlib import Path

from app.core.response import BusinessError

_SAMPLE_BYTES = 65_536


def detect_encoding(path: Path) -> str:
    """对文件头部采样判定编码。返回 'utf-8' | 'gbk' | 'gb18030'。"""
    with open(path, "rb") as f:
        chunk = f.read(_SAMPLE_BYTES)
    if not chunk:
        raise BusinessError("文件为空，无法识别编码")

    if chunk.startswith(b"\xef\xbb\xbf"):
        return "utf-8"  # BOM 由 utf-8-sig 透明剥离，标签统一记 utf-8

    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            chunk.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue

    raise BusinessError("无法识别文件编码（已尝试 UTF-8 / GBK / GB18030）")
