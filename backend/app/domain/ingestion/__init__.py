"""数据接入领域包：编码识别、类型推断、落盘与注册。行业无关（不变式 7）。"""

from app.domain.ingestion.encoding import detect_encoding
from app.domain.ingestion.types import classify_value

__all__ = ["detect_encoding", "classify_value"]
