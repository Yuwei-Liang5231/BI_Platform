"""LLM 适配器（B9，架构"infra/llm：换模型只改配置"）。

边界（计划 3.3 铁律）：LLM **只产意图**——从指标清单中选指标、解析时间与
比较方式，输出结构化 JSON；数值一律由指标中心（query.service）统一计算。
LLM 不写 SQL、不产数字，杜绝幻觉影响数据可信度。

- OpenAI 兼容 chat.completions（DeepSeek 等同协议），base_url/api_key/model
  全配置化（config.llm_*，来自 env 文件）
- 未配置（任一项为空）→ llm_available=False，调用方走关键词兜底解析器
- 失败（网络/超时/JSON 解析）不抛异常——返回 None 由调用方降级
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger("app.infra.llm")


def llm_available(settings) -> bool:
    return bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)


def chat_json(settings, system_prompt: str, user_prompt: str, timeout: float = 30.0) -> dict | None:
    """请求 LLM 并解析 JSON 响应。任何失败返回 None（调用方降级，不致命）。"""
    if not llm_available(settings):
        return None
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
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
