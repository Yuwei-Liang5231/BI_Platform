"""编码识别单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.response import BusinessError
from app.domain.ingestion.encoding import detect_encoding


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_utf8_detected(tmp_path):
    p = _write(tmp_path, "a.csv", "订单,金额\nSO1,10.5\n".encode("utf-8"))
    assert detect_encoding(p) == "utf-8"


def test_utf8_bom_detected(tmp_path):
    p = _write(tmp_path, "b.csv", "订单,金额\nSO1,10.5\n".encode("utf-8-sig"))
    assert detect_encoding(p) == "utf-8"


def test_gbk_detected(tmp_path):
    p = _write(tmp_path, "c.csv", "订单,金额\nSO1,10.5\n".encode("gbk"))
    assert detect_encoding(p) == "gbk"


def test_gb18030_fallback(tmp_path):
    # "𠮷" 是 GB18030 独有字符，GBK 无法编码
    p = _write(tmp_path, "d.csv", "名称\n𠮷\n".encode("gb18030"))
    assert detect_encoding(p) == "gb18030"


def test_undecodable_raises(tmp_path):
    p = _write(tmp_path, "e.bin", b"\xff\xfe\x81\x81\xfa\xfa")
    with pytest.raises(BusinessError):
        detect_encoding(p)


def test_empty_file_raises(tmp_path):
    p = _write(tmp_path, "f.csv", b"")
    with pytest.raises(BusinessError):
        detect_encoding(p)
