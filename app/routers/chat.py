# app/routers/chat.py

"""
对话接口路由：
  POST /v1/chat/completions   OpenAI 兼容（流式 / 非流式）

功能：
  - 图片/文件上传（Cosverse R2）
  - Function Calling 伪实现（@@TOOL_CALL_BEGIN@@ 标记）
  - 配额耗尽 / cookie 过期自动换号重试
"""
import asyncio
import copy
import json
import logging
import random
import re
import time
import traceback
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cookie_manager import cm
from app.cosverse_client import (
    messages_to_text,
    stream_cosverse_response,
    upload_url_to_cosverse,
    _build_attachment_list,
)
from app.models import AccountState
from app.routers.model_list import resolve_model, DEFAULT_MODEL

log = logging.getLogger("cosverse_proxy.chat")

router = APIRouter()

# 将最大重试次数改为 4：以便在第 3 次失败并强制注册新号后，能有第 4 次机会使用新号完成当前请求
MAX_RETRIES     = 4
RETRY_DELAY     = 1.0
RETRY_DELAY_MAX = 3.0

# ══════════════════════════════════════════════════════════
# 工具调用（Function Calling）伪实现
# ══════════════════════════════════════════════════════════
_TC_BEGIN   = "@@TOOL_CALL_BEGIN@@"
_TC_END     = "@@TOOL_CALL_END@@"
_TC_PATTERN = re.compile(
    rf"{re.escape(_TC_BEGIN)}\s*(.*?)\s*{re.escape(_TC_END)}",
    re.DOTALL,
)


def _backoff(attempt: int) -> float:
    delay = min(RETRY_DELAY * (2 ** (attempt - 1)), RETRY_DELAY_MAX)
    return delay + random.uniform(0, delay * 0.1)


# ── 消息预处理 ────────────────────────────────────────────

def _convert_messages(messages: list[dict]) -> list[dict]:
    """
    标准化 messages：
    - tool 角色 → user（带工具返回标注）
    - 保留 list 类型 content（后续图片过滤会处理）
    """
    result = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        name    = msg.get("name", "")
        if role == "tool":
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            result.append({
                "role":    "user",
                "content": f"[工具 {name} 返回结果]\n{text}\n请基于这个结果直接回复用户，不要再次调用工具。",
            })
        else:
            result.append({"role": role, "content": content})
    return result


async def _extract_and_filter_images(
    messages: list[dict],
) -> tuple[list[dict], list[str]]:
    """
    遍历 messages，提取图片 URL 用于上传。
    倒序扫描，只保留最近 3 次 user 消息中的图片（节省配额）；
    更早的历史图片替换为占位文本。

    返回：(清理后的纯文本 messages, 需要上传的 image_url 列表)
    """
    resolved_messages: list[dict] = []
    images_to_upload:  list[str]  = []
    user_turn_count = 0

    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_turn_count += 1

        content = msg.get("content")

        if not isinstance(content, list):
            resolved_messages.insert(0, msg)
            continue

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                if user_turn_count <= 3:
                    if url:
                        images_to_upload.append(url)
                    text_parts.append("[附带图片]")
                else:
                    text_parts.append("[历史图片已被系统省略]")

        resolved_messages.insert(0, {
            **msg,
            "content": "\n\n".join(filter(None, text_parts)),
        })

    images_to_upload.reverse()
    return resolved_messages, images_to_upload


# ── Function Calling 工具 ─────────────────────────────────

def _annotate_tool_declarations(tools: list[dict]) -> list[dict]:
    """在工具参数 description 里注入【必填】/【可选】标签，提升模型遵从率"""
    annotated = copy.deepcopy(tools)
    for tool in annotated:
        func         = tool if "parameters" in tool else tool.get("function", {})
        params       = func.get("parameters", {})
        required_set = set(params.get("required", []))
        props        = params.get("properties", {})
        for param_name, param_def in props.items():
            tag = "【必填】" if param_name in required_set else "【可选】"
            existing = param_def.get("description", "")
            param_def["description"] = f"{tag} {existing}".strip()
        if required_set:
            params["_required_params_notice"] = (
                f"以下参数为必填，绝对不能省略或传空值：{sorted(required_set)}"
            )
    return annotated


def _validate_tool_call(tc: dict, tool_declarations: list[dict]) -> tuple[bool, list[str]]:
    name = tc.get("name", "")
    args = tc.get("arguments", {})
    for tool in tool_declarations:
        func      = tool if "parameters" in tool else tool.get("function", {})
        func_name = func.get("name", "")
        if func_name != name:
            continue
        params  = func.get("parameters", {})
        required = params.get("required", [])
        missing  = [r for r in required if r not in args or args[r] in (None, "", {}, [])]
        if missing:
            log.warning("tool call [%s] 缺少必填参数: %s", name, missing)
            return False, missing
        return True, []
    log.warning("tool call [%s] 未在工具列表中找到，跳过校验", name)
    return True, []


def _build_tool_system_prompt(tool_declarations: list[dict]) -> str:
    annotated = _annotate_tool_declarations(tool_declarations)
    tool_json = json.dumps(annotated, ensure_ascii=False, indent=2)
    return f"""## 核心设定：外部工具调用系统

你被赋予了调用外部真实系统（工具）的能力。当用户的需求需要真实执行（如画图、查询、搜索）时，你必须精确输出工具调用代码块，而不能假装执行。

### 🚨 绝对禁止的错误模式

❌ 只演戏，不调用（没有输出 `{_TC_BEGIN}` 代码块）
❌ 假装已经拿到结果（没有真正调用工具就给出答案）
❌ 需要参数却传空对象：`"arguments": {{}}` —— 这等于没有调用！

### 🔒 调用前必须完成的自检

在输出任何 `{_TC_BEGIN}` 块之前，逐项确认：
1. 工具名称来自下方【可用工具】列表
2. 每个【必填】参数是否都有具体的、非空的值
3. 如果某个【必填】参数还不知道 → **先向用户提问，不要调用**

### ✅ 正确的调用格式

过渡语（可选）……
{_TC_BEGIN}
{{
  "name": "工具名",
  "arguments": {{
    "参数名": "参数值"
  }}
}}
{_TC_END}

**注意：**
- `{_TC_BEGIN}` 和 `{_TC_END}` 必须独占一行
- 之间只能有纯净的 JSON，不能有 Markdown 代码块标记
- 输出完 `{_TC_END}` 后立即停止，等待系统返回结果
- 【必填】参数不能是空字符串 `""`、`null` 或空对象 `{{}}`

### 📌 当前【可用工具】列表

{tool_json}

### ⚠️ 系统最终警告

- 假装执行而不输出代码块 → 严重惩罚
- 输出代码块但【必填】参数为空或缺失 → 视同未调用，严重惩罚"""


def _parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """从文本中提取所有工具调用，返回 (去除调用块后的纯文本, tool_calls列表)"""
    tool_calls: list[dict] = []
    for m in _TC_PATTERN.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            tc  = {
                "id":        f"call_{uuid.uuid4().hex[:12]}",
                "name":      obj.get("name", ""),
                "arguments": obj.get("arguments", {}),
            }
            tool_calls.append(tc)
            log.info("解析到工具调用: %s", tc)
        except json.JSONDecodeError:
            log.warning("无法解析工具调用块: %r", raw)
    clean = _TC_PATTERN.sub("", text).strip()
    return clean, tool_calls


def _check_truncation(
    full_text: str, original_tools: list[dict]
) -> tuple[bool, str, list[dict]]:
    """检测工具调用块是否被上游截断（有 BEGIN 但没有 END）"""
    begin_idx = full_text.find(_TC_BEGIN)
    end_idx   = full_text.find(_TC_END)
    if begin_idx != -1 and end_idx == -1:
        fallback_text = full_text[:begin_idx].strip()
        name_match    = re.search(r'"name"\s*:\s*"([^"]+)"', full_text[begin_idx:])
        next_tools    = original_tools
        if name_match:
            target_name = name_match.group(1)
            filtered    = [
                t for t in original_tools
                if t.get("name") == target_name
                or t.get("function", {}).get("name") == target_name
            ]
            if filtered:
                next_tools = filtered
        return True, fallback_text, next_tools
    return False, "", original_tools


# ── 响应格式化 ────────────────────────────────────────────

def _make_plain_response(text: str, model: str) -> dict:
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage":   None,
    }


def _make_tool_call_response(tool_calls: list[dict], clean_text: str, model: str) -> dict:
    openai_tool_calls = [
        {
            "id":   tc["id"],
            "type": "function",
            "function": {
                "name":      tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for tc in tool_calls
    ]
    message: dict[str, Any] = {
        "role":       "assistant",
        "content":    clean_text or None,
        "tool_calls": openai_tool_calls,
    }
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage":   None,
    }


def _plain_text_to_sse(text: str, model: str) -> str:
    now     = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    return "".join([
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n\n",
    ])


def _make_tool_call_sse(tool_calls: list[dict], clean_text: str, model: str) -> str:
    now     = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    chunks = [
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
    ]
    if clean_text:
        chunks.append(
            _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                  "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}]})
        )
    openai_tool_calls_delta = [
        {
            "index": i,
            "id":    tc["id"],
            "type":  "function",
            "function": {
                "name":      tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for i, tc in enumerate(tool_calls)
    ]
    chunks.append(
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"tool_calls": openai_tool_calls_delta}, "finish_reason": None}]})
    )
    chunks.append(
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    )
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


# ── 上游流转文本 ──────────────────────────────────────────

async def _collect_full_text(gen) -> str:
    """消费 stream_cosverse_response 生成器，拼接完整文本"""
    parts = []
    async for chunk in gen:
        parts.append(chunk)
    return "".join(parts)


# ── 配额/cookie 错误检测 ──────────────────────────────────

def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    quota_keywords = (
        "quota", "429", "too many requests", "rate limit", "rate-limit",
        "rate_limit", "insufficient_quota", "daily limit", "usage limit",
        "message limit", "exceeded your current"
    )
    return any(k in msg for k in quota_keywords)

def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cookie_expired", "401", "unauthorized", "forbidden", "403"))


# ══════════════════════════════════════════════════════════
# 主路由
# ══════════════════════════════════════════════════════════

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body     = await request.json()
    messages = body.get("messages", [])
    stream   = body.get("stream", False)
    model    = body.get("model", DEFAULT_MODEL)
    tools    = body.get("tools", [])      # OpenAI function-calling tools
    has_tools = bool(tools)

    if not messages:
        raise HTTPException(400, "messages 不能为空")

    model_id, provider = resolve_model(model)

    # 1. 标准化消息（tool → user，保留 list content）
    clean_messages = _convert_messages(messages)

    # 2. 图片提取与历史过滤
    base_messages, raw_image_urls = await _extract_and_filter_images(clean_messages)

    # 3. 构建纯文本 prompt（用于无图片情况或作为文字主体）
    prompt_text = messages_to_text(base_messages)

    log.info(
        "chat | model=%s→%s stream=%s msgs=%d tools=%d images=%d",
        model, model_id, stream, len(base_messages), len(tools), len(raw_image_urls),
    )

    # ── 内部：带配额/cookie 感知的单次尝试 ────────────────
    async def _single_attempt(
        acc: AccountState,
        attempt_tools: list[dict],
    ) -> tuple[Any, bool]:
        """
        执行一次上游请求。
        返回 (result, success)：
          - result 是 str（完整文本）或抛出异常
        """
        # 上传图片（绑定当前账号的 cookie）
        cosverse_attachments: list[dict] = []
        if raw_image_urls:
            upload_tasks    = [upload_url_to_cosverse(u, acc) for u in raw_image_urls]
            upload_results  = await asyncio.gather(*upload_tasks, return_exceptions=True)
            good_results    = [r for r in upload_results if isinstance(r, dict) and r]
            cosverse_attachments = _build_attachment_list(good_results)
            if len(good_results) < len(raw_image_urls):
                log.warning(
                    "[%s] %d/%d 张图片上传失败",
                    acc.email, len(raw_image_urls) - len(good_results), len(raw_image_urls),
                )

        # 若有工具，注入 system prompt
        attempt_messages = list(base_messages)
        if has_tools and attempt_tools:
            tool_sys   = _build_tool_system_prompt(attempt_tools)
            insert_pos = next(
                (i + 1 for i, m in enumerate(attempt_messages) if m["role"] == "system"), 0
            )
            attempt_messages.insert(insert_pos, {"role": "system", "content": tool_sys})

        prompt = messages_to_text(attempt_messages)

        full_text = await _collect_full_text(
            stream_cosverse_response(
                prompt, acc,
                model=model_id, provider=provider,
                attachments=cosverse_attachments or None,
            )
        )
        return full_text

    # ══ 流式 ═══════════════════════════════════════════════
    if stream:
        async def sse_generator():
            last_error: Exception = RuntimeError("未知错误")
            current_tools         = list(tools)
            acc: Optional[AccountState] = None

            # 外层循环：MAX_RETRIES 次（含换号）
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    acc = await cm.get_next()
                except HTTPException as e:
                    yield f'data: {{"error": "无可用账号: {e.detail}"}}\n\n'
                    return

                try:
                    full_text = await _single_attempt(acc, current_tools)

                    # 工具调用路径
                    if has_tools:
                        # 截断检测
                        is_truncated, fallback_text, next_tools = _check_truncation(
                            full_text, tools
                        )
                        if is_truncated:
                            if attempt < MAX_RETRIES:
                                current_tools = next_tools
                                log.warning(
                                    "stream(tools) attempt %d 工具块被截断，缩减 tools 重试",
                                    attempt,
                                )
                                continue
                            else:
                                log.error("stream(tools) 所有尝试均被截断，降级返回过渡语")
                                yield _plain_text_to_sse(fallback_text, model)
                                return

                        clean_text, tool_calls = _parse_tool_calls(full_text)

                        # 必填参数校验
                        if tool_calls:
                            invalid = []
                            for tc in tool_calls:
                                valid, missing = _validate_tool_call(tc, tools)
                                if not valid:
                                    invalid.append((tc["name"], missing))
                            if invalid:
                                if attempt < MAX_RETRIES:
                                    log.warning(
                                        "stream(tools) attempt %d 必填参数缺失 %s，重试",
                                        attempt, invalid,
                                    )
                                    continue
                                else:
                                    log.error(
                                        "stream(tools) 参数仍不完整 %s，降级返回文本", invalid
                                    )
                                    yield _plain_text_to_sse(clean_text or full_text, model)
                                    return

                        # 记录用量
                        cm.record_usage(acc)

                        if tool_calls:
                            yield _make_tool_call_sse(tool_calls, clean_text, model)
                        else:
                            yield _plain_text_to_sse(clean_text or full_text, model)
                        return

                    # 普通流式——逐块 yield
                    # 已拿到完整文本，转换成 SSE 流输出
                    resp_id = f"chatcmpl-{uuid.uuid4().hex}"
                    created = int(time.time())

                    def _sse(obj: dict) -> str:
                        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

                    yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                             "finish_reason": None}]})
                    # 分块输出，每次最多 200 字符，保持流式体验
                    text = full_text
                    chunk_size = 200
                    for i in range(0, max(len(text), 1), chunk_size):
                        piece = text[i:i + chunk_size]
                        yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model,
                                    "choices": [{"index": 0, "delta": {"content": piece},
                                                 "finish_reason": None}]})
                    yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    yield "data: [DONE]\n\n"

                    cm.record_usage(acc)
                    return

                except PermissionError as e:
                    # cookie 过期
                    last_error = e
                    log.warning("stream attempt %d cookie 过期 [%s]，换号重试", attempt, acc.email)
                    try:
                        # ★ 增加 attempt=attempt 传参
                        acc = await cm.mark_failed_and_get_replacement(acc, "cookie_expired", attempt=attempt)
                    except HTTPException as he:
                        yield f'data: {{"error": "换号失败: {he.detail}"}}\n\n'
                        return

                except Exception as e:
                    last_error = e
                    if _is_quota_error(e):
                        log.warning(
                            "stream attempt %d 上游明确配额耗尽/限流 [%s]: %s，换号重试",
                            attempt, acc.email if acc else "?", e
                        )
                        if acc:
                            try:
                                acc = await cm.mark_failed_and_get_replacement(acc, "quota_exhausted", attempt=attempt)
                            except HTTPException as he:
                                yield f'data: {{"error": "换号失败: {he.detail}"}}\n\n'
                                return
                    elif _is_auth_error(e):
                        log.warning(
                            "stream attempt %d auth 错误 [%s]: %s，换号重试",
                            attempt, acc.email if acc else "?", e,
                        )
                        if acc:
                            try:
                                # ★ 增加 attempt=attempt 传参
                                acc = await cm.mark_failed_and_get_replacement(
                                    acc, "cookie_expired", attempt=attempt
                                )
                            except HTTPException as he:
                                yield f'data: {{"error": "换号失败: {he.detail}"}}\n\n'
                                return
                    else:
                        log.warning("stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(_backoff(attempt))

            traceback.print_exc()
            yield f'data: {{"error": "{last_error}"}}\n\n'

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ══ 非流式 ═════════════════════════════════════════════
    last_error: Exception = RuntimeError("未知错误")
    current_tools         = list(tools)
    acc: Optional[AccountState] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            acc = await cm.get_next()
        except HTTPException as e:
            return JSONResponse(
                status_code=500,
                content={"error": {"message": f"无可用账号: {e.detail}", "type": "no_account"}},
            )

        try:
            full_text = await _single_attempt(acc, current_tools)

            # 工具调用路径
            if has_tools:
                is_truncated, fallback_text, next_tools = _check_truncation(full_text, tools)
                if is_truncated:
                    if attempt < MAX_RETRIES:
                        current_tools = next_tools
                        log.warning(
                            "non-stream(tools) attempt %d 工具块被截断，缩减 tools 重试", attempt
                        )
                        continue
                    else:
                        log.error("non-stream(tools) 所有尝试均被截断，降级返回过渡语")
                        cm.record_usage(acc)
                        return JSONResponse(content=_make_plain_response(fallback_text, model))

                clean_text, tool_calls = _parse_tool_calls(full_text)

                if tool_calls:
                    invalid = []
                    for tc in tool_calls:
                        valid, missing = _validate_tool_call(tc, tools)
                        if not valid:
                            invalid.append((tc["name"], missing))
                    if invalid:
                        if attempt < MAX_RETRIES:
                            log.warning(
                                "non-stream(tools) attempt %d 参数不完整 %s，重试",
                                attempt, invalid,
                            )
                            continue
                        else:
                            log.error("non-stream(tools) 参数仍不完整 %s，降级", invalid)
                            cm.record_usage(acc)
                            return JSONResponse(
                                content=_make_plain_response(clean_text or full_text, model)
                            )

                cm.record_usage(acc)
                if tool_calls:
                    return JSONResponse(
                        content=_make_tool_call_response(tool_calls, clean_text, model)
                    )
                return JSONResponse(
                    content=_make_plain_response(clean_text or full_text, model)
                )

            # 普通非流式
            text = full_text.strip()
            cm.record_usage(acc)
            return JSONResponse(content={
                "id":      f"chatcmpl-{uuid.uuid4().hex}",
                "object":  "chat.completion",
                "created": int(time.time()),
                "model":   model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens":     len(prompt_text) // 4,
                    "completion_tokens": len(text) // 4,
                    "total_tokens":      (len(prompt_text) + len(text)) // 4,
                },
            })

        except PermissionError as e:
            last_error = e
            log.warning("non-stream attempt %d cookie 过期 [%s]，换号重试", attempt, acc.email)
            try:
                # ★ 增加 attempt=attempt 传参
                acc = await cm.mark_failed_and_get_replacement(acc, "cookie_expired", attempt=attempt)
            except HTTPException as he:
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": f"换号失败: {he.detail}", "type": "no_account"}},
                )

        except Exception as e:
            last_error = e
            if _is_quota_error(e):
                log.warning(
                    "non-stream attempt %d 上游明确配额耗尽/限流 [%s]: %s，换号重试",
                    attempt, acc.email if acc else "?", e
                )
                if acc:
                    try:
                        acc = await cm.mark_failed_and_get_replacement(acc, "quota_exhausted", attempt=attempt)
                    except HTTPException as he:
                        return JSONResponse(
                            status_code=500,
                            content={"error": {"message": f"换号失败: {he.detail}", "type": "no_account"}},
                        )
            elif _is_auth_error(e):
                log.warning(
                    "non-stream attempt %d auth 错误 [%s]: %s，换号重试",
                    attempt, acc.email if acc else "?", e,
                )
                if acc:
                    try:
                        # ★ 增加 attempt=attempt 传参
                        acc = await cm.mark_failed_and_get_replacement(acc, "cookie_expired", attempt=attempt)
                    except HTTPException as he:
                        return JSONResponse(
                            status_code=500,
                            content={"error": {"message": f"换号失败: {he.detail}",
                                               "type": "no_account"}},
                        )
            else:
                log.warning("non-stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(_backoff(attempt))
                else:
                    traceback.print_exc()

    return JSONResponse(
        status_code=502,
        content={"error": {"message": str(last_error), "type": "upstream_error"}},
    )