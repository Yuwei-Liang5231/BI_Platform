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
import time

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infra.models import LlmModel

logger = logging.getLogger("app.infra.llm")


def _parse_extra_body(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        logger.warning("LLM_EXTRA_BODY 不是合法 JSON 对象，已忽略: %.100r", raw)
        return None


def resolve_llm_config(db: Session | None, settings: Settings) -> dict | None:
    """解析当前生效的 LLM 配置。

    返回 {"base_url","api_key","model","source","model_id","verify_ssl","ca_bundle","extra_body"} 或 None（未配置）。
    source: "db"（模型管理页启用的记录）| "env"（env 文件兜底）。
    SSL 校验与请求体附加字段取全局 settings（企业内网自签证书：verify_ssl=false 或
    ca_bundle 指向公司 CA 包；extra_body 如关闭思考模式），对 db/env 两来源一视同仁。
    """
    verify = {
        "verify_ssl": settings.llm_verify_ssl,
        "ca_bundle": settings.llm_ca_bundle,
        "extra_body": _parse_extra_body(settings.llm_extra_body),
    }
    if db is not None:
        active = db.query(LlmModel).filter(LlmModel.is_active == 1).first()
        if active is not None:
            return {
                "base_url": active.base_url,
                "api_key": active.api_key,
                "model": active.model,
                "source": "db",
                "model_id": active.id,
                **verify,
            }
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        return {
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
            "source": "env",
            "model_id": None,
            **verify,
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


def _http_verify(config: dict):
    """httpx 的 verify 参数：ca_bundle 优先，其次 verify_ssl 开关（默认校验）。"""
    bundle = (config.get("ca_bundle") or "").strip()
    if bundle:
        return bundle
    return bool(config.get("verify_ssl", True))


def mask_llm_config(config: dict | None) -> dict | None:
    """脱敏副本（api_key 打码），供列表/状态接口返回。"""
    if not config:
        return None
    return {**config, "api_key": _mask_key(config.get("api_key", ""))}


def _extract_json_object(content: str) -> dict | None:
    """从模型返回文本中稳健提取 JSON 对象。

    依次尝试：直接解析 → 去代码围栏 → 花括号配平截取首个 {...}（部分模型
    会在 JSON 前后夹杂说明文字或 <think> 块）。全部失败返回 None。
    """
    text = (content or "").strip()
    if not text:
        return None
    candidates = [text]
    if text.startswith("```"):
        candidates.append(text.strip("`").removeprefix("json").strip())
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for cand in candidates:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _post_chat(config: dict, payload: dict, timeout: float) -> tuple[dict | None, dict | None]:
    """单次 chat 请求 + JSON 提取。失败返回 (None, None)（不抛异常）。

    第二个返回值为该次响应的 token 用量（{prompt_tokens, completion_tokens}），
    供 AI 观测面板（B2）记录；接口未返回 usage 时为 None。
    """
    url = config["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    # 连接 5s 快速失败（外网不可达时尽快降级关键词解析器），生成读取给足 timeout
    timeout_policy = httpx.Timeout(timeout, connect=5.0)
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_policy, verify=_http_verify(config))
        resp.raise_for_status()
        body = resp.json()
        message = body["choices"][0]["message"]
        usage = body.get("usage") or {}
        usage_clean = {
            k: usage[k] for k in ("prompt_tokens", "completion_tokens") if isinstance(usage.get(k), int)
        } or None
        obj = _extract_json_object(message.get("content") or "")
        if obj is None:
            # content 为空/非 JSON：推理型模型 token 耗尽或输出夹带说明文字
            logger.warning(
                "LLM 返回无法解析为 JSON 对象: finish=%s content=%.200r reasoning_len=%s",
                body["choices"][0].get("finish_reason"),
                message.get("content"),
                len(message.get("reasoning_content") or ""),
            )
        return obj, usage_clean
    except Exception as exc:  # 网络/超时/JSON/结构任一失败
        logger.warning("LLM 请求失败: %s", exc)
        return None, None


def chat_json(
    config: dict | None,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
    retries: int = 1,
    meta: dict | None = None,
) -> dict | None:
    """请求 LLM 并解析 JSON 响应。任何失败返回 None（调用方降级，不致命）。

    config 为 resolve_llm_config 的产物（或路由层临时拼装的等价 dict）；
    None / 缺字段直接返回 None。
    max_tokens 留足 2000：推理型模型（如 deepseek-v4）思考过程也计入 token，
    余量不足会被思考耗尽（finish_reason=length）导致 content 为空。
    config.extra_body（dict）逐键并入请求体（如 {"enable_thinking": false}
    关闭思考模式，省时省钱——是否生效取决于网关/模型）。
    **失败自动重试**：retries 表示额外重试次数，总尝试次数 = retries + 1。
    默认 retries=1（总 2 次，与历史行为一致）；新调用点传 retries=0 关闭重试
    （2026-09-15：网关偶发超时/限流，temperature=0 输出确定性，重试安全）。

    meta（B2 观测，可选）：传入 dict 就地回填调用元数据——
    attempted（是否真的发起了请求）/ ok / duration_ms（多次尝试累加）/
    model / prompt_tokens / completion_tokens。供 AI 调用观测落库。
    """
    if not config or not (config.get("base_url") and config.get("api_key") and config.get("model")):
        return None
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 2000,
    }
    extra = config.get("extra_body")
    if isinstance(extra, dict):
        payload.update(extra)

    if meta is not None:
        meta["attempted"] = True
        meta["model"] = config.get("model") or ""
        meta.setdefault("duration_ms", 0)

    for attempt in range(1, retries + 2):
        t0 = time.monotonic()
        obj, usage = _post_chat(config, payload, timeout)
        if meta is not None:
            meta["duration_ms"] = int(meta.get("duration_ms", 0)) + int((time.monotonic() - t0) * 1000)
            meta["ok"] = obj is not None
            if usage:
                meta["prompt_tokens"] = usage.get("prompt_tokens")
                meta["completion_tokens"] = usage.get("completion_tokens")
        if obj is not None:
            if attempt > 1:
                logger.info("LLM 请求第 %s 次重试成功", attempt)
            return obj
        logger.warning("LLM 意图解析第 %s/%s 次尝试失败", attempt, retries + 1)
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
        resp = httpx.post(
            url, json=payload, headers=headers,
            timeout=httpx.Timeout(timeout, connect=5.0), verify=_http_verify(config),
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return True, f"连接成功，模型已响应：{(content or '').strip()[:50]}"
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        return False, f"接口返回错误 HTTP {exc.response.status_code}：{detail}"
    except Exception as exc:
        return False, f"连接失败：{exc}"
