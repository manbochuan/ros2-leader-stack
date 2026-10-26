#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百炼工作流 ↔ OpenAI 兼容 转换层（shim）

作用
----
让 demo-partner（梧桐 ACPs 的 Partner 示例）以为自己在调用 OpenAI 接口，
实际上把请求转发给阿里云百炼的工作流应用。

    demo-partner ──OpenAI格式──> 本服务 ──百炼app格式──> 百炼工作流

这样 demo-partner 一行代码都不用改，只改三个环境变量即可。

对外提供
--------
POST /v1/chat/completions   支持 stream=true / false
GET  /v1/models
GET  /health

环境变量
--------
DASHSCOPE_API_KEY     必填，百炼 API-KEY
BAILIAN_APP_ID        必填，百炼工作流应用 ID
BAILIAN_INPUT_FIELD   可选，工作流入参字段名，默认 prompt
BAILIAN_BASE_URL      可选，默认 https://dashscope.aliyuncs.com
BAILIAN_WORKSPACE     可选，业务空间 ID，填了会带 X-DashScope-WorkSpace 头
BAILIAN_TIMEOUT       可选，上游超时秒数，默认 120
MESSAGE_MODE          可选，last_user（默认）或 all
USE_SESSION           可选，1 时把 OpenAI 的 user 字段当作百炼 session_id
SHIM_API_KEY          可选，本层自己的鉴权 key
MODEL_NAME            可选，对外暴露的模型名，默认 bailian-workflow
LOG_LEVEL             可选，默认 INFO
HOST / PORT           可选，仅直接 python shim.py 时生效，默认 127.0.0.1:9099
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


# --------------------------------------------------------------------------
# .env 加载（不依赖 python-dotenv；systemd 走 EnvironmentFile 时不生效也无妨）
# --------------------------------------------------------------------------
def load_dotenv(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except OSError:
        pass


load_dotenv(os.environ.get("ENV_FILE", ".env"))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
BAILIAN_APP_ID = os.environ.get("BAILIAN_APP_ID", "").strip()
BAILIAN_BASE_URL = os.environ.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com").rstrip("/")
BAILIAN_INPUT_FIELD = os.environ.get("BAILIAN_INPUT_FIELD", "prompt").strip() or "prompt"
BAILIAN_WORKSPACE = os.environ.get("BAILIAN_WORKSPACE", "").strip()
BAILIAN_TIMEOUT = float(os.environ.get("BAILIAN_TIMEOUT", "120"))
MESSAGE_MODE = os.environ.get("MESSAGE_MODE", "last_user").strip()
USE_SESSION = os.environ.get("USE_SESSION", "0").strip() == "1"
SHIM_API_KEY = os.environ.get("SHIM_API_KEY", "").strip()
MODEL_NAME = os.environ.get("MODEL_NAME", "bailian-workflow").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# ---------- biz_params（百炼工作流「开始节点」的自定义变量）----------
# 有些工作流的入参不只有 sys.query，还要通过 biz_params 传结构化变量
# （例如 requirement_json / node_spec_json / leader_context / history）。
# 调用方（Partner / 编排层）把结构化参数放进 prompt 里的标记块，
# shim 解析出来转成 biz_params，从 prompt 里删掉。
BIZ_BEGIN_MARKER = os.environ.get("BIZ_BEGIN_MARKER", "<<<BIZ_PARAMS>>>").strip()
BIZ_END_MARKER = os.environ.get("BIZ_END_MARKER", "<<<END_BIZ_PARAMS>>>").strip()
# prompt 整体就是一个 JSON 对象时，是否直接当作 biz_params（1 = 是）
BIZ_AUTO_JSON = os.environ.get("BIZ_AUTO_JSON", "0").strip() == "1"
# 百炼流式开关：机器要拿完整 JSON，默认关闭
FLOW_STREAM_MODE = os.environ.get("FLOW_STREAM_MODE", "none").strip()


def _parse_apps(raw: str) -> dict[str, str]:
    """解析 BAILIAN_APPS，格式：  模型名=app_id,模型名=app_id"""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key, val = key.strip(), val.strip()
        if key and val:
            out[key] = val
    return out


# 多个百炼应用的路由表： 请求体里的 model 名 -> 百炼 app_id
# 例： BAILIAN_APPS=ros2_codegen=abc123,ros2_plan=def456,ros2_analyze=ghi789
BAILIAN_APPS = _parse_apps(os.environ.get("BAILIAN_APPS", ""))
# 向后兼容：只配了单个 BAILIAN_APP_ID 时，用 MODEL_NAME 作为它的 model 名
if BAILIAN_APP_ID:
    BAILIAN_APPS.setdefault(MODEL_NAME, BAILIAN_APP_ID)


def bailian_url(app_id: str) -> str:
    return f"{BAILIAN_BASE_URL}/api/v1/apps/{app_id}/completion"


# 兼容旧变量（默认那个应用的地址）
BAILIAN_URL = bailian_url(BAILIAN_APP_ID) if BAILIAN_APP_ID else ""

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("shim")


class BailianError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"bailian {status}: {body}")
        self.status = status
        self.body = body


# --------------------------------------------------------------------------
# 百炼调用
# --------------------------------------------------------------------------
def _bailian_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    if BAILIAN_WORKSPACE:
        headers["X-DashScope-WorkSpace"] = BAILIAN_WORKSPACE
    return headers


def extract_biz(prompt: str) -> tuple[str, dict[str, Any]]:
    """
    从 prompt 里拆出结构化参数（biz_params）。

    两种写法：

      1) 标记块（默认方式）
             <自然语言需求>

             <<<BIZ_PARAMS>>>
             {"requirement_json": "{\\"project\\": ...}"}
             <<<END_BIZ_PARAMS>>>

      2) 整个 prompt 就是一个 JSON 对象（需要 BIZ_AUTO_JSON=1）

    返回 (去掉结构化块之后的 prompt, biz_params 字典)。
    不匹配时原样返回，biz_params 为空字典。
    """
    if not prompt:
        return prompt, {}

    text = prompt
    begin, end = BIZ_BEGIN_MARKER, BIZ_END_MARKER

    if begin and end and begin in text:
        head, _, rest = text.partition(begin)
        raw, sep, tail = rest.partition(end)
        if sep:
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, dict):
                    clean = (head + tail).strip()
                    log.info("biz_params 标记块解析成功：字段=%s", sorted(parsed.keys()))
                    return clean, parsed
                log.warning("biz_params 标记块不是 JSON 对象，已忽略")
            except Exception as exc:  # noqa: BLE001
                log.warning("biz_params 标记块解析失败：%s", exc)
        else:
            log.warning("biz_params 标记块缺少结束标记 %s", end)

    if BIZ_AUTO_JSON:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, dict) and parsed:
                log.info("prompt 整体是 JSON，按 biz_params 处理：字段=%s", sorted(parsed.keys()))
                return "", parsed

    return text, {}


async def call_bailian(prompt: str, session_id: str | None = None,
                       app_id: str = "",
                       biz_params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = bailian_url(app_id or BAILIAN_APP_ID)

    # 百炼的 input.prompt 是必填的。工作流如果只吃结构化变量，
    # 调用方会把 prompt 剥空 —— 这时补一个占位值，否则百炼直接报
    # InvalidParameter: Required parameter(Prompt) missing or invalid
    if not prompt.strip():
        prompt = "（本请求为结构化输入，参数见 biz_params）"

    payload: dict[str, Any] = {
        "input": {BAILIAN_INPUT_FIELD: prompt},
        "parameters": {"flow_stream_mode": FLOW_STREAM_MODE},
        "debug": {},
    }
    if biz_params:
        # ★ 按阿里云官方 API 参考：调「工作流应用」时 biz_params 放在 input 里，
        #   且「开始节点」的自定义变量直接作为它的键。
        #   （顶层 biz_params 会被忽略；user_defined_params 那层是给
        #     智能体应用传「插件参数」用的，不是给工作流变量用的。）
        payload["input"]["biz_params"] = biz_params
    if USE_SESSION and session_id:
        payload["input"]["session_id"] = session_id

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=BAILIAN_TIMEOUT) as client:
        for attempt in range(3):
            try:
                resp = await client.post(url, headers=_bailian_headers(), json=payload)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    log.warning("百炼 %s，第 %d 次重试", resp.status_code, attempt + 1)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise BailianError(resp.status_code, resp.text[:800])
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                # 上游只是慢，不是故障。重试只会把总耗时变成三倍，直接失败更快暴露问题。
                log.error("百炼超时（%.0fs），工作流太慢，不再重试", BAILIAN_TIMEOUT)
                raise BailianError(
                    598, f"上游超时（{BAILIAN_TIMEOUT:.0f}s），工作流响应过慢：{exc!r}"
                ) from exc
            except httpx.HTTPError as exc:
                last_err = exc
                if attempt < 2:
                    # 用 %r，异常消息为空时也能看到类型名
                    log.warning("百炼网络异常 %r，第 %d 次重试", exc, attempt + 1)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise BailianError(599, f"network error: {exc!r}") from exc
    raise BailianError(599, f"network error: {last_err}")


def extract_text(data: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    output = data.get("output") or {}
    text = output.get("text")
    if not isinstance(text, str) or not text.strip():
        # 工作流输出结构不标准时的兜底：把 output 原样交出去，便于排查
        text = json.dumps(output, ensure_ascii=False) if output else ""
        if text:
            log.warning("output.text 为空，已回退为 output 原文")
    return text, output.get("session_id"), (data.get("usage") or {})


def map_usage(usage: dict[str, Any], prompt: str, text: str) -> dict[str, int]:
    pt = ot = 0
    for model in usage.get("models") or []:
        if isinstance(model, dict):
            pt += int(model.get("input_tokens") or 0)
            ot += int(model.get("output_tokens") or 0)
    if pt == 0 and ot == 0:
        # 百炼某些工作流不回 usage；给个粗估，避免客户端以为计费异常
        pt = max(1, len(prompt) // 4)
        ot = max(1, len(text) // 4)
    return {"prompt_tokens": pt, "completion_tokens": ot, "total_tokens": pt + ot}


# --------------------------------------------------------------------------
# OpenAI 消息 → 百炼 prompt
# --------------------------------------------------------------------------
def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and part.get("text"):
                    chunks.append(str(part["text"]))
                elif isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif part.get("type") == "image_url":
                    chunks.append("[图片]")
        return "\n".join(c for c in chunks if c)
    return "" if content is None else str(content)


def build_prompt(messages: list[dict[str, Any]]) -> str:
    if MESSAGE_MODE == "all":
        parts: list[str] = []
        for msg in messages:
            text = content_to_text(msg.get("content"))
            if text:
                parts.append(f"{msg.get('role', 'user')}: {text}")
        return "\n\n".join(parts)

    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = content_to_text(msg.get("content"))
            if text:
                return text
    for msg in reversed(messages):
        text = content_to_text(msg.get("content"))
        if text:
            return text
    return ""


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------
def _chunk_event(rid: str, model: str, delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def sse_stream(rid: str, model: str, text: str) -> AsyncIterator[str]:
    yield _chunk_event(rid, model, {"role": "assistant", "content": ""})
    size = 80
    for i in range(0, len(text), size):
        yield _chunk_event(rid, model, {"content": text[i : i + size]})
    yield _chunk_event(rid, model, {}, finish="stop")
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------
# FastAPI
# --------------------------------------------------------------------------
app = FastAPI(title="Bailian Workflow OpenAI-Compatible Shim", version="1.0.0")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if SHIM_API_KEY and request.url.path.startswith("/v1"):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token and token != SHIM_API_KEY:
            return JSONResponse(
                {"error": {"message": "invalid api key", "type": "invalid_request_error"}},
                status_code=401,
            )
    return await call_next(request)


@app.get("/health")
async def health() -> JSONResponse:
    problems: list[str] = []
    if not DASHSCOPE_API_KEY:
        problems.append("DASHSCOPE_API_KEY 未配置")
    if not BAILIAN_APPS:
        problems.append("BAILIAN_APPS（或 BAILIAN_APP_ID）未配置")
    body = {
        "status": "error" if problems else "ok",
        "problems": problems,
        "models": sorted(BAILIAN_APPS),
        "apps": BAILIAN_APPS,
        "input_field": BAILIAN_INPUT_FIELD,
        "message_mode": MESSAGE_MODE,
        "biz_begin_marker": BIZ_BEGIN_MARKER,
        "biz_end_marker": BIZ_END_MARKER,
        "biz_auto_json": BIZ_AUTO_JSON,
        "flow_stream_mode": FLOW_STREAM_MODE,
    }
    return JSONResponse(body, status_code=503 if problems else 200)


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    names = sorted(BAILIAN_APPS) or [MODEL_NAME]
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "aliyun-bailian",
            }
            for name in names
        ],
    }


async def _handle_chat(request: Request) -> JSONResponse | StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}},
            status_code=400,
        )

    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    prompt = build_prompt(messages)
    if not prompt.strip():
        return JSONResponse(
            {"error": {"message": "messages 为空或没有可用文本", "type": "invalid_request_error"}},
            status_code=400,
        )

    # 拆出结构化参数（biz_params）—— 有些工作流的入参不只有 sys.query
    prompt, biz_params = extract_biz(prompt)
    if not prompt.strip() and not biz_params:
        return JSONResponse(
            {"error": {"message": "去掉结构化块之后没有任何可用文本", "type": "invalid_request_error"}},
            status_code=400,
        )

    want_stream = bool(body.get("stream"))
    model = body.get("model") or MODEL_NAME
    session_id = body.get("user") if isinstance(body.get("user"), str) else None

    # 按 model 名选百炼应用
    app_id = BAILIAN_APPS.get(model)
    if not app_id:
        return JSONResponse(
            {
                "error": {
                    "message": f"未知的 model「{model}」。可用的 model：{sorted(BAILIAN_APPS) or '（未配置任何百炼应用）'}",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    started = time.time()
    try:
        data = await call_bailian(prompt, session_id, app_id, biz_params)
    except BailianError as exc:
        log.error("百炼调用失败 status=%s body=%s", exc.status, exc.body)
        # 598 = 上游超时，用 504 让客户端一眼看出是网关超时而不是后端错误
        http_status = 504 if exc.status == 598 else 502
        return JSONResponse(
            {"error": {"message": f"百炼返回错误 {exc.status}: {exc.body}", "type": "upstream_error"}},
            status_code=http_status,
        )

    text, bailian_session, usage = extract_text(data)
    rid = data.get("request_id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    log.info(
        "ok rid=%s prompt_chars=%d out_chars=%d %.2fs session=%s stream=%s biz=%s",
        data.get("request_id"),
        len(prompt),
        len(text),
        time.time() - started,
        bailian_session,
        want_stream,
        sorted(biz_params) if biz_params else "-",
    )

    if want_stream:
        return StreamingResponse(
            sse_stream(rid, model, text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return JSONResponse(
        {
            "id": rid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": map_usage(usage, prompt, text),
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _handle_chat(request)


@app.post("/chat/completions")  # 兼容 base_url 不带 /v1 的客户端
async def chat_completions_alias(request: Request):
    return await _handle_chat(request)


# --------------------------------------------------------------------------
# 直接运行：python shim.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "9099"))
    log.info("启动 shim：http://%s:%d", host, port)
    if BAILIAN_APPS:
        for name, aid in sorted(BAILIAN_APPS.items()):
            log.info("  model=%-18s -> %s", name, bailian_url(aid))
    else:
        log.warning("没有配置任何百炼应用：请设置 BAILIAN_APPS 或 BAILIAN_APP_ID")
    if not DASHSCOPE_API_KEY:
        log.warning("DASHSCOPE_API_KEY 未配置，所有调用都会失败")
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())
