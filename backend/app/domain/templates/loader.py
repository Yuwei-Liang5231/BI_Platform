"""模板包（YAML）加载与结构校验。

包结构（每个行业一个文件，industry 为 ASCII 小写）：
    industry: demo
    name: 示例行业
    description: ...
    metrics:
      - code: demo_metric_a           # 全局唯一引用码（建议带行业前缀）
        name: 示例指标
        aliases: [别名一, 别名二]     # 可选
        definition: 口径说明          # 可选
        topic: sales                  # 可选，默认 general
        parent: null                  # 可选，父指标 code（须在同包中先于本条出现）
        dimensions: [维度一, 维度二]  # 可选
        disambiguation: {question: ..., options: [...], default: ...}  # 可选
        requires: null                # 或 extended-operator（依赖阶段 4a 扩展算子）
        calc_rule: {...}              # 受限结构（schema.parse_calc_rule 校验）

校验失败抛 TemplateError（导入与列表接口统一转 BusinessError 400）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.config import get_settings
from app.core.response import BusinessError
from app.domain.metric.schema import CalcRuleError, parse_calc_rule

REQUIRES_EXTENDED = "extended-operator"
VALID_REQUIRES = (None, REQUIRES_EXTENDED)

_CODE_RE = re.compile(r"^[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff-]{0,98}$")
_INDUSTRY_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")

_ALLOWED_ITEM_KEYS = {
    "code", "name", "aliases", "definition", "topic", "parent",
    "dimensions", "disambiguation", "requires", "calc_rule",
}


class TemplateError(ValueError):
    """模板包结构非法（面向模板维护者）。"""


def _fail(industry: str, detail: str) -> BusinessError:
    return BusinessError(f"模板包 {industry}: {detail}", 40000)


def _validate_metric(raw: dict, industry: str, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise _fail(industry, "metrics 每条必须是对象")
    unknown = set(raw) - _ALLOWED_ITEM_KEYS
    if unknown:
        raise _fail(industry, f"指标 {raw.get('code')!r} 含不支持的字段：{sorted(unknown)}")

    code = raw.get("code")
    if not isinstance(code, str) or not _CODE_RE.match(code.strip() or ""):
        raise _fail(industry, f"code 非法：{code!r}（字母/中文开头，2-99 位）")
    code = code.strip()
    if code in seen:
        raise _fail(industry, f"code 重复：{code}")
    seen.add(code)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _fail(industry, f"{code}: name 必须是非空字符串")

    requires = raw.get("requires")
    if requires not in VALID_REQUIRES:
        raise _fail(industry, f"{code}: requires 只允许 null / {REQUIRES_EXTENDED}")

    aliases = raw.get("aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
        raise _fail(industry, f"{code}: aliases 必须是非空字符串数组")

    disambiguation = raw.get("disambiguation") or {}
    if not isinstance(disambiguation, dict):
        raise _fail(industry, f"{code}: disambiguation 必须是对象")

    dimensions = raw.get("dimensions") or []
    if not isinstance(dimensions, list):
        raise _fail(industry, f"{code}: dimensions 必须是数组")

    topic = raw.get("topic") or "general"
    if not isinstance(topic, str) or not topic.strip():
        raise _fail(industry, f"{code}: topic 必须是非空字符串")

    parent = raw.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise _fail(industry, f"{code}: parent 必须是同包内先出现指标的 code 或 null")

    calc_rule = raw.get("calc_rule")
    if not isinstance(calc_rule, dict):
        raise _fail(industry, f"{code}: calc_rule 必须是对象")
    if requires is None:  # 扩展算子指标允许无合法 calc_rule（仅登记口径）
        try:
            parse_calc_rule(calc_rule)
        except CalcRuleError as exc:
            raise _fail(industry, f"{code}: calc_rule 不合法——{exc}") from exc

    return {
        "code": code,
        "name": name.strip(),
        "aliases": [a.strip() for a in aliases if a.strip()],
        "definition": raw.get("definition") or "",
        "topic": topic.strip(),
        "parent": parent.strip() if isinstance(parent, str) else None,
        "dimensions": dimensions,
        "disambiguation": disambiguation,
        "requires": requires,
        "calc_rule": calc_rule,
    }


def _pack_path(industry: str) -> Path:
    path = get_settings().resolved_templates_dir / f"{industry}.yaml"
    if not path.exists():
        raise BusinessError(f"模板包 {industry!r} 不存在（目录：{path.parent}）", 40400)
    return path


def load_pack(industry: str) -> dict:
    """加载并校验单个行业模板包。"""
    industry = (industry or "").strip()
    if not _INDUSTRY_RE.match(industry):
        raise BusinessError(f"行业标识非法：{industry!r}", 40000)
    path = _pack_path(industry)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BusinessError(f"模板包 {industry} YAML 解析失败：{exc}", 50000) from exc
    if not isinstance(raw, dict):
        raise _fail(industry, "包根必须是对象")

    pack_industry = raw.get("industry")
    if pack_industry != industry:
        raise _fail(industry, f"文件内 industry={pack_industry!r} 与文件名不一致")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _fail(industry, "缺少包名 name")
    metrics_raw = raw.get("metrics")
    if not isinstance(metrics_raw, list) or not metrics_raw:
        raise _fail(industry, "metrics 必须是非空数组")

    seen: set[str] = set()
    metrics = [_validate_metric(item, industry, seen) for item in metrics_raw]

    # parent 必须在同包且先出现（保证导入顺序可解析层级）
    appeared: set[str] = set()
    for m in metrics:
        if m["parent"] is not None and m["parent"] not in appeared:
            raise _fail(industry, f"{m['code']}: parent {m['parent']!r} 不存在或未先出现")
        appeared.add(m["code"])

    return {
        "industry": industry,
        "name": name.strip(),
        "description": raw.get("description") or "",
        "metrics": metrics,
    }


def list_packs() -> list[dict]:
    """扫描模板目录，返回行业列表（按文件名排序）。单包损坏不阻断整体列表。"""
    directory = get_settings().resolved_templates_dir
    result = []
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.yaml")):
        industry = path.stem
        try:
            pack = load_pack(industry)
        except BusinessError:
            result.append({"industry": industry, "name": industry, "description": "",
                           "metric_count": 0, "error": True})
            continue
        extended = sum(1 for m in pack["metrics"] if m["requires"] is not None)
        result.append({
            "industry": industry,
            "name": pack["name"],
            "description": pack["description"],
            "metric_count": len(pack["metrics"]),
            "extended_count": extended,
        })
    return result
