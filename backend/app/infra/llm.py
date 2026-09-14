"""LLM 适配器（B9，架构"infra/llm：换模型只改配置"）。

边界（计划 3.3 铁律）：LLM **只产意图**——从指标清单中选指标、解析时间与
比较方式，输出结构化 JSON；数值一律由指标中心（query.service）统一计算。
LLM 不写 SQL、不产数字，杜绝幻觉影响数据可信度。

- OpenAI 兼容 chat.completions（DeepSeek 等同协议），base_url/api_key/model 全配置化
- 配置优先级（阶段 2）：数据库 llm_models 表 is_active=1 的记录（页面"模型管理"
  可增删改切换）> env 文件 settings.llm_*（兜底默认）；两处都未配置则不可用
- 未配置（任一项为空）→ llm_available=False，调用方走关键词兜底解析器
- 失败（网络/超时/JSON 解析）不抛异常——返回 None 由调用方降级
"""

from __future__ import annotations

import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infra.models import LlmModel

logger = logging.getLogger("app.infra.llm")


def resolve_llm_config(db: Session | None, settings: Settings) -> dict | None:
    """解析当前生效的 LLM 配置。

    返回 {"base_url","api_key","model","source","model_id"} 或 None（未配置）。
    source: "db"（模型管理页启用的记录）| "env"（env 文件兜底）。
    """
    if db is not None:
        active = db.query(LlmModel).filter(LlmModel.is_active == 1).first()
        if active is not None:
            return {
                "base_url": active.base_url,
                "api_key": active.api_key,
                "model": active.model,
                "source": "db",
                "model_id": active.id,
            }
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        return {
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
            "source": "env",
            "model_id": None,
        }
    return None


def llm_available(settings) -> bool:
    """env 兜底通道是否可用（兼容旧调用；运行时判定请用 resolve_llm_config）。"""
    return bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def mask_llm_config(config: dict | None) -> dict | None:
    """脱敏副本（api_key 打码），供列表/状态接口返回。"""
    if not config:
        return None
    return {**config, "api_key": _mask_key(config.get("api_key", ""))}


def chat_json(
    config: dict | None,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
) -> dict | None:
    """请求 LLM 并解析 JSON 响应。任何失败返回 None（调用方降级，不致命）。

    config 为 resolve_llm_config 的产物（或路由层临时拼装的等价 dict）；
    None / 缺字段直接返回 None。
    """
    if not config or not (config.get("base_url") and config.get("api_key") and config.get("model")):
        return None
    url = config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
    }
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # 兼容个别模型无视 json_object 模式包裹代码围栏
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        return json.loads(content)
    except Exception as exc:  # 网络/超时/JSON/结构任一失败都降级
        logger.warning("LLM 意图解析失败（降级到关键词解析器）: %s", exc)
        return None


def test_connection(config: dict | None, timeout: float = 15.0) -> tuple[bool, str]:
    """连通性测试（模型管理页用）：发一条最小 chat 请求验证配置。

    返回 (ok, 消息)。与 chat_json 不同，这里需要把失败原因明确反馈给管理员，
    故不吞异常。
    """
    if not config or not (config.get("base_url") and config.get("api_key") and config.get("model")):
        return False, "配置不完整：base_url / api_key / model 均必填"
    url = config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": "回复 OK 两个字母即可"}],
        "max_tokens": 10,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return True, f"连接成功，模型已响应：{(content or '').strip()[:50]}"
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        return False, f"接口返回错误 HTTP {exc.response.status_code}：{detail}"
    except Exception as exc:
        return False, f"连接失败：{exc}"
