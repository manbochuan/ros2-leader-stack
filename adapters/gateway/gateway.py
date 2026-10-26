#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACPs 出站网关（acps-gateway）

作用
----
让百炼侧（只会发 HTTP 的东西）能够调用梧桐平台上的 ACPs 智能体。

    百炼插件 ──HTTP(JSON)──→ 本服务 ──mTLS + AIP──→ 梧桐 Partner
                               ↑
                     客户端证书在这里被使用，从不离开服务器

对百炼暴露的接口（会出现在 /openapi.json 里）
--------------------------------------------
    POST /wf/<搭档名>    按 targets.json 里的搭档名调用（每个搭档一个独立操作）
    GET  /ping           连通性测试

不暴露给百炼的接口（include_in_schema=False，但可用）
---------------------------------------------------
    GET  /health         健康检查
    POST /invoke         通用调用（自己指定 endpoint / skill）
    POST /discover       按需求查平台上有哪些智能体可用
    POST /wf/{name}      通用兜底（新增搭档后不用改代码也能调）

鉴权
----
    除 /ping 和 /health 外，都需要请求头： X-Auth-Token: <AUTH_TOKEN>
    这个头不出现在 OpenAPI 里 —— 它在百炼插件的「Header 列表」里配。

配置
----
    见同目录 .env.example 与 targets.json
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(Path(os.environ.get("ENV_FILE", BASE_DIR / ".env")))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


HOST = env("GATEWAY_HOST", "0.0.0.0")
PORT = int(env("GATEWAY_PORT", "9100"))
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", f"http://YOUR_HOST:{PORT}")
AUTH_TOKEN = env("AUTH_TOKEN", "")
MY_AIC = env("MY_AIC", "1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD")

CLI_DIR = Path(env("ACPS_CLI_DIR", "/opt/acps-agent/acps-community/acps-cli"))
CLI_BIN = Path(env("ACPS_CLI_BIN", str(CLI_DIR / ".venv/bin/acps-cli")))
CLI_CFG = env("ACPS_CLI_CONFIG", "acps-cli.toml")

CERT_FILE = env("CLIENT_CERT", "/opt/acps-agent/acps-community/acps-cli/private/agent-cert.pem")
KEY_FILE = env("CLIENT_KEY", "/opt/acps-agent/acps-community/acps-cli/private/agent-key.pem")
CA_FILE = env("CA_BUNDLE", "/opt/acps-agent/acps-community/acps-cli/private/trust-bundle.pem")

DEFAULT_TIMEOUT = float(env("DEFAULT_TIMEOUT_SEC", "180"))
POLL_INTERVAL = float(env("POLL_INTERVAL_SEC", "2.0"))
TARGETS_FILE = Path(env("TARGETS_FILE", str(BASE_DIR / "targets.json")))

# 对端服务器证书的校验强度：
#   chain  = 验证证书链（确认对方是平台 CA 签发的合法智能体），但不校验主机名 —— 默认
#   strict = 证书链 + 主机名/IP 全校验
#   off    = 完全不校验（仅本地调试用）
#
# 为什么默认是 chain？
#   平台上不少智能体的 ACS 里没有声明 certificate.altNames.ip，所以它们的证书
#   SAN 里没有自己的公网 IP，严格校验会直接拒连（实测：北京城区旅游智能体 9540 端口）。
#   而这些证书都是同一个平台 CA 签发的，链验证通过已经能确认"它是平台上的注册智能体"。
VERIFY_SERVER_CERT = env("VERIFY_SERVER_CERT", "chain").lower()

# /ask_platform 里，一个搭档调失败后最多再试几个（按平台给出的相关度顺序）
# 语义检索的第一名不一定最合适（实测：检索"用户健康档案采集"会选中别家的搭档），
# 失败回退能明显提升成功率。设 1 表示不回退。
ASK_PLATFORM_MAX_TRIES = int(env("ASK_PLATFORM_MAX_TRIES", "5"))

# 单次尝试的超时上限
ASK_PLATFORM_TIMEOUT = float(env("ASK_PLATFORM_TIMEOUT_SEC", "60"))

# 所有尝试加起来的总时间预算（秒）。先到先算：
#   · 证书错/连不上这类是秒级失败 → 能在预算内试很多个
#   · 卡在 accepted / 超时这类很慢 → 预算用完就停
ASK_PLATFORM_TOTAL_BUDGET = float(env("ASK_PLATFORM_TOTAL_BUDGET_SEC", "90"))

# 「接受任务后一直不开始处理」的容忍秒数。
# 实测遇到过：对方回了 accepted 就再没动静，一直白等到超时。
# 状态停在 accepted 超过这个时间，就判定对方异常，提前放弃去试下一个。
ACCEPTED_STALL_SEC = float(env("ACCEPTED_STALL_SEC", "20"))

LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("acps-gateway")


# ---------------------------------------------------------------------------
# 搭档映射表
# ---------------------------------------------------------------------------
def load_targets() -> dict[str, dict]:
    if not TARGETS_FILE.exists():
        log.warning("搭档配置不存在：%s", TARGETS_FILE)
        return {}
    try:
        data = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        log.error("读搭档配置失败：%s", exc)
        return {}


TARGETS = load_targets()


# ---------------------------------------------------------------------------
# 请求 / 响应模型（这些会变成百炼插件里看到的参数表）
# ---------------------------------------------------------------------------
class TopicSpec(BaseModel):
    name: str = Field(..., description="话题名，以 / 开头，例如 /chatter")
    msg_type: str = Field(
        default="std_msgs/msg/String",
        description="消息类型全限定名，例如 std_msgs/msg/String、sensor_msgs/msg/LaserScan",
    )


class TextRequest(BaseModel):
    """只传一段自然语言需求的调用（项目规划 / 需求分析）"""
    input: str = Field(..., description="用自然语言描述的需求，例如“帮我做一个避障小车项目”")
    session: str | None = Field(default=None, description="会话标识，用于关联多轮（可省略）")
    timeout_sec: float | None = Field(default=None, description="超时秒数（可省略）")


class CodegenRequest(BaseModel):
    """生成单个节点代码。可以只给 input，也可以给结构化规格（更稳定）"""
    input: str | None = Field(
        default=None,
        description="自然语言描述。若同时提供了下面的结构化字段，以结构化字段为准",
    )
    node_name: str | None = Field(default=None, description="节点名，小写+下划线，以 _node 结尾")
    class_name: str | None = Field(default=None, description="类名，大驼峰，以 Node 结尾")
    what_to_do: str | None = Field(default=None, description="一句话说明这个节点做什么")
    publish_topics: list[TopicSpec] | None = Field(default=None, description="该节点发布的话题列表")
    subscribe_topics: list[TopicSpec] | None = Field(default=None, description="该节点订阅的话题列表")
    core_logic: str | None = Field(default=None, description="核心逻辑说明（可省略）")
    extra_features: list[str] | None = Field(default=None, description="额外特性（可省略）")
    student_level: str | None = Field(default=None, description="beginner 或 advanced（可省略）")
    timeout_sec: float | None = Field(default=None, description="超时秒数（可省略）")


class CallResult(BaseModel):
    ok: bool = Field(..., description="是否成功")
    target: str | None = Field(default=None, description="实际调用的搭档名")
    state: str | None = Field(default=None, description="任务最终状态")
    task_id: str | None = Field(default=None, description="任务号，便于排查")
    text: str | None = Field(default=None, description="搭档返回的内容（代码 / 规划书 / 分析结果）")
    elapsed_sec: float | None = Field(default=None, description="耗时（秒）")
    error: str | None = Field(default=None, description="失败原因")


# ---------------------------------------------------------------------------
# 鉴权（这个头不出现在 OpenAPI 里，在百炼插件的 Header 列表里配）
# ---------------------------------------------------------------------------
def require_token(
    x_auth_token: str | None = Header(default=None, include_in_schema=False),
) -> None:
    if not AUTH_TOKEN:
        return
    if x_auth_token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="X-Auth-Token 不正确")


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ROS2 智能体出站调用网关",
    version="0.2.0",
    description=(
        "把「生成 ROS2 节点代码 / 生成项目规划书 / 需求分析」这三件事，"
        "转发给梧桐智能体互联平台上的搭档智能体。\n\n"
        "每个操作都返回结构化结果，其中 text 字段是直接可以展示给用户的内容。"
    ),
    servers=[{"url": PUBLIC_BASE_URL}],
)


# ---------------------------------------------------------------------------
# AIP 出站调用
# ---------------------------------------------------------------------------
_SSL_CTX: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """
    自己构建出站用的 SSL 上下文。

    为什么不用 httpx 的 cert=/verify= 参数？
    ---------------------------------------------------------------
    实测（同一台机器、同一个端点、同一套证书）：

        httpx  (verify=<CA路径>, cert=(crt,key))   -> RemoteProtocolError
                                                      Server disconnected
        httpx  (verify=False,   cert=(crt,key))    -> 200 OK
        自建 context（cafile=CA）+ load_cert_chain -> 200 OK
        自建 context + 强制 TLS1.2                 -> 200 OK

    也就是说 httpx 内部自己那条路径下，服务端会拒绝客户端证书并把连接断开
    （TLS1.3 下客户端只能看到"连接被断开"，看不到证书错误）。
    而 curl 用同样的证书一切正常，说明证书和网络都没问题。

    结论：走"自建 context"这条实测可用的路径。
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX

    ca = Path(CA_FILE)
    if ca.exists():
        ctx = ssl.create_default_context(cafile=str(ca))
    else:
        log.warning("CA 信任包不存在（%s），将不校验服务端证书", CA_FILE)
        ctx = ssl.create_default_context()

    # 按配置调整"链验证 / 主机名校验"的组合
    if VERIFY_SERVER_CERT == "off":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("VERIFY_SERVER_CERT=off：不校验对端证书，仅建议本地调试使用")
    elif VERIFY_SERVER_CERT == "strict":
        pass  # create_default_context 的默认就是 链验证 + 主机名校验
    else:  # chain（默认）
        # 注意顺序：先关主机名校验（这会把 verify_mode 置成 CERT_NONE），
        # 再把 verify_mode 设回 CERT_REQUIRED，得到"验链但不验主机名"
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED

    if Path(CERT_FILE).exists() and Path(KEY_FILE).exists():
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    else:
        log.error("客户端证书或私钥不存在，出站调用会被服务端拒绝")

    ctx.set_alpn_protocols(["http/1.1"])
    _SSL_CTX = ctx
    return ctx


def _rpc(endpoint: str, payload: dict, timeout: float) -> dict:
    """发一条 AIP 报文，带客户端证书做 mTLS"""
    with httpx.Client(verify=_ssl_context(), timeout=timeout) as client:
        resp = client.post(endpoint, json=payload,
                           headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        # 对端可能返回 null / 数组，别让后面的 .get() 崩掉
        raise ValueError(f"对端返回的不是 JSON 对象：{str(data)[:200]}")
    return data


def _command(endpoint: str, command: str, task_id: str, text: str | None,
             timeout: float, data_type: str = "text") -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "rpc",
        "params": {
            "command": {
                "type": "task-command",
                "id": f"gw-{uuid.uuid4()}",
                "sentAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "senderRole": "leader",
                "senderId": MY_AIC,
                "mentions": None,
                "dataItems": ([{"metadata": None, "type": data_type, "text": text}]
                              if text else None),
                "groupId": None,
                "sessionId": f"gw-{task_id}",
                "command": command,
                "commandParams": None,
                "taskId": task_id,
            }
        },
    }
    return _rpc(endpoint, body, timeout)


def _extract_text(result: dict) -> str:
    """从 TaskResult 里把产物文本抠出来。抓不到 text 时兜底返回整个结果的 JSON。"""
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                if not node["text"].startswith("Task completed"):
                    chunks.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(result.get("products"))
    if chunks:
        return "\n\n".join(chunks)
    walk(result.get("dataItems"))
    if chunks:
        return "\n\n".join(chunks)

    # 兜底：对方没有按 text 类型返回（例如结构化/JSON 输出），
    # 就把整个结果序列化交出去，至少调用方能读到内容，而不是拿到空字符串。
    log.warning("产物里没有 text 类型的内容，回退为原始结果 JSON")
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return str(result)


def aip_call(endpoint: str, skill: str, text: str, timeout: float,
             data_type: str = "text") -> dict:
    """完整走一遍：start → 轮询 get → 取产物 → complete"""
    started = time.time()
    task_id = f"gw-{uuid.uuid4()}"

    log.info("→ start  endpoint=%s skill=%s type=%s task=%s",
             endpoint, skill or "-", data_type, task_id)
    try:
        first = _command(endpoint, "start", task_id, text, timeout, data_type)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "connect-failed", "task_id": task_id,
                "error": repr(exc)[:400]}

    # JSON-RPC 层的错误（对方明确拒绝时，原因在这里）
    if first.get("error"):
        log.warning("  start 返回 JSON-RPC error：%s",
                    json.dumps(first["error"], ensure_ascii=False)[:300])
        return {"ok": False, "state": "rpc-error", "task_id": task_id,
                "error": json.dumps(first["error"], ensure_ascii=False)[:400]}

    # 注意用 or {}：result 键存在但值是 null 时，默认值 {} 不会生效
    result = first.get("result") or {}
    state = (result.get("status") or {}).get("state", "?")
    log.info("  start 返回 state=%s", state)

    if state in ("rejected", "failed"):
        return {"ok": False, "state": state, "task_id": task_id,
                "error": json.dumps(result.get("status"), ensure_ascii=False)[:500]}

    deadline = started + timeout
    poll_errors = 0
    while time.time() < deadline:
        if state in ("awaiting-completion", "completed"):
            break
        # ★ awaiting-input = 对方在等"用户补充信息"（相当于追问）。
        #   它不会自己往前走到 awaiting-completion，继续等只会白等到超时
        #   （实测：有个搭档卡在这里，白等了 60 秒）。所以立即结束轮询，
        #   把它的追问内容带回去，让上游决定"是追问学生还是换一个搭档"。
        if state == "awaiting-input":
            log.info("  对方处于 awaiting-input（等用户补充信息），提前结束轮询")
            break
        # ★ 一直卡在 accepted，说明对方压根没开始处理（实测遇到过，白等到超时）
        if state == "accepted" and (time.time() - started) > ACCEPTED_STALL_SEC:
            return {"ok": False, "state": "stalled", "task_id": task_id,
                    "error": (f"对方接受任务后 {ACCEPTED_STALL_SEC:.0f}s 仍未开始处理"
                              f"（状态一直停在 accepted），判定为对方异常")}
        time.sleep(POLL_INTERVAL)
        try:
            got = _command(endpoint, "get", task_id, None, timeout)
        except Exception as exc:  # noqa: BLE001
            poll_errors += 1
            log.warning("  get 失败（第 %d 次）：%s", poll_errors, repr(exc)[:150])
            if poll_errors >= 5:
                return {"ok": False, "state": state, "task_id": task_id,
                        "error": f"轮询连续失败 {poll_errors} 次：{repr(exc)[:200]}"}
            continue
        if got.get("error"):
            log.warning("  get 返回 JSON-RPC error：%s",
                        json.dumps(got["error"], ensure_ascii=False)[:300])
            return {"ok": False, "state": "rpc-error", "task_id": task_id,
                    "error": json.dumps(got["error"], ensure_ascii=False)[:400]}
        result = got.get("result") or {}
        state = (result.get("status") or {}).get("state", "?")
        log.info("  get   state=%s  (%.0fs)", state, time.time() - started)
        if state in ("failed", "rejected", "canceled"):
            return {"ok": False, "state": state, "task_id": task_id,
                    "error": json.dumps(result.get("status"), ensure_ascii=False)[:500]}

    # 对方在等用户补充信息 → 不是失败，是"它要追问"。把内容带回去，交上游决定。
    if state == "awaiting-input":
        return {"ok": False, "state": "awaiting-input", "task_id": task_id,
                "error": ("对方在等待补充信息（awaiting-input）："
                          + (_extract_text(result)[:300] or "(未返回内容)"))}

    if state not in ("awaiting-completion", "completed"):
        return {"ok": False, "state": state, "task_id": task_id,
                "error": f"超时（{timeout:.0f}s），最后状态={state}"}

    text_out = _extract_text(result)

    try:
        _command(endpoint, "complete", task_id, None, 20)
    except Exception as exc:  # noqa: BLE001
        log.warning("complete 失败（不影响结果）：%s", repr(exc)[:150])

    return {"ok": True, "state": state, "task_id": task_id,
            "text": text_out, "elapsed_sec": round(time.time() - started, 2)}


# ---------------------------------------------------------------------------
# 内部：把请求体转成给搭档的文本
# ---------------------------------------------------------------------------
def _payload_to_text(req: BaseModel) -> str:
    data = req.model_dump(exclude_none=True)
    for key in ("input", "text"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return json.dumps(data, ensure_ascii=False)


async def _do_wf_async(name: str, req: BaseModel | dict,
                       timeout_override: float | None = None) -> dict:
    """给异步路由用的包装（把阻塞的 aip_call 丢到线程池）"""
    return await run_in_threadpool(_do_wf, name, req, timeout_override)


def _do_wf(name: str, req: BaseModel | dict,
           timeout_override: float | None = None) -> dict:
    """真正干活的地方。刻意写成同步函数 —— aip_call 全程是阻塞的，
    同步路由里直接调（FastAPI 会把同步路由丢到线程池），异步路由里用
    _do_wf_async 包一层。"""
    target = TARGETS.get(name)
    if not target:
        raise HTTPException(status_code=404,
                            detail=f"未配置搭档 '{name}'，可用：{sorted(TARGETS.keys())}")

    if isinstance(req, BaseModel):
        text = _payload_to_text(req)
        body = req.model_dump(exclude_none=True)
    else:
        body = req
        text = body.get("input") or body.get("text")
        if not isinstance(text, str) or not text.strip():
            text = json.dumps(body, ensure_ascii=False)

    timeout = float(timeout_override or target.get("timeout_sec") or DEFAULT_TIMEOUT)
    try:
        out = aip_call(target["endpoint"], target.get("skill", ""), text, timeout)
    except Exception as exc:  # noqa: BLE001
        # 任何意外都包成结构化结果返回，绝不给百炼抛 500
        log.exception("调用搭档 %s 时发生未预期的异常", name)
        return {"ok": False, "target": name, "state": "internal-error",
                "error": repr(exc)[:400]}
    out["target"] = name
    return out


# ---------------------------------------------------------------------------
# 对百炼暴露的接口
# ---------------------------------------------------------------------------
@app.get("/ping", summary="连通性测试", tags=["系统"],
         description="返回网关是否在线、自己的 AIC、已配置的搭档列表。用于确认百炼能否访问到本服务。")
def ping_get():
    return {"ok": True, "service": "acps-gateway", "ts": time.time(),
            "my_aic": MY_AIC, "targets": sorted(TARGETS.keys())}


@app.post("/wf/ros2_codegen", response_model=CallResult, tags=["搭档"],
          summary="生成单个 ROS2 节点代码",
          description=("根据自然语言描述或结构化规格，生成一个可直接运行的 ROS2 Python 节点代码，"
                       "并附带运行步骤。适合「帮我写一个发布 hello world 的节点」这类单节点需求。\n\n"
                       "优先传结构化字段（node_name / class_name / what_to_do / publish_topics / "
                       "subscribe_topics），结果更稳定；也可以只传 input。"))
def wf_ros2_codegen(req: CodegenRequest, _: None = Depends(require_token)):
    return _do_wf("ros2_codegen", req, req.timeout_sec)


@app.post("/wf/ros2_plan", response_model=CallResult, tags=["搭档"],
          summary="生成 ROS2 项目规划书",
          description=("根据项目需求生成完整的项目规划书与学习指南：包含节点划分、话题设计、"
                       "文件结构、学习路径。适合「帮我做一个避障小车项目」这类多节点项目需求。"))
def wf_ros2_plan(req: TextRequest, _: None = Depends(require_token)):
    return _do_wf("ros2_plan", req, req.timeout_sec)


@app.post("/wf/ros2_analyze", response_model=CallResult, tags=["搭档"],
          summary="ROS2 需求分析（拆功能 / 划业务流）",
          description=("对模糊的项目需求做需求分析：判断属于单节点还是项目模式、拆解功能模块、"
                       "规划业务流，输出结构化结果。适合在生成规划书之前先做一步需求梳理。"))
def wf_ros2_analyze(req: TextRequest, _: None = Depends(require_token)):
    return _do_wf("ros2_analyze", req, req.timeout_sec)


# ---------------------------------------------------------------------------
# 不暴露给百炼、但可用的接口
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "service": "acps-gateway",
            "cert_ok": Path(CERT_FILE).exists() and Path(KEY_FILE).exists(),
            "ca_ok": Path(CA_FILE).exists(),
            "targets": sorted(TARGETS.keys())}


@app.post("/wf/{name}", include_in_schema=False)
async def wf_generic(name: str, request: Request, _: None = Depends(require_token)):
    """通用兜底：新增搭档后，不改代码也能调（POST /wf/<搭档名>）"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {"input": (await request.body()).decode("utf-8", "ignore")}
    return await _do_wf_async(name, body)


@app.post("/invoke", include_in_schema=False)
def invoke(payload: dict = Body(...), _: None = Depends(require_token)):
    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="缺少 endpoint")
    text = payload.get("input") or payload.get("text") or ""
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    timeout = float(payload.get("timeout_sec") or DEFAULT_TIMEOUT)
    try:
        return aip_call(endpoint, payload.get("skill", ""), text, timeout)
    except Exception as exc:  # noqa: BLE001
        log.exception("invoke 失败")
        return JSONResponse(status_code=502, content={"ok": False, "error": repr(exc)[:400]})


def _run_acps_cli(args: list[str], timeout: int = 60) -> tuple[bool, Any]:
    """跑一次 acps-cli，返回 (成功, 解析后的 JSON / 错误文本)"""
    cmd = [str(CLI_BIN), "--config", CLI_CFG, *args]
    try:
        proc = subprocess.run(cmd, cwd=str(CLI_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)[:400]
    raw = (proc.stdout or "").strip()
    if not raw:
        return False, (proc.stderr or "命令无输出")[:400]
    try:
        return True, json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return False, f"输出不是 JSON：{exc}; 原文={(raw[:200])}"


def _candidates_from(acs_map: dict) -> list[dict]:
    """
    把平台的 acsMap 规整成候选列表。

    注意：ACS 里每个技能除了 name，还带着"能力契约"：
        description   —— 说明（里面有【输入要求】必填/可选）
        inputModes    —— 收 text/plain 还是 application/json
        outputModes   —— 返回什么格式
        examples      —— 调用示例（最能说明输入长什么样）
    这些以前被丢掉了，导致调用方不知道对方要什么。现在全部保留。
    """
    out: list[dict] = []
    for aic, acs in (acs_map or {}).items():
        eps = acs.get("endPoints") or []
        skills: list[dict] = []
        for s in (acs.get("skills") or []):
            if not isinstance(s, dict):
                continue
            skills.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description"),
                "inputModes": s.get("inputModes") or [],
                "outputModes": s.get("outputModes") or [],
                "examples": s.get("examples") or [],
                "tags": s.get("tags") or [],
                "version": s.get("version"),
            })
        out.append({
            "aic": aic,
            "name": acs.get("name"),
            "description": acs.get("description"),
            "endpoint": eps[0].get("url") if eps else None,
            "active": acs.get("active"),
            "skills": skills,
        })
    return out


def _fmt_modes(modes: list) -> str:
    modes = [str(m) for m in (modes or []) if m]
    return "、".join(modes) if modes else "未声明"


def _wants_json(skills: list) -> bool:
    """对方是否要求 application/json 输入"""
    for s in (skills or []):
        if "application/json" in (s.get("inputModes") or []):
            return True
    return False


def _summarize(cands: list[dict], max_chars: int = 3500) -> str:
    """
    给大模型看的纯文本摘要（百炼的输出参数表表达不了嵌套结构）。

    这里把 ACS 的"能力契约"完整透出来 —— 尤其是：
      · 输入格式（text/plain 还是 application/json）
      · description 里的【输入要求】必填/可选
      · 调用示例
    这样调用方的大模型就能自己判断"该问用户要什么信息"。
    """
    if not cands:
        return "没有找到能完成这件事的智能体。"

    lines: list[str] = []
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. {c.get('name') or '未命名'}（aic: {c.get('aic')}）")
        for s in (c.get("skills") or [])[:6]:
            modes_in = s.get("inputModes") or []
            head = f"   技能：{s.get('name') or s.get('id') or '未命名'}"
            if "application/json" in modes_in:
                head += "   ⚠️ 需要结构化 JSON 输入"
            lines.append(head)
            lines.append(f"     输入格式：{_fmt_modes(modes_in)}")
            lines.append(f"     输出格式：{_fmt_modes(s.get('outputModes'))}")

            desc = (s.get("description") or "").strip()
            if desc:
                for dl in desc.splitlines()[:10]:
                    dl = dl.strip()
                    if dl:
                        lines.append(f"     {dl}")

            ex = [e for e in (s.get("examples") or [])
                  if isinstance(e, str) and e.strip()]
            if ex:
                lines.append(f"     示例：{ex[0][:140]}")
        lines.append("")

    lines.append("要调用其中一个，把它的 aic 传给 call_partner。")
    lines.append("标了「需要结构化 JSON 输入」的搭档，要先把它要求的字段向用户收集齐，"
                 "组织成 JSON 放进 params（用户的问题放进 question 字段）。")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（内容过长已截断）"
    return text


# 缓存最近发现到的 aic -> 端点，供 /call_partner 解析
_DISCOVERED: dict[str, dict] = {}


def _resolve_endpoint(aic: str) -> tuple[str, str, list]:
    """由 AIC 查出端点、首个技能、以及全部技能（含能力契约）。先查缓存，再走结构化查询。"""
    hit = _DISCOVERED.get(aic)
    if hit and hit.get("endpoint"):
        skills = hit.get("skills") or []
        return hit["endpoint"], (skills[0].get("id") if skills else "") or "", skills

    flt = json.dumps({
        "conditions": [
            {"field": "aic", "op": "eq", "value": aic},
            {"field": "active", "op": "eq", "value": True},
        ]
    }, ensure_ascii=False)
    ok, data = _run_acps_cli(["discover", "query", "--type", "filtered",
                              "--filter-json", flt, "--limit", "1"])
    if not ok:
        return "", "", []
    cands = _candidates_from((data.get("result") or {}).get("acsMap") or {})
    for c in cands:
        if c["aic"] == aic and c.get("endpoint"):
            _DISCOVERED[aic] = c
            skills = c.get("skills") or []
            return c["endpoint"], (skills[0].get("id") if skills else "") or "", skills
    return "", "", []


def _build_payload(text: str, params: Any, skills: list) -> tuple[str, str]:
    """
    组装要发给搭档的内容。

    ★ AIP 的 dataItems[].type 是字面量，只接受 'text' / 'file'（实测报错原文：
      "Input should be 'text'"）。所以不管是自然语言还是 JSON，
      type 一律填 'text'，区别只在 text 字段里放什么。

    · 对方声明 application/json 输入 → 内容组装成 JSON 字符串
      （用户的问题放进 question 字段）
    · 否则 → 内容就是自然语言；如果给了 params，就附在文本后面

    返回 (内容, dataItem 的 type)
    """
    data: Any = None
    if params:
        if isinstance(params, (dict, list)):
            data = params
        else:
            try:
                data = json.loads(str(params))
            except Exception:  # noqa: BLE001
                data = {"params_raw": str(params)}

    if _wants_json(skills):
        if not isinstance(data, dict):
            data = {"data": data} if data is not None else {}
        data.setdefault("question", text)
        return json.dumps(data, ensure_ascii=False), "text"

    if data is None:
        return text, "text"

    return (f"{text}\n\n【结构化参数】\n{json.dumps(data, ensure_ascii=False)}"), "text"


@app.post("/discover", tags=["搭档"],
          summary="查询平台上能做某件事的智能体",
          description=("用自然语言描述要做的事，返回平台上能完成这件事的智能体列表。"
                       "当用户的需求超出你自己的能力范围时，先用这个工具查一查，"
                       "再用 call_partner 调用查到的智能体。"))
def discover(payload: dict = Body(...), _: None = Depends(require_token)):
    need = (payload.get("need") or payload.get("query") or "").strip()
    if not need:
        raise HTTPException(status_code=400, detail="缺少 need")
    limit = int(payload.get("limit") or 5)

    ok, data = _run_acps_cli(["discover", "query", need, "--limit", str(limit)])
    if not ok:
        return {"ok": False, "count": 0, "summary": "查询失败。", "error": str(data)[:400]}

    cands = _candidates_from((data.get("result") or {}).get("acsMap") or {})
    for c in cands:
        if c.get("endpoint"):
            _DISCOVERED[c["aic"]] = c

    return {"ok": True, "count": len(cands), "summary": _summarize(cands),
            "candidates": cands}


class CallPartnerRequest(BaseModel):
    aic: str = Field(..., description="要调用的智能体 AIC（先用 discover 查到）")
    input: str = Field(..., description="要它做的事，用自然语言描述")
    params: str | None = Field(
        default=None,
        description=("结构化参数（JSON 字符串）。当 discover 显示对方要求 application/json 输入时，"
                     "把它要求的字段组织成 JSON 填在这里，用户的问题会自动放进 question 字段"),
    )
    skill: str | None = Field(default=None, description="指定技能 id（可省略，默认用它的第一个技能）")
    timeout_sec: float | None = Field(default=None, description="超时秒数（可省略）")


@app.post("/call_partner", tags=["搭档"], response_model=CallResult,
          summary="调用平台上某个智能体",
          description=("按 AIC 调用梧桐平台上的一个智能体，把结果取回来。"
                       "AIC 用 discover 工具查得。适用于用户的需求超出你自己能力范围的情况。\n\n"
                       "如果 discover 显示对方「需要结构化 JSON 输入」，"
                       "先把它的必填字段向用户收集齐，组织成 JSON 放进 params，再调用。"))
def call_partner(req: CallPartnerRequest, _: None = Depends(require_token)):
    endpoint, skill, skills = _resolve_endpoint(req.aic)
    if not endpoint:
        return {"ok": False, "state": "not-found",
                "error": f"查不到 AIC 为 {req.aic} 的智能体端点（可能未注册、未在线，或 AIC 写错）"}

    use_skill = req.skill or skill
    timeout = float(req.timeout_sec or DEFAULT_TIMEOUT)
    payload_text, data_type = _build_payload(req.input, req.params, skills)
    try:
        out = aip_call(endpoint, use_skill, payload_text, timeout, data_type)
    except Exception as exc:  # noqa: BLE001
        log.exception("call_partner 失败 aic=%s", req.aic)
        return {"ok": False, "state": "internal-error", "error": repr(exc)[:400]}
    out["target"] = req.aic
    return out


class AskPlatformRequest(BaseModel):
    need: str = Field(..., description="用一句话概括用户要什么，用来在平台上检索合适的智能体")
    input: str = Field(..., description="要转达给那个智能体的完整需求")
    params: str | None = Field(
        default=None,
        description=("结构化参数（JSON 字符串）。对方要求 application/json 输入时填这里，"
                     "用户的问题会自动放进 question 字段"),
    )
    timeout_sec: float | None = Field(default=None, description="超时秒数（可省略）")


class AskResult(BaseModel):
    ok: bool = Field(..., description="是否成功")
    text: str | None = Field(default=None, description="搭档返回的内容（直接展示给用户）")
    chosen_name: str | None = Field(default=None, description="实际用了哪个搭档（名字）")
    chosen_aic: str | None = Field(default=None, description="实际用了哪个搭档（AIC）")
    tried: list[dict] = Field(default_factory=list,
                              description="依次尝试过的搭档（第一个失败会自动试下一个）")
    note: str | None = Field(default=None,
                             description="补充说明（例如：指定的搭档没找到，已退回动态检索）")
    elapsed_sec: float | None = Field(default=None, description="耗时（秒）")
    error: str | None = Field(default=None, description="失败原因")


@app.post("/ask_platform", tags=["搭档"], response_model=AskResult,
          summary="把问题转给平台上最合适的智能体",
          description=("一步完成：先在梧桐平台上检索最合适的智能体，再把需求转给它，"
                       "最后把它的回答取回来。\n\n"
                       "**参数：**\n"
                       "- `need` —— 用一句话概括要什么（用来检索搭档）\n"
                       "- `input` —— 完整的需求内容\n"
                       "- `params` —— 可选。如果 discover 显示该搭档「需要结构化 JSON 输入」，"
                       "就把它的必填字段收集齐，组织成 JSON 填这里；用户的问题会自动放进 question 字段\n"
                       "- ★ `only_aic` —— 可选。**只用这个 AIC**（流水线里锁定用自己注册的搭档，"
                       "避免语义检索挑到别家的同名搭档导致格式不兼容）。"
                       "找不到时逐级兜底：单独按 AIC 再查一次 → 退回动态检索\n"
                       "- `exclude_aic` —— 可选。排除这些 AIC（逗号分隔或数组）。"
                       "用于「解析失败后重试、别再用同一个」\n"
                       "- `allow_fallback` —— 可选，默认 true。`only_aic` 找不到时是否允许退回动态检索\n\n"
                       "**注意：** 平台上的搭档分两类 —— 一类收自然语言（text/plain），"
                       "一类要结构化 JSON（application/json）。discover 的返回里会标明是哪一类。"))
async def ask_platform(request: Request, _: None = Depends(require_token)):
    """
    参数容错设计：既接受 JSON 请求体，也接受 query string；键名大小写不敏感。
    这样调用方（百炼插件）那边"传入方法 / 提交方式"配成哪种都能跑通。
    """
    started = time.time()

    try:
        raw_body = await request.json()
    except Exception:  # noqa: BLE001
        raw_body = {}
    if not isinstance(raw_body, dict):
        raw_body = {}

    body = {str(k).lower(): v for k, v in raw_body.items()}
    qs = {k.lower(): v for k, v in request.query_params.items()}

    def pick(name: str) -> str:
        v = body.get(name)
        if v is None or v == "":
            v = qs.get(name)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        return str(v).strip() if v is not None else ""

    need = pick("need")
    inp = pick("input") or pick("text") or pick("query") or pick("prompt")
    if not need:
        need = inp
    if not inp:
        inp = need
    if not need:
        return {"ok": False,
                "error": "缺少参数：need / input 至少给一个（JSON 请求体或查询串都可以）",
                "elapsed_sec": round(time.time() - started, 2)}

    # ① 检索
    ok, data = _run_acps_cli(["discover", "query", need, "--limit", "5"])
    if not ok:
        return {"ok": False, "error": f"平台检索失败：{str(data)[:300]}"}

    cands = _candidates_from((data.get("result") or {}).get("acsMap") or {})
    for c in cands:
        if c.get("endpoint"):
            _DISCOVERED[c["aic"]] = c

    # ①.5 ★ only_aic / exclude_aic 过滤
    # ------------------------------------------------------------------
    # only_aic 的用途：流水线（需求分析→项目规划→代码生成）里，三跳都
    # 必须用【我们自己的】那个搭档 —— 因为对方的输出格式要和下游对齐，
    # 语义检索挑中的"同名的别人家搭档"格式往往不兼容（而且"成功但格式
    # 不对"不会触发回退，会更难排查）。
    #
    # 过滤链（逐级兜底）：
    #   1) 在检索结果里只留 only_aic
    #   2) 没有 → 单独按 AIC 再查一次（可能只是没被检索捞出来）
    #   3) 还没有 → 退回动态检索（除非 allow_fallback=false）
    # ------------------------------------------------------------------
    only_aic = pick("only_aic")
    exclude_raw = body.get("exclude_aic")
    if exclude_raw is None:
        exclude_raw = qs.get("exclude_aic")
    if isinstance(exclude_raw, str):
        exclude_raw = [x.strip() for x in exclude_raw.split(",") if x.strip()]
    exclude_aic = [str(x) for x in (exclude_raw or [])]
    allow_fallback = str(pick("allow_fallback") or "true").lower() not in ("0", "false", "no")

    note = ""
    if only_aic:
        before = len(cands)
        cands = [c for c in cands if c.get("aic") == only_aic]
        if not cands:
            log.info("only_aic=%s 不在检索结果里，单独按 AIC 查一次", only_aic)
            ep, _sk, sk_all = _resolve_endpoint(only_aic)
            if ep:
                cands = [{"aic": only_aic, "name": only_aic,
                          "endpoint": ep, "skills": sk_all}]
                note = "指定的搭档不在检索结果里，已按 AIC 单独查到"
        if not cands and allow_fallback:
            log.warning("only_aic=%s 找不到，退回动态检索", only_aic)
            cands = _candidates_from((data.get("result") or {}).get("acsMap") or {})
            note = f"指定的搭档（{only_aic}）当前不可用，已退回动态检索"
        log.info("only_aic 过滤：%d → %d 个候选%s", before, len(cands),
                 f"（{note}）" if note else "")

    if exclude_aic:
        before = len(cands)
        cands = [c for c in cands if c.get("aic") not in exclude_aic]
        log.info("exclude_aic 过滤：%d → %d 个候选", before, len(cands))

    if only_aic and not cands:
        return {"ok": False,
                "error": (f"指定的搭档（aic={only_aic}）当前不可用，"
                          f"且 allow_fallback=false 不允许退回动态检索"),
                "tried": [],
                "note": "指定的搭档不可用",
                "elapsed_sec": round(time.time() - started, 2)}

    # ② 选 + ③ 调：按平台给出的相关度顺序依次尝试，
    #    第一个失败就试下一个（语义检索的第一名不一定最合适）
    timeout = ASK_PLATFORM_TIMEOUT
    try:
        t = pick("timeout_sec")
        if t:
            timeout = float(t)
    except (TypeError, ValueError):
        pass

    # params 可能是嵌套对象，不能用 pick（pick 会把它序列化成字符串）
    params = body.get("params")
    if params is None or params == "":
        params = qs.get("params")

    usable = [c for c in cands if c.get("endpoint")]
    if not usable:
        return {"ok": False,
                "error": "平台上没有找到能处理这个需求的智能体",
                "elapsed_sec": round(time.time() - started, 2)}

    chosen = None
    out: dict | None = None
    tried: list[dict] = []
    budget = ASK_PLATFORM_TOTAL_BUDGET
    n_usable = len(usable[:max(1, ASK_PLATFORM_MAX_TRIES)])
    for idx, cand in enumerate(usable[:max(1, ASK_PLATFORM_MAX_TRIES)], 1):
        is_ours = cand.get("aic") == MY_AIC

        # 我们自己的智能体后端挂着百炼旧智能体，天生慢（实测 80~112 秒），
        # 不走总预算、用长超时；平台上的外部搭档通常 1~25 秒，用短超时 + 预算控制。
        if is_ours:
            per_try = DEFAULT_TIMEOUT
            log.info("（这是我们自己的智能体，按长超时 %.0fs 处理，不占外部预算）", per_try)
        else:
            spent = time.time() - started
            if spent > budget:
                log.info("总预算 %.0fs 已用完（已试 %d 个），停止尝试", budget, len(tried))
                break
            per_try = min(timeout, max(10.0, budget - spent))

        skills = cand.get("skills") or []
        skill = (skills[0].get("id") if skills else "") or ""
        payload_text, data_type = _build_payload(inp, params, skills)
        tried.append({"name": cand.get("name"), "aic": cand.get("aic")})
        log.info("ask_platform 尝试第 %d/%d 个：%s（%s）skill=%s type=%s params=%s 单次上限=%.0fs",
                 idx, n_usable, cand.get("name"), cand["aic"], skill or "-", data_type,
                 "有" if params else "无", per_try)
        try:
            r = aip_call(cand["endpoint"], skill, payload_text, per_try, data_type)
        except Exception as exc:  # noqa: BLE001
            log.warning("  调用异常：%s", repr(exc)[:200])
            r = {"ok": False, "state": "internal-error", "error": repr(exc)[:300]}
        if r.get("ok"):
            chosen, out = cand, r
            break
        log.warning("  失败（%s），试下一个", str(r.get("error") or r.get("state"))[:150])
        out = r                              # 记住最后一次失败，用于报错

    if chosen is None:
        return {"ok": False,
                "chosen_name": tried[-1]["name"] if tried else None,
                "chosen_aic": tried[-1]["aic"] if tried else None,
                "error": (f"试了 {len(tried)} 个搭档都失败了；"
                          f"最后一次失败：{str((out or {}).get('error'))[:200]}"),
                "tried": tried,
                "note": note or None,
                "elapsed_sec": round(time.time() - started, 2)}

    return {"ok": True, "text": out.get("text"),
            "chosen_name": chosen.get("name"), "chosen_aic": chosen["aic"],
            "tried": tried,
            "note": note or None,
            "elapsed_sec": round(time.time() - started, 2)}


if __name__ == "__main__":
    import uvicorn

    log.info("启动 acps-gateway  %s:%s", HOST, PORT)
    log.info("  我的 AIC   : %s", MY_AIC)
    log.info("  对外基础地址: %s", PUBLIC_BASE_URL)
    log.info("  客户端证书 : %s", CERT_FILE)
    log.info("  CA 信任包  : %s", CA_FILE)
    log.info("  已配置搭档 : %s", sorted(TARGETS.keys()) or "（空）")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
