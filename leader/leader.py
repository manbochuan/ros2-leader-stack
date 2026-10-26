#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Leader —— ROS2 学习智能体的代码层编排服务

为什么要有这个东西
------------------
原来的「主 agent」是百炼的一个智能体应用，编排全靠提示词。实测下来：

  · 让大模型按提示词走两步（discover → call），它只走一步就停了（把候选列表当答案）
  · 让大模型记住"节点清单""已生成节点"，它会忘
  · 让大模型别保留规划书全文，它照样保留

所以把「编排」从提示词搬到代码里：**决策由 if/else 和状态机做，不是由大模型做。**

分工
----
    百炼主 agent  = 「嘴」  人设、语气、展示结果、概念问答
    本服务        = 「脑」  意图分类、会话状态、调度 Partner ①②③、汇总
    百炼工作流    = 「手」  真正生成需求规格 / 规划书 / 代码

对外接口
--------
    POST /leader        一次对话（带 session_id）
    POST /leader/reset  清掉某个会话
    GET  /health        健康检查
    GET  /session/{id}  看某个会话的状态（调试用）

鉴权
----
    X-Auth-Token: <AUTH_TOKEN>

Partner 传参形态（重要）
------------------------
    同学的三个 Partner 是百炼「工作流应用」，开始节点有自定义变量：
        ① 需求分析   leader_context / history
        ② 项目规划   requirement_json
        ③ 代码生成   node_spec_json

    这些变量怎么通过 API 传进去，我们还没最终确认（详见 README 的"已知未决"）。
    所以这里做成可配置的几种形态，实测确认后改 .env 一行即可：

        BIZ_MODE=input.biz_params      biz_params 放在 input 里（官方文档写法）
        BIZ_MODE=top.biz_params        biz_params 放在顶层
        BIZ_MODE=input.flat            变量直接放 input 里，和 prompt 平级
        BIZ_MODE=input.biz_params.udp  再包一层 user_defined_params
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ===========================================================================
# 配置
# ===========================================================================
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(Path(os.environ.get("ENV_FILE", BASE_DIR / ".env")))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


HOST = env("LEADER_HOST", "0.0.0.0")
PORT = int(env("LEADER_PORT", "9200"))
AUTH_TOKEN = env("AUTH_TOKEN", "")

DB_PATH = Path(env("LEADER_DB", str(BASE_DIR / "leader.db")))

DASHSCOPE_API_KEY = env("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE = env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com").rstrip("/")

# ---- 三个 Partner 的 AIC（走平台，定死用我们自己的）----
# 为什么用 AIC 而不是百炼 appId：
#   这三个 Partner 是注册到梧桐平台上的智能体，Leader 经「网关 → 平台」调它们。
#   调用时必须带上 only_aic 把目标锁死 —— 因为语义检索可能挑到别人家同名的
#   "需求分析/项目规划"搭档，那些搭档的输出格式和我们的下游对不上
#   （而且"成功但格式不对"不会触发网关的回退，很难排查）。
#
# 这三个值在「把三个 Partner 注册到平台」之后填（改 .env 即可，代码不用动）。
REQUIREMENT_AIC = env("REQUIREMENT_AIC", "")   # ① 需求分析
PLAN_AIC        = env("PLAN_AIC", "")          # ② 项目规划
CODEGEN_AIC     = env("CODEGEN_AIC", "")       # ③ 代码生成

# 需求分析 / 项目规划 / 代码生成 在平台检索时用的关键词
NEED_REQUIREMENT = env("NEED_REQUIREMENT", "ROS2 需求分析")
NEED_PLAN        = env("NEED_PLAN", "ROS2 项目规划")
NEED_CODEGEN     = env("NEED_CODEGEN", "ROS2 单节点代码生成")

# 百炼直连参数（★ 现在只作为"没配 AIC 时的降级方案"保留）
# 为什么还留着：三个 Partner 还没注册完之前，可以先用直连跑通流程；
# 配了 AIC 之后自动走平台（见 call_partner_via_platform 里的分支）。
PARTNER1_APP = env("PARTNER1_APP", "")   # ① 需求分析（百炼 appId）
PARTNER2_APP = env("PARTNER2_APP", "")   # ② 项目规划
PARTNER3_APP = env("PARTNER3_APP", "")   # ③ 代码生成

# 传参形态（实测确认后改这里）
BIZ_MODE = env("BIZ_MODE", "input.biz_params")
# 百炼的 flow_stream_mode：官方合法值是 message_format / full_thoughts / agent_format
FLOW_STREAM_MODE = env("FLOW_STREAM_MODE", "")
PARTNER_TIMEOUT = float(env("PARTNER_TIMEOUT_SEC", "300"))
PARTNER_RETRY = int(env("PARTNER_RETRY", "1"))

# 代码生成并发上限（同学文档 CC-05：上限 4）
MAX_CONCURRENCY = int(env("MAX_CONCURRENCY", "4"))

# 分批策略（同学文档 CC-02：节点数 >= 3 时先出 2 个）
BATCH_THRESHOLD = int(env("BATCH_THRESHOLD", "3"))
BATCH_FIRST = int(env("BATCH_FIRST", "2"))

# 会话记忆窗口（同学文档 L-02：15 轮）
HISTORY_TURNS = int(env("HISTORY_TURNS", "15"))

# 知识库检索（报错排查用）
KB_URL = env("KB_URL", "")          # 例如 http://127.0.0.1:8000/search
KB_TOP_K = int(env("KB_TOP_K", "5"))
KB_TIMEOUT = float(env("KB_TIMEOUT_SEC", "20"))

# 出站网关（调梧桐平台上的搭档 —— 也就是"叮当的搭档"）
# Leader 判断"不是 ROS2 领域"的需求时，转给网关的 /ask_platform
GATEWAY_URL = env("GATEWAY_URL", "http://127.0.0.1:9100").rstrip("/")
GATEWAY_TOKEN = env("GATEWAY_TOKEN", "")
GATEWAY_TIMEOUT = float(env("GATEWAY_TIMEOUT_SEC", "180"))

# 兜底回答用的小模型（意图分类没命中 / 概念问答 / 报错排查）
ANSWER_MODEL = env("ANSWER_MODEL", "qwen-flash")
ANSWER_MAX_TOKENS = int(env("ANSWER_MAX_TOKENS", "1500"))
ROUTER_MODEL = env("ROUTER_MODEL", "qwen-flash")

LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("leader")


# ===========================================================================
# 会话状态（SQLite）
#   同学文档 L-02 两条纪律：
#     1) 长输出（规划书/代码汇总）在代码层拼接，不进 LLM 记忆  → 我们存在这里
#     2) 重状态（节点清单/项目元信息）每轮随参数重新注入        → 我们每次读出来注入
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    mode         TEXT,              -- single | project | guidance | ''
    nodes        TEXT,              -- 节点清单 JSON：[{node_name, file_name, what_to_do, ...}]
    generated    TEXT,              -- 已生成节点名 JSON 数组
    plan         TEXT,              -- 完整 plan（不进 LLM，只在代码里用）
    guide        TEXT,              -- 学习指南（同上）
    plan_md      TEXT,              -- 渲染好的规划书（同上）
    level        TEXT,              -- beginner | advanced
    ask_history  TEXT,              -- need_ask 追问累积
    turns        TEXT,              -- 对话历史 JSON：[{role, content}]，滚动保留 N 轮
    updated_at   REAL
);
"""


def db() -> sqlite3.Connection:
    """
    取一个数据库连接。

    ★ 并发设置（多人同时问也不会因为锁库失败）：
      · WAL  ：多读一写并行，读不会被写挡住（默认的 rollback journal 会互斥）
      · busy_timeout：写冲突时最多等 15 秒，而不是立刻抛 database is locked
      · synchronous=NORMAL：WAL 下足够安全，写入更快
    ★ 注意：WAL 是【数据库文件级】的持久设置，第一次设上以后一直有效；
      开启后会多出 leader.db-wal / leader.db-shm 两个文件，属正常现象。
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as exc:  # noqa: BLE001
        log.warning("设置 SQLite 并发参数失败（不影响功能）：%s", exc)
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def load_session(session_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
    if not row:
        return {
            "session_id": session_id, "mode": "", "nodes": [], "generated": [],
            "plan": None, "guide": "", "plan_md": "", "level": "",
            "ask_history": "", "turns": [],
        }
    d = dict(row)
    for key in ("nodes", "generated", "turns"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except Exception:  # noqa: BLE001
            d[key] = []
    for key in ("plan",):
        raw = d.get(key)
        try:
            d[key] = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            d[key] = None
    return d


def save_session(s: dict) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO sessions
               (session_id, mode, nodes, generated, plan, guide, plan_md,
                level, ask_history, turns, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 mode=excluded.mode, nodes=excluded.nodes, generated=excluded.generated,
                 plan=excluded.plan, guide=excluded.guide, plan_md=excluded.plan_md,
                 level=excluded.level, ask_history=excluded.ask_history,
                 turns=excluded.turns, updated_at=excluded.updated_at""",
            (s["session_id"], s.get("mode", ""),
             json.dumps(s.get("nodes") or [], ensure_ascii=False),
             json.dumps(s.get("generated") or [], ensure_ascii=False),
             json.dumps(s.get("plan"), ensure_ascii=False) if s.get("plan") else None,
             s.get("guide") or "", s.get("plan_md") or "",
             s.get("level") or "", s.get("ask_history") or "",
             json.dumps((s.get("turns") or [])[-HISTORY_TURNS * 2:], ensure_ascii=False),
             time.time()),
        )


def push_turn(s: dict, role: str, content: str) -> None:
    """记一轮对话（长输出不进来 —— 纪律 1 由调用方保证）"""
    turns = s.get("turns") or []
    turns.append({"role": role, "content": content[:2000]})   # 单条封顶，防爆
    s["turns"] = turns[-HISTORY_TURNS * 2:]


# ===========================================================================
# 意图分类（规则优先）
#   同学文档 L-05/L-06：4 类意图
# ===========================================================================
ERROR_HINTS = ["报错", "错误", "error", "traceback", "异常", "跑不起来", "跑不了",
               "失败", "不工作", "崩溃", "nan", "inf", "无法", "报了个", "提示"]
CONCEPT_HINTS = ["什么是", "是什么", "怎么理解", "有什么区别", "区别是", "为什么",
                 "原理", "解释一下", "介绍一下", "能不能讲", "讲讲", "含义"]
PROJECT_HINTS = ["项目", "系统", "小车", "机器人", "搭建", "设计一个", "设计个",
                 "做一个", "做个", "帮我做", "开发一个", "实现一个", "整套",
                 "多个节点", "多节点", "方案"]
SINGLE_HINTS = ["写一个", "写个", "生成一个", "生成个", "节点", "发布者", "订阅者",
                "定时器", "服务端", "客户端", "参数", "launch"]
CHAT_HINTS = ["你好", "您好", "谢谢", "在吗", "哈哈", "好的", "嗯", "收到"]

# ---- 判断"原文里有没有明显不属于 ROS2 的内容" ----
#
# 用途：混合请求（"帮我写 ROS2 代码，再写个 Java 贪吃蛇"）里，
# 接入层的 [analysis] 只会把 ROS2 那部分传给工作流，非 ROS2 部分被丢掉。
# 学生只拿到一半答案会困惑，所以在返回时补一句说明。
#
# ★ 注意：这不是"判断是不是 ROS2"（那是门卫的职责），
#   只是"原文里出现这些词 → 大概有东西被丢掉了 → 打个招呼"。
#   漏判不影响功能（只是少一句提示）；误判也只是多一句话。
#
# ★ 刻意不列 Python / C++（ROS2 开发里太常见），只列"明显不搭"的。
NON_ROS2_HINTS = [
    # 其它编程语言 / 技术栈
    "java", "javascript", "typescript", "kotlin", "swift", "golang",
    "go语言", "rust", "php", "ruby", "c#", "csharp", ".net",
    "前端", "后端", "数据库", "mysql", "redis", "爬虫", "leetcode",
    "算法题", "安卓", "android", "ios", "小程序", "网页", "网站",
    # 非技术领域
    "旅游", "景点", "攻略", "美食", "餐厅", "酒店", "机票",
    "养生", "减肥", "减脂", "食谱", "健身", "体检", "诊断", "看病",
    "股票", "基金", "理财", "保险", "法律", "合同", "诉讼",
    "小说", "剧本", "诗歌", "作文", "论文", "简历", "面试",
    "报修", "校园卡", "选课",
]

APOLOGY = (
    "\n\n---\n\n"
    "⚠️ 你提到的其他部分不在我能处理的范围内（我只做 ROS2 相关的事），"
    "所以上面只回答了 ROS2 那部分，抱歉～\n"
    "其他领域的问题，可以去平台上找对应的智能体。"
)


def _append_note(reply: str, note: str) -> str:
    """追加补充说明前，把【两边】重复的分隔线都收掉（否则会出现 --- / --- 连着两条）"""
    base = re.sub(r"(\s*-{3,}\s*)+$", "", (reply or "").rstrip()).rstrip()
    note = re.sub(r"^(\s*-{3,}\s*)+", "", (note or "").lstrip()).lstrip()
    if not base:
        return note
    return f"{base}\n\n---\n\n{note}"


def _has_offtopic(text: str) -> bool:
    """原文里是否出现了明显不属于 ROS2 的内容（用来决定要不要补一句说明）"""
    t = (text or "").lower()
    return any(h in t for h in NON_ROS2_HINTS)


# ===========================================================================
# 剥离上游（叮当 / 平台）夹带的包装文字
# ===========================================================================
# 叮当把请求转过来时，会把"上一轮全文 + 协作元信息"一起塞进正文，形如：
#     调用智能体「ROS2 学习编排智能体」帮我写一个发布hello的ros节点
#     上一轮候选结果：本次由 **ROS2 学习编排智能体** 参与回答。……（几千字全文）
#     叮当请求补充：请补充偏好。 用户回答：A
# ★ 这堆字会污染规则分类（里面含"项目""规划书""节点"等词），实测把一句
#   "写一个 hello 节点"误判成"项目需求"，于是又跑一遍完整流水线 → 刷屏。
#   所以进来第一件事就是剥掉它，只留用户真正说的那句话。
_UPSTREAM_CALL_RE = re.compile(r"调用智能体\s*[「『][^」』]{0,80}[」』]\s*")
_UPSTREAM_CUT_MARKERS = ("上一轮候选结果", "叮当请求补充", "叮当规划",
                         "协作与数据流转", "协作驾驶舱")


def strip_upstream_wrapper(text: str) -> str:
    """把叮当/平台夹带的编排元信息剥掉，只留用户真正说的话。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    t = _UPSTREAM_CALL_RE.sub("", raw).strip()
    if t.startswith("调用智能体"):
        t = t[len("调用智能体"):].strip()
    for marker in _UPSTREAM_CUT_MARKERS:
        i = t.find(marker)
        if i > 0:
            t = t[:i].strip()
    return t or raw


# 把混合请求切成小段（按标点和连接词）
_MIX_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]+|再|同时|另外|还有|以及|顺便|然后")


def split_mixed_request(text: str) -> tuple[str, str]:
    """
    把混合请求拆成（ROS2 部分, 非 ROS2 部分）。

    判断依据只有一条：这一小段里有没有 NON_ROS2_HINTS 里的词。
    例："帮我写一个发布hello的ros节点，再帮我写一个java贪吃蛇"
        → ("帮我写一个发布hello的ros节点", "帮我写一个java贪吃蛇")
    """
    parts = [p.strip() for p in _MIX_SPLIT_RE.split(text or "") if p.strip()]
    ros: list[str] = []
    other: list[str] = []
    for p in parts:
        (other if _has_offtopic(p) else ros).append(p)
    return "，".join(ros), "，".join(other)

# 注意：这里刻意**不做**「是不是 ROS2」的判断。
# 那是 Partner ① 的 Decision 阶段（门卫）的职责，Leader 不该重复实现。
# 规则四类都没命中时，直接发给 ① 让它判断。


def rule_classify(text: str) -> str | None:
    """规则分类。命中返回意图名，没命中返回 None。"""
    t = (text or "").lower().strip()
    if not t:
        return None
    for name, hints in (("报错排查", ERROR_HINTS),
                        ("概念讲解", CONCEPT_HINTS),
                        ("项目需求", PROJECT_HINTS),
                        ("单节点", SINGLE_HINTS)):
        for h in hints:
            if h.lower() in t:
                log.info("规则命中：'%s' → %s", h, name)
                return name
    if any(h in text for h in CHAT_HINTS):
        return "闲聊"
    return None


# 学生回复 A/B/C/D 的识别
CHOICE_RE = re.compile(r"^\s*([ABCD])\b|^\s*([ABCD])[\.、,，:：]", re.I)
NODE_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*_node)")


def parse_choice(text: str) -> str | None:
    m = CHOICE_RE.match(text or "")
    if m:
        return (m.group(1) or m.group(2)).upper()
    return None


# ===========================================================================
# 调大模型 / Partner
# ===========================================================================
def _llm_chat(messages: list[dict], model: str, max_tokens: int,
              timeout: float = 60) -> str:
    """调百炼的 OpenAI 兼容接口（模型在云端，本服务只发 HTTP）"""
    url = f"{DASHSCOPE_BASE}/compatible-mode/v1/chat/completions"
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _build_partner_payload(prompt: str, biz: dict[str, Any]) -> dict:
    """按 BIZ_MODE 组装百炼请求体（形态未最终确认，所以做成可切换）"""
    if not prompt.strip():
        # 百炼的 input.prompt 是必填的
        prompt = "（本请求为结构化输入，参数见自定义变量）"

    payload: dict[str, Any] = {
        "input": {"prompt": prompt},
        "parameters": {},
        "debug": {},
    }
    if FLOW_STREAM_MODE:
        payload["parameters"]["flow_stream_mode"] = FLOW_STREAM_MODE

    if not biz:
        return payload

    if BIZ_MODE == "top.biz_params":
        payload["biz_params"] = biz
    elif BIZ_MODE == "input.flat":
        payload["input"].update(biz)
    elif BIZ_MODE == "input.biz_params.udp":
        # 智能体应用传插件参数那种写法，保底一试
        payload["input"]["biz_params"] = {"user_defined_params": biz}
    else:  # input.biz_params（默认，官方文档写法）
        payload["input"]["biz_params"] = biz
    return payload


def call_partner(app_id: str, prompt: str = "", biz: dict[str, Any] | None = None,
                 timeout: float | None = None) -> dict:
    """
    调一个百炼工作流应用，返回解析后的信封。
    返回： {"ok": bool, "raw": str, "error": str|None, "elapsed": float}
    """
    if not app_id:
        return {"ok": False, "raw": "", "error": "该 Partner 的 appId 未配置", "elapsed": 0}
    if not DASHSCOPE_API_KEY:
        return {"ok": False, "raw": "", "error": "DASHSCOPE_API_KEY 未配置", "elapsed": 0}

    url = f"{DASHSCOPE_BASE}/api/v1/apps/{app_id}/completion"
    payload = _build_partner_payload(prompt, biz or {})
    t0 = time.time()
    last_err = ""

    for attempt in range(PARTNER_RETRY + 1):
        try:
            with httpx.Client(timeout=timeout or PARTNER_TIMEOUT) as client:
                resp = client.post(url, json=payload, headers={
                    "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                })
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                log.warning("Partner %s 返回 %s（第 %d 次）", app_id[:8], resp.status_code, attempt + 1)
                continue
            data = resp.json()
            if data.get("code"):
                last_err = f"{data.get('code')}: {data.get('message')}"
                log.warning("Partner %s 业务错误 %s", app_id[:8], last_err)
                break                       # 参数错误重试没意义
            text = ((data.get("output") or {}).get("text") or "").strip()
            return {"ok": bool(text), "raw": text,
                    "error": None if text else "Partner 返回空文本",
                    "elapsed": round(time.time() - t0, 2)}
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)[:300]
            log.warning("Partner %s 调用异常（第 %d 次）：%s", app_id[:8], attempt + 1, last_err)

    return {"ok": False, "raw": "", "error": last_err, "elapsed": round(time.time() - t0, 2)}


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _balanced_json_at(text: str, start: int) -> str | None:
    """
    从 text[start] 之后找第一个【括号配平】的 {...}（跳过字符串里的括号与转义）。

    为什么需要它：
      · 专家有时会漏写 END 标记 —— 这时不能靠 END 定位，只能靠配平把 JSON 抠出来；
      · 专家有时会在标记段里、JSON 之后又写几行说明文字 —— 尾部多出的字会让
        json.loads 报 "Extra data: char N"，配平截取正好把尾巴切掉。
    """
    if not text or start < 0 or start >= len(text):
        return None
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        ch = text[j]
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
                return text[i:j + 1]
    return None


def _loads_tolerant(seg: str) -> Any:
    """
    宽容地解析一段 JSON（三道保险）：
      1) 直接 json.loads
      2) 去掉 ```json 围栏再试
      3) 只取第一个配平的 {...} 再试（丢弃尾部杂字 —— 就是 "Extra data" 的成因）
    """
    seg = (seg or "").strip()
    if not seg:
        raise ValueError("空内容")
    for cand in (seg, _strip_fence(seg)):
        try:
            return json.loads(cand)
        except Exception:  # noqa: BLE001
            pass
    cut = _balanced_json_at(seg, 0)
    if cut and cut.strip() != seg:
        log.warning("JSON 尾部有多余内容，已按配平截断（原 %d 字 → %d 字）",
                    len(seg), len(cut))
        return json.loads(cut)
    raise ValueError(f"不是合法 JSON：{seg[:200]!r}")


def _extract_between(text: str, begin: str, end: str) -> str | None:
    """
    取标记段内容。优先按 end 标记切；end 缺失时退回"括号配平"。
    （专家漏写 END 时，原来会直接判"没有标记"而丢掉整份结果。）
    """
    text = text or ""
    i = text.find(begin)
    if i < 0:
        return None
    body_start = i + len(begin)
    j = text.find(end, body_start)
    if j >= 0:
        return text[body_start:j].strip()
    cut = _balanced_json_at(text, body_start)
    if cut:
        log.warning("标记段缺少 %s，已按括号配平截取（%d 字）", end, len(cut))
        return cut
    return None


def parse_partner1(raw: str) -> dict:
    """① 需求分析：输出一个 JSON（valid / need_ask / ask_question / data）"""
    try:
        out = _loads_tolerant(raw)
        if not isinstance(out, dict):
            return {"valid": False, "error": "① 输出不是 JSON 对象"}
        return out
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": f"① 输出不是合法 JSON：{exc}"}


def parse_partner2(raw: str) -> dict:
    """② 项目规划：学习指南 + <!--NODE_LIST--> + <!--PLAN_JSON--> 三段"""
    raw = raw or ""
    node_raw = _extract_between(raw, "<!--NODE_LIST_START-->", "<!--NODE_LIST_END-->")
    plan_raw = _extract_between(raw, "<!--PLAN_JSON_START-->", "<!--PLAN_JSON_END-->")
    guide = raw.split("<!--NODE_LIST_START-->")[0].strip()

    out: dict[str, Any] = {"guide": guide, "nodes": None, "plan": None, "error": None}

    if node_raw:
        try:
            env = _loads_tolerant(node_raw)
            nodes = (env or {}).get("nodes") if isinstance(env, dict) else None
            if isinstance(nodes, list):
                out["nodes"] = nodes
            else:
                out["error"] = "② NODE_LIST 段里没有 nodes 数组"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"② NODE_LIST 段解析失败：{exc}"
            log.warning("② NODE_LIST 解析失败：%s", exc)

    if plan_raw:
        try:
            env2 = _loads_tolerant(plan_raw)
            plan = (env2 or {}).get("plan") if isinstance(env2, dict) else None
            if isinstance(plan, dict):
                out["plan"] = plan
        except Exception as exc:  # noqa: BLE001
            log.warning("② PLAN_JSON 解析失败：%s", exc)
            out["error"] = out["error"] or f"② PLAN_JSON 段解析失败：{exc}"

    if out["nodes"] is None:
        out["error"] = out["error"] or "② 输出里没有 NODE_LIST 标记（工作流可能走了模板回退）"
    return out


def parse_partner3(raw: str) -> dict:
    """③ 代码生成：<!--ENV_START--> ... <!--ENV_END--> 单引用信封"""
    env_raw = _extract_between(raw, "<!--ENV_START-->", "<!--ENV_END-->")
    if not env_raw:
        return {"valid": False, "error": "③ 输出里没有 ENV 标记", "raw": (raw or "")[:300]}
    try:
        env = _loads_tolerant(env_raw)
        if not isinstance(env, dict):
            return {"valid": False, "error": "③ 信封不是 JSON 对象", "raw": (raw or "")[:300]}
        return env
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": f"③ 信封解析失败：{exc}", "raw": (raw or "")[:300]}


# 专家原始输出的落盘目录（解析失败时一定要留证据，否则没法复盘）
RAW_DUMP_DIR = env("RAW_DUMP_DIR", "/opt/acps-agent/leader/raw")


def _dump_raw(tag: str, raw: str, err: str | None = None) -> str | None:
    """把专家的原始输出写到文件里，文件头带上错误原因和长度。"""
    try:
        os.makedirs(RAW_DUMP_DIR, exist_ok=True)
        name = f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.txt"
        path = os.path.join(RAW_DUMP_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# tag={tag}\n# err={err}\n# len={len(raw or '')}\n")
            f.write("# " + "-" * 60 + "\n")
            f.write(raw or "")
        log.warning("专家原始输出已落盘：%s（err=%s）", path, err)
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("原始输出落盘失败：%s", exc)
        return None


def _valid_node(node: Any) -> bool:
    """
    ③ 的输入必须先过这一关：必须是一个带 node_name 的对象。
    （② 偶尔会返回字符串数组/空对象，直接喂给 ③ 会产出垃圾代码 —— 宁可当场熔断。）
    """
    return isinstance(node, dict) and bool(str(node.get("node_name") or "").strip())


# ===========================================================================
# 知识库检索（报错排查）
# ===========================================================================
def search_kb(question: str) -> dict:
    if not KB_URL:
        return {"context": "", "source_count": 0}
    try:
        with httpx.Client(timeout=KB_TIMEOUT) as client:
            resp = client.post(KB_URL, json={"question": question, "top_k": KB_TOP_K})
            resp.raise_for_status()
            d = resp.json()
        return {"context": d.get("context") or "",
                "source_count": int(d.get("source_count") or 0)}
    except Exception as exc:  # noqa: BLE001
        log.warning("知识库检索失败：%s", repr(exc)[:200])
        return {"context": "", "source_count": 0, "error": repr(exc)[:200]}


def _gateway_ask(need: str, input_text: str, only_aic: str = "",
                 exclude_aic: list[str] | None = None,
                 timeout: float | None = None) -> dict:
    """
    直接调网关的 /ask_platform，返回【网关的原始响应】。

    网关那侧会做：平台检索 → only_aic 过滤（含三级兜底）→ 按对方契约组装载荷
    → mTLS+AIP 调用 → 失败回退。Leader 不重复实现这些。

    返回网关原样： {"ok", "text", "chosen_name", "chosen_aic", "tried", "note", "error", ...}
    """
    if not GATEWAY_URL:
        return {"ok": False, "error": "网关未配置（GATEWAY_URL 为空）"}

    payload: dict[str, Any] = {"need": need, "input": input_text}
    if only_aic:
        payload["only_aic"] = only_aic
    if exclude_aic:
        payload["exclude_aic"] = exclude_aic

    try:
        with httpx.Client(timeout=timeout or GATEWAY_TIMEOUT) as client:
            resp = client.post(
                f"{GATEWAY_URL}/ask_platform",
                json=payload,
                headers={"X-Auth-Token": GATEWAY_TOKEN,
                         "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            d = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("调网关失败：%s", repr(exc)[:200])
        return {"ok": False, "error": f"网关调用失败：{repr(exc)[:200]}"}

    if not isinstance(d, dict):
        return {"ok": False, "error": f"网关返回了非预期格式：{str(d)[:200]}"}
    return d


def ask_platform(text: str) -> dict:
    """
    把【非 ROS2 领域】的需求转给平台上最合适的搭档。

    这就是"转平台"的动作：用学生的原话重新检索，让平台去找别的领域的智能体。
    （注意：不是"丢回给叮当" —— 叮当没有 AIP 端点，它是用户界面；
      真正的调度者是 Leader 自己。）
    """
    d = _gateway_ask(need=text, input_text=text)

    steps = [{"step": f"平台搭档「{d.get('chosen_name') or '未知'}」",
              "ok": bool(d.get("ok")), "elapsed": d.get("elapsed_sec"),
              "error": d.get("error")}]
    if not d.get("ok"):
        return {"reply": f"平台上没有找到能处理这个需求的搭档：{d.get('error') or '未知原因'}",
                "steps": steps, "state": "failed"}

    name = d.get("chosen_name") or "平台上的智能体"
    return {"reply": f"我帮你问了一下平台上的「{name}」～\n\n{d.get('text') or ''}",
            "steps": steps, "state": "ok"}


def call_partner_via_platform(need: str, input_text: str, only_aic: str,
                              exclude_aic: list[str] | None = None) -> dict:
    """
    经「网关 → 平台」调一个 Partner，并把目标 AIC 锁死。
    返回结构和直连版 call_partner 保持一致，便于调用点无感切换。
    """
    if not only_aic:
        return {"ok": False, "raw": "", "error": "该 Partner 的 AIC 未配置",
                "elapsed": 0, "note": None}

    t0 = time.time()
    d = _gateway_ask(need=need, input_text=input_text, only_aic=only_aic,
                     exclude_aic=exclude_aic)
    if not d.get("ok"):
        return {"ok": False, "raw": "",
                "elapsed": d.get("elapsed_sec") or round(time.time() - t0, 2),
                "error": d.get("error") or "平台调用失败",
                "note": d.get("note")}
    return {"ok": True, "raw": d.get("text") or "",
            "elapsed": d.get("elapsed_sec") or round(time.time() - t0, 2),
            "error": None, "note": d.get("note")}


def invoke_partner(*, need: str, aic: str, app_id: str,
                   prompt: str = "", biz: dict[str, Any] | None = None,
                   exclude_aic: list[str] | None = None) -> dict:
    """
    调一个 Partner 的【统一入口】：

      · 配了 AIC  → 走平台（ask_platform + only_aic，把目标锁死）
      · 没配 AIC  → 退回直连百炼（用 app_id）

    走平台时传给对方的内容：
      · ① 需求分析 → prompt（学生原话）+ 结构化参数附录
      · ② 项目规划 → biz["requirement_json"]
      · ③ 代码生成 → biz["node_spec_json"]

    exclude_aic：排除某些 AIC（用于"输出格式不对 → 换一个重试"）。
    """
    biz = biz or {}

    if aic:
        # 结构化参数优先（②③ 就是这种）
        for key in ("requirement_json", "node_spec_json"):
            if key in biz:
                return call_partner_via_platform(need, str(biz[key]), aic, exclude_aic)

        # 没有结构化参数（① 那种）→ 用 prompt，把 biz 作为附录带上
        text = prompt
        extra = {k: v for k, v in biz.items() if v}
        if extra:
            text = (prompt + "\n\n【结构化参数】\n"
                    + json.dumps(extra, ensure_ascii=False))
        return call_partner_via_platform(need, text, aic, exclude_aic)

    # 没配 AIC → 直连百炼（过渡期用）
    return call_partner(app_id, prompt, biz)


# ===========================================================================
# 规划书渲染（Leader 用自己的模板，而不是把 ② 的原文转给用户）
# ===========================================================================
def render_plan_book(plan: dict | None) -> str:
    if not isinstance(plan, dict):
        return ""
    p = plan.get("project") or {}
    lines = ["## 📋 项目规划书", "", "### 项目信息", ""]
    lines.append(f"- 项目名：**{p.get('name_cn') or '—'}**（`{p.get('name_en') or '—'}`）")
    lines.append(f"- 功能包：`{p.get('package_name') or '—'}`")
    lines.append(f"- 难度：{p.get('difficulty') or '—'}")
    if p.get("description"):
        lines.append(f"- 简介：{p['description']}")

    nodes = plan.get("nodes") or []
    if nodes:
        lines += ["", "### 节点清单", ""]
        for i, n in enumerate(nodes, 1):
            lines.append(f"**{i}. `{n.get('node_name') or '—'}`** — {n.get('function') or n.get('what_to_do') or ''}")
            for t in (n.get("publish_topics") or []):
                lines.append(f"  - 发布 `{t.get('topic')}`（{t.get('message_type')}）")
            for t in (n.get("subscribe_topics") or []):
                lines.append(f"  - 订阅 `{t.get('topic')}`（{t.get('message_type')}）")
            if n.get("core_logic"):
                lines.append(f"  - 核心逻辑：{n['core_logic']}")
            lines.append("")

    topics = plan.get("topic_overview") or []
    if topics:
        lines += ["### 话题总览", "",
                  "| 话题 | 消息类型 | 发布者 | 订阅者 | 作用 |",
                  "|---|---|---|---|---|"]
        for t in topics:
            lines.append(f"| `{t.get('topic')}` | `{t.get('message_type')}` | "
                         f"{t.get('publisher') or '—'} | {t.get('subscriber') or '—'} | "
                         f"{t.get('purpose') or ''} |")
        lines.append("")

    deps = plan.get("dependencies") or []
    if deps:
        lines += ["### 依赖包", "", "、".join(f"`{d}`" for d in deps), ""]

    files = plan.get("file_structure") or []
    if files:
        lines += ["### 文件清单", "", "```", *files, "```", ""]

    return "\n".join(lines).strip()


def render_codes(envs: list[dict]) -> str:
    parts = []
    for env in envs:
        name = env.get("node_name") or "node"
        parts.append(f"## 节点：`{name}`")
        parts.append("")
        if env.get("valid") and env.get("code"):
            fname = env.get("file_name") or f"{name}.py"
            parts.append("```python")
            parts.append(f"# file: {fname}")
            parts.append(env["code"].strip())
            parts.append("```")
        else:
            parts.append(f"> ⚠️ 该节点生成失败：{env.get('error') or env.get('summary') or '未知原因'}")
        if env.get("teaching"):
            parts.append("")
            parts.append(env["teaching"].strip())
        parts.append("")
        parts.append("---")
        parts.append("")
    return "\n".join(parts).strip()


def render_options(confirmed: bool) -> str:
    if confirmed:
        return ("**接下来：**\n\n"
                "C. 帮我生成某个节点的代码（请告诉我节点名）\n"
                "D. 帮我生成全部节点代码")
    return ("**接下来：**\n\n"
            "A. 规划可以，就按这个来\n"
            "B. 我要修改规划（请告诉我怎么改）\n"
            "C. 帮我生成某个节点的代码（请告诉我节点名）\n"
            "D. 帮我生成全部节点代码")


# ===========================================================================
# 编排主流程
# ===========================================================================
def run_single_node(user_text: str, s: dict) -> dict:
    """
    单节点需求（"写一个发布 hello 的节点"）—— 轻量路径。

    ★ 为什么不走 ①②：
      单节点不需要"需求规格化"和"项目规划"，直接让 ③ 生成一个节点就够了。
      （原来这里和"项目需求"共用 run_pipeline，结果一句"写一个 hello 节点"
        被当成完整项目：规划书 + 两个节点 + 学习指南 + A/B/C/D 菜单，
        几千字刷屏 —— 而用户只要一段代码。）

    ③ 支持 text/plain 自然语言输入（skill: ros2-codegen.single-node），
    所以这里直接把学生原话发过去。
    """
    steps: list[dict] = []
    r3 = invoke_partner(
        need=NEED_CODEGEN,
        aic=CODEGEN_AIC,
        app_id=PARTNER3_APP,
        prompt=user_text,
    )
    steps.append({"step": "③ 单节点代码生成", "ok": r3["ok"],
                  "elapsed": r3["elapsed"], "error": r3["error"]})
    if not r3["ok"]:
        return {"reply": f"代码生成没跑通：{r3['error']}", "steps": steps,
                "state": "failed"}

    env = parse_partner3(r3["raw"])
    env.setdefault("node_name", "node")
    s["mode"] = "single"
    s["nodes"] = [{"node_name": env.get("node_name")}]
    s["generated"] = [env.get("node_name")] if env.get("valid") else []

    if not env.get("valid") and not env.get("code"):
        return {"reply": (f"代码生成结果异常：{env.get('error') or '没拿到代码'}\n\n"
                          f"（原文前 500 字）\n{r3['raw'][:500]}"),
                "steps": steps, "state": "failed"}

    return {"reply": render_codes([env]), "steps": steps, "state": "ok"}


def run_pipeline(user_text: str, s: dict) -> dict:
    """
    规则命中「项目需求」时走这里（完整的 ①→②→③ 流水线）。

    实现上和「规则没命中」走的是同一条路（都要先问 ①），
    区别只是给 ① 一个方向提示。原来这里是独立实现，
    结果漏了 reject 兜底 —— 现在转发给 gate_and_route，避免两份逻辑不一致。
    """
    return gate_and_route(user_text, s, hint="ROS2 学习项目")


def _has_concrete_question(requirement: dict) -> bool:
    """① 的 Analysis 阶段会填 has_concrete_question；没这个字段时按「有」处理"""
    if not isinstance(requirement, dict):
        return False
    v = requirement.get("has_concrete_question")
    if v is None:
        return True                      # 工作流没这个字段 → 宽容处理
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _is_rejected(env1: dict, req: dict) -> bool:
    """① 是不是明确拒绝了（兼容几种可能的字段名）"""
    for key in ("decision", "accepted"):
        v = env1.get(key, req.get(key))
        if v is False:
            return True
        if isinstance(v, str) and v.strip().lower() in ("reject", "rejected", "false", "no"):
            return True
    return False


def gate_and_route(user_text: str, s: dict, hint: str = "") -> dict:
    """
    调 Partner ① 做需求分析，再按它的意见决定下一步。

    ★ 注意：「是不是 ROS2」这件事的判断在【接入层的门卫】里，不在 Leader 里。
      接入层的 [decision] 阶段（门卫）负责判领域，[analysis] 阶段负责
      把非 ROS2 的内容过滤掉。Leader 只按 ① 的返回决定下一步。

    「规则命中项目需求/单节点」和「规则没命中」**都走这里** ——
    因为无论哪种情况，最终都得问 ① 才知道该不该走流水线。
    （早期这两条路是各写一遍的，结果一条漏了 reject 兜底，出了 bug。）

        ① 拒了 / 输出不合法 / 调用挂了  → 转给平台上的搭档
        ① 认了 + 有具体问题            → 继续 ②③
        ① 认了 + 没具体问题(guidance)   → 反问澄清
        ① 要追问(need_ask)            → 把追问转给学生

    hint：Leader 规则命中时给的一点方向提示（会作为 leader_context 传给 ①）；
          规则没命中时不传，让 ① 自己判断领域。
    """
    steps: list[dict] = []
    if hint:
        leader_ctx = f"方向：{hint}；水平：{s.get('level') or 'beginner'}；约束：无"
    else:
        leader_ctx = ("方向：未知（这条请求没有命中 Leader 的规则，"
                      "请你判断它是否属于 ROS2 学习领域）；"
                      f"水平：{s.get('level') or 'beginner'}")
    r1 = invoke_partner(
        need=NEED_REQUIREMENT,
        aic=REQUIREMENT_AIC,
        app_id=PARTNER1_APP,
        prompt=user_text,
        biz={"leader_context": leader_ctx,
             "history": s.get("ask_history") or ""},
    )
    steps.append({"step": "① 需求分析", "ok": r1["ok"],
                  "elapsed": r1["elapsed"], "error": r1["error"]})

    def _to_platform(reason: str) -> dict:
        log.info("转平台搭档：%s", reason)
        out = ask_platform(user_text)
        out["steps"] = steps + (out.get("steps") or [])
        return out

    if not r1["ok"]:
        return _to_platform(f"① 没跑通（{r1['error']}）")

    env1 = parse_partner1(r1["raw"])

    # ★ 输出不合法时，先怀疑"检索挑错了搭档"（平台上可能有别人家同名的
    #   「需求分析」，输出格式和我们的下游对不上），排除它再试一次。
    #   （网关那边会因此去试下一个候选；如果没有别的候选，会如实报错。）
    if not env1.get("valid") and REQUIREMENT_AIC:
        first_err = str(env1.get("error") or env1.get("reason") or "格式不合法")
        log.info("① 输出不合法（%s），排除 %s 重试一次", first_err[:80], REQUIREMENT_AIC)
        r1b = invoke_partner(
            need=NEED_REQUIREMENT,
            aic=REQUIREMENT_AIC,
            app_id=PARTNER1_APP,
            prompt=user_text,
            biz={"leader_context": leader_ctx,
                 "history": s.get("ask_history") or ""},
            exclude_aic=[REQUIREMENT_AIC],
        )
        steps.append({"step": "① 需求分析（换搭档重试）", "ok": r1b["ok"],
                      "elapsed": r1b["elapsed"], "error": r1b["error"]})
        if r1b["ok"]:
            env1b = parse_partner1(r1b["raw"])
            if env1b.get("valid"):
                log.info("换搭档后解析成功，继续流水线")
                env1 = env1b

    if not env1.get("valid"):
        return _to_platform("① 输出不合法")

    req = env1.get("data") or {}

    if _is_rejected(env1, req):
        return _to_platform("① 拒绝了这条请求")

    if env1.get("need_ask"):
        ask = env1.get("ask_question") or ""

        # ★ 关键兜底：① 没有"领域拒绝"这个出口（它的信封里只有
        #   valid / need_ask / data，没有 reject），所以遇到"推荐北京好吃的"
        #   这类请求时，它会走 need_ask 通道，追问一句"请选择 ROS2 项目方向"
        #   —— 这实质就是领域拒绝，只是借了追问的壳。
        #
        #   直接把这个追问转给学生是错的（牛头不对马嘴），应该当成拒绝 → 转平台。
        #   （注册到平台之后，接入层的门卫会先 reject 掉这类请求；这里是双保险。）
        if any(k in ask for k in ("不属于", "无关", "不是 ROS", "无法为其",
                                  "不在我", "超出了", "不属于我")):
            log.info("① 用追问的形式表达了领域拒绝，转平台：%s", ask[:80])
            return _to_platform(f"① 判定不属于 ROS2 范畴（追问形式）：{ask[:60]}")

        if not ask:
            ask = "能再补充一点信息吗？"
        s["ask_history"] = (s.get("ask_history") or "") + f"\n[追问]{ask}"
        s["mode"] = "project"
        return {"reply": ask, "steps": steps, "state": "need_ask"}

    if not _has_concrete_question(req):
        return {"reply": "能再具体说说是想做什么吗？比如：\n"
                         "- 想生成某一种节点的代码\n"
                         "- 想做一个完整的项目（多个节点协作）\n"
                         "- 或者遇到了某个报错想排查",
                "steps": steps, "state": "clarify"}

    return run_from_requirement(req, s, steps)


def run_from_requirement(requirement: dict, s: dict, steps: list[dict]) -> dict:
    """从结构化需求继续：② 项目规划 → ③ 代码生成（分批）"""

    # ---------- ② 项目规划 ----------
    # ★ 关键：直连百炼时，只有 input.prompt 这条通道是可靠的
    #   （input.biz_params 传自定义变量实测读不到）。
    #   所以把需求 JSON 同时放进 prompt 和 biz：
    #     · 走平台 → invoke_partner 用 biz["requirement_json"] 当 input
    #     · 直连百炼 → 用 prompt 当 input.prompt
    requirement_json = json.dumps(requirement, ensure_ascii=False)
    r2 = invoke_partner(
        need=NEED_PLAN,
        aic=PLAN_AIC,
        app_id=PARTNER2_APP,
        prompt=requirement_json,
        biz={"requirement_json": requirement_json},
    )
    steps.append({"step": "② 项目规划", "ok": r2["ok"], "elapsed": r2["elapsed"],
                  "error": r2["error"]})
    if not r2["ok"]:
        return {"reply": f"项目规划没跑通：{r2['error']}", "steps": steps, "state": "failed"}

    env2 = parse_partner2(r2["raw"])
    if not env2.get("nodes"):
        # ★ 熔断：解析不出来就【到此为止】，绝不把半截原文当结果往下传。
        #   同时把原文落盘（解析类问题没有原文就没法复盘）。
        dumped = _dump_raw("partner2", r2["raw"], env2.get("error"))
        log.warning("② 解析失败，已熔断：%s（原文 %d 字，落盘=%s）",
                    env2.get("error"), len(r2["raw"] or ""), dumped)
        return {"reply": (f"项目规划结果解析失败，已中止，没有把半成品当成结果返回。\n\n"
                          f"原因：{env2.get('error')}\n"
                          f"原始输出已保存到：`{dumped or '(落盘失败)'}`（{len(r2['raw'] or '')} 字）\n\n"
                          f"（供排查用的开头 300 字）\n{r2['raw'][:300]}"),
                "steps": steps, "state": "failed"}

    plan = env2.get("plan") or {}
    nodes = env2["nodes"]

    # ★ 熔断二：节点规格不合法就不派给 ③（否则 ③ 会按垃圾规格生成垃圾代码）
    bad = [n for n in nodes if not _valid_node(n)]
    if bad:
        dumped = _dump_raw("partner2-badnodes", r2["raw"], "nodes 里有不合规的条目")
        log.warning("② 返回的 nodes 里有 %d 条不合规（缺 node_name），已熔断", len(bad))
        return {"reply": (f"项目规划结果不合规：有 {len(bad)}/{len(nodes)} 个节点缺少 node_name，"
                          f"已中止，没有把它们派给代码生成。\n\n"
                          f"原始输出已保存到：`{dumped or '(落盘失败)'}`"),
                "steps": steps, "state": "failed"}
    s["mode"] = "project"
    s["nodes"] = nodes
    s["plan"] = plan
    s["guide"] = env2.get("guide") or ""
    s["plan_md"] = render_plan_book(plan)
    s["generated"] = []

    # ---------- ③ 代码生成（分批）----------
    if len(nodes) >= BATCH_THRESHOLD:
        batch = nodes[:BATCH_FIRST]
        rest = len(nodes) - BATCH_FIRST
    else:
        batch = nodes
        rest = 0

    envs = concurrent_generate(batch, steps)
    s["generated"] = [e.get("node_name") for e in envs if e.get("valid")]

    body = [s["plan_md"] or "", "---", s["guide"] or ""]
    if envs:
        body += ["---", render_codes(envs)]
    reply = "\n\n".join(x for x in body if x)

    if rest:
        reply += (f"\n\n---\n\n已生成 {len(envs)}/{len(nodes)} 个节点。"
                  f"要继续生成剩余 {rest} 个，回复「继续」即可。")
    # ★ 不再追加 A/B/C/D 菜单：在叮当这类"每次调用都是新任务"的场景里，
    #   学生回一次 A 会被当成一条新请求，叮当再调一次 → 整条流水线重跑一遍，
    #   于是同一份规划书刷屏好几遍。规划已经给了，想要代码直接说就行。
    return {"reply": reply, "steps": steps, "state": "ok"}


def concurrent_generate(nodes: list[dict], steps: list[dict]) -> list[dict]:
    if not nodes:
        return []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, MAX_CONCURRENCY)) as pool:
        futs = {}
        for n in nodes:
            # ★ 同 ②：把节点规格同时放进 prompt 和 biz
            #   （直连百炼只有 input.prompt 可靠；走平台用 biz）
            node_spec_json = json.dumps(n, ensure_ascii=False)
            fut = pool.submit(
                invoke_partner,
                need=NEED_CODEGEN,
                aic=CODEGEN_AIC,
                app_id=PARTNER3_APP,
                prompt=node_spec_json,
                biz={"node_spec_json": node_spec_json},
            )
            futs[fut] = n
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                r3 = fut.result()
            except Exception as exc:  # noqa: BLE001
                r3 = {"ok": False, "raw": "", "error": repr(exc)[:200], "elapsed": 0}
            if r3["ok"]:
                env = parse_partner3(r3["raw"])
            else:
                env = {"valid": False, "error": r3["error"], "node_name": n.get("node_name")}
            env.setdefault("node_name", n.get("node_name"))
            env["_elapsed"] = r3["elapsed"]
            results.append(env)
            steps.append({"step": f"③ {n.get('node_name')}", "ok": bool(env.get("valid")),
                          "elapsed": r3["elapsed"], "error": env.get("error")})
    # 保持与输入顺序一致
    order = {n.get("node_name"): i for i, n in enumerate(nodes)}
    results.sort(key=lambda e: order.get(e.get("node_name"), 99))
    return results


def answer_directly(user_text: str, s: dict, intent: str) -> dict:
    """Leader 自答：概念讲解 / 闲聊 / 报错排查"""
    sys_prompt = ("你是 ROS2 Python 学习助手，面向初学者，语气亲切，像学长学姐。"
                  "回答要准确、简洁。")
    if intent == "报错排查":
        kb = search_kb(user_text)
        if kb.get("source_count"):
            sys_prompt += ("\n\n下面是知识库检索到的参考资料，请优先依据它回答，"
                           "并在末尾注明来源：\n" + kb["context"][:4000])
        else:
            sys_prompt += "\n\n知识库没有检索到相关资料，请依据你自己的知识回答，并提醒学生这一点。"
    msgs = [{"role": "system", "content": sys_prompt}]
    for t in (s.get("turns") or [])[-6:]:
        msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": user_text})
    try:
        text = _llm_chat(msgs, ANSWER_MODEL, ANSWER_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001
        return {"reply": f"我这边调用模型出错了：{repr(exc)[:200]}", "steps": [], "state": "failed"}
    return {"reply": text.strip(), "steps": [], "state": "ok"}


def handle_turn(user_text: str, session_id: str) -> dict:
    # ★ 第一件事：剥掉上游（叮当/平台）夹带的"上一轮全文 + 协作元信息"。
    #   否则规则表会被那些文字误判（实测把"写一个 hello 节点"判成"项目需求"），
    #   而且 ①②③ 拿到的输入也会被污染。
    clean_text = strip_upstream_wrapper(user_text)
    if clean_text != (user_text or "").strip():
        log.info("已剥离上游包装：原 %d 字 → 净 %d 字",
                 len(user_text or ""), len(clean_text))
    user_text = clean_text

    s = load_session(session_id)
    push_turn(s, "user", user_text)

    # ---------- 项目模式下优先看是不是 A/B/C/D ----------
    if s.get("mode") == "project" and s.get("nodes"):
        choice = parse_choice(user_text)
        if choice == "A":
            s["generated"] = s.get("generated") or []
            out = {"reply": "好的，规划确认了。\n\n" + render_options(confirmed=True),
                   "steps": [], "state": "ok"}
        elif choice == "B":
            out = run_pipeline((s.get("ask_history") or "") + "\n修改意见：" + user_text, s)
        elif choice == "C":
            m = NODE_NAME_RE.search(user_text)
            name = m.group(1) if m else None
            if not name:
                out = {"reply": "请告诉我要生成哪个节点（节点名以 `_node` 结尾）。",
                       "steps": [], "state": "ok"}
            else:
                node = next((n for n in s["nodes"]
                             if n.get("node_name") == name or name in (n.get("file_name") or "")), None)
                if not node:
                    out = {"reply": f"未找到节点 `{name}`，请检查名称。", "steps": [], "state": "ok"}
                else:
                    steps: list[dict] = []
                    envs = concurrent_generate([node], steps)
                    s["generated"] = list(dict.fromkeys((s.get("generated") or [])
                                                        + [e.get("node_name") for e in envs if e.get("valid")]))
                    out = {"reply": render_codes(envs), "steps": steps, "state": "ok"}
        elif choice == "D":
            todo = [n for n in s["nodes"]
                    if n.get("node_name") not in (s.get("generated") or [])]
            if not todo:
                out = {"reply": "所有节点都已生成，无需重复生成。", "steps": [], "state": "ok"}
            else:
                if len(todo) >= BATCH_THRESHOLD:
                    todo = todo[:BATCH_FIRST]
                steps = []
                envs = concurrent_generate(todo, steps)
                s["generated"] = list(dict.fromkeys((s.get("generated") or [])
                                                    + [e.get("node_name") for e in envs if e.get("valid")]))
                left = [n for n in s["nodes"] if n.get("node_name") not in s["generated"]]
                reply = render_codes(envs)
                if left:
                    reply += f"\n\n已生成 {len(s['generated'])}/{len(s['nodes'])} 个节点。继续？（继续 / 停一下）"
                out = {"reply": reply, "steps": steps, "state": "ok"}
        else:
            out = {"reply": "请回复 A（确认规划）、B（修改规划）、C（生成某个节点）、D（生成全部节点）。",
                   "steps": [], "state": "ok"}
    else:
        # ---------- 新需求：先分类 ----------
        intent = rule_classify(user_text)
        if intent is None:
            # 规则没命中 → 交给 Partner ① 当门卫（ROS2 识别是它的职责）
            log.info("规则未命中，交给 ① 判断")
            out = gate_and_route(user_text, s)
        elif intent == "单节点":
            # ★ 轻量路径：单节点不需要 ①②，直接把原话交给 ③ 生成代码
            out = run_single_node(user_text, s)
        elif intent == "项目需求":
            out = run_pipeline(user_text, s)
        else:
            out = answer_directly(user_text, s, intent)

    # ★ 混合请求收尾：
    #   ROS2 那部分已经答完了，非 ROS2 那部分【真的转给平台上别的搭档】，
    #   把结果附在后面 —— 这才是"编排"该有的样子（而不是静默丢掉，或者只道歉）。
    #   转不到（平台没有合适搭档）时，退回原来那句说明。
    #   ★ 注意：如果本次本来就是"整条都转平台"（steps 里有"平台搭档"），就别再转一次。
    _already_forwarded = any(str(st.get("step", "")).startswith("平台搭档")
                             for st in (out.get("steps") or []))
    if (out.get("state") == "ok" and _has_offtopic(user_text)
            and not _already_forwarded):
        _ros_part, _other_part = split_mixed_request(user_text)
        if _other_part:
            log.info("混合请求：非 ROS2 部分转平台 → %s", _other_part[:60])
            plat = ask_platform(_other_part)
            out["steps"] = (out.get("steps") or []) + (plat.get("steps") or [])
            _got = (plat.get("reply") or "").strip()
            if plat.get("state") == "ok" and _got:
                out["reply"] = _append_note(
                    out.get("reply") or "",
                    "## 🔗 你问的另一部分，我转给平台搭档了\n\n" + _got)
                out["offtopic_forwarded"] = True
            else:
                out["reply"] = _append_note(out.get("reply") or "", APOLOGY)
                out["offtopic_dropped"] = True
        else:
            # 拆不出独立的非 ROS2 段（只是夹带了个别关键词）→ 只补一句说明
            log.info("检出混合请求关键词，补一句说明")
            out["reply"] = _append_note(out.get("reply") or "", APOLOGY)
            out["offtopic_dropped"] = True

    push_turn(s, "assistant", out.get("reply", "")[:500])
    save_session(s)
    out["session_id"] = session_id
    out["mode"] = s.get("mode") or ""
    out["nodes"] = [n.get("node_name") for n in (s.get("nodes") or [])]
    out["generated"] = s.get("generated") or []
    return out


# ===========================================================================
# HTTP 接口
# ===========================================================================
app = FastAPI(
    title="ROS2 学习智能体 · Leader 编排服务",
    version="0.1.0",
    description=("代码层编排：意图分类 → 调 Partner ①②③ → 汇总。\n\n"
                 "把「决策」放在代码里（确定），把「生成」交给百炼的工作流（智能）。"),
)


def require_token(x_auth_token: str | None = Header(default=None,
                                                   include_in_schema=False)) -> None:
    if AUTH_TOKEN and x_auth_token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="X-Auth-Token 不正确")


class LeaderRequest(BaseModel):
    text: str = Field(..., description="学生说的话")
    session_id: str | None = Field(default=None, description="会话 id（不传则自动生成）")


class LeaderResponse(BaseModel):
    reply: str = Field(..., description="要展示给学生的话（直接原文输出）")
    session_id: str = Field(..., description="会话 id（下一轮要带回来）")
    state: str = Field(..., description="ok / need_ask / clarify / failed")
    mode: str = Field(default="", description="当前模式：project / single / ''")
    nodes: list[str] = Field(default_factory=list, description="节点清单（节点名）")
    generated: list[str] = Field(default_factory=list, description="已生成的节点")
    steps: list[dict] = Field(default_factory=list, description="各步耗时与结果（调试用）")


# ---------------------------------------------------------------------------
# OpenAI 兼容入口
# ---------------------------------------------------------------------------
# 为什么要有这个：
#   梧桐的接入层框架（demo-partner）只会发 OpenAI 格式的请求
#   （POST /v1/chat/completions），它没有"自己发任意 HTTP"的能力。
#   所以要让"叮当 → 平台 → 我们的接入层 → Leader"这条链路通，
#   Leader 就得装成一个 OpenAI 兼容的服务。
#
#   ★ 注意：这里刻意用 def（同步）而不是 async def —— handle_turn 全程阻塞
#     （要等百炼/平台返回），FastAPI 会把同步路由丢进线程池，不堵事件循环。
# ---------------------------------------------------------------------------
def _last_user_text(messages: list) -> str:
    """从 OpenAI 风格的 messages 里取"最后一条 user 消息的文本"（和 shim 一致）"""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):      # 多段 content 的写法
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            joined = "".join(parts).strip()
            if joined:
                return joined
    # 兜底：取最后一条任何角色的文本
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            if msg["content"].strip():
                return msg["content"]
    return ""


# ★ 给"外部插件"用的入口密钥。
#   Leader 的 /v1/chat/completions 现在暴露在公网上（百炼的自定义插件要直连它），
#   如果不设密钥，任何扫到 9200 的人都能调它 → 白烧百炼额度。
#   所以设 PLUGIN_KEY 后，请求必须带上它。
#   （留空 = 不校验，方便本机调试；生产环境务必设一个。）
PLUGIN_KEY = env("PLUGIN_KEY", "")

# 密钥可以放在这些地方（谁方便放哪就放哪）：
#   · 请求头 X-Plugin-Key
#   · URL 查询串 ?key=... / ?plugin_key=...
#   · 请求体里的 key / plugin_key / pluginKey 字段
_KEY_FIELDS = ("key", "plugin_key", "pluginKey", "plugin-key",
               "X-Plugin-Key", "x_plugin_key", "x-plugin-key", "token")


def _check_plugin_key(header_key: str | None,
                      query: dict | None = None,
                      payload: dict | None = None,
                      authorization: str | None = None) -> None:
    """校验插件入口的密钥。PLUGIN_KEY 为空时直接放行。"""
    if not PLUGIN_KEY:
        return
    cands: list[str] = [header_key or ""]
    # ★ 也接受 OpenAI 风格的 Authorization: Bearer <PLUGIN_KEY>。
    #   接入层框架（demo-partner）只会发这个头，它不做自定义请求头，
    #   所以 8449 → Leader 这一跳用它带密钥。
    if authorization:
        _a = authorization.strip()
        if _a.lower().startswith("bearer "):
            _a = _a[7:].strip()
        cands.append(_a)
    for src in (query, payload):
        if isinstance(src, dict):
            for f in _KEY_FIELDS:
                v = src.get(f)
                if isinstance(v, str):
                    cands.append(v)
    for c in cands:
        if c.strip() == PLUGIN_KEY:
            return
    raise HTTPException(status_code=401, detail="缺少或不正确的插件密钥（X-Plugin-Key）")


# 学生的话可能在请求体的这些字段里（百炼自定义工具通常发扁平结构）
_TEXT_FIELDS = ("content", "question", "prompt", "text", "input",
                "query", "message", "q", "user_input", "userInput")


def _extract_request_text(payload: dict | None, query: dict | None = None) -> str:
    """
    从请求里取"学生说的话"。兼容：
      ① OpenAI 标准格式：{"messages":[{"role":"user","content":"..."}]}
      ② 扁平格式（百炼自定义工具）：{"content": "..."}
      ③ 扁平字段放在 URL 查询串里：?content=...
      ④ 兜底：整个 body 里只有一个字符串值时，就用它
    """
    payload = payload or {}
    query = query or {}

    # ① OpenAI 格式
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        t = _last_user_text(messages)
        if t:
            return t

    # ② body 里的扁平字段
    for f in _TEXT_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # ③ query 里的扁平字段
    for f in _TEXT_FIELDS:
        v = query.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # ④ 兜底：body 里恰好只有一个字符串值（且不是密钥字段）
    strs = [v for k, v in payload.items()
            if isinstance(v, str) and v.strip() and k not in _KEY_FIELDS]
    if len(strs) == 1:
        return strs[0].strip()

    return ""


@app.post("/v1/chat/completions", include_in_schema=False)
def openai_compat(payload: dict,
                  request: Request,
                  x_plugin_key: str | None = Header(default=None, alias="X-Plugin-Key"),
                  authorization: str | None = Header(default=None, alias="Authorization"),
                  key: str | None = Query(default=None),
                  plugin_key: str | None = Query(default=None)):
    """
    OpenAI 兼容入口 —— 同时服务三种调用方：

      · 接入层框架（发 OpenAI 标准格式）
      · 百炼自定义插件（通常发扁平格式，如 {"content": "..."}）
      · 手工 curl 调试

    输入很宽容：见 _extract_request_text。
    输出是 OpenAI 标准格式（choices[0].message.content 就是给用户看的文本）。

    ★ 配了 PLUGIN_KEY 时，密钥可以放在：请求头 X-Plugin-Key、
      请求头 Authorization: Bearer <PLUGIN_KEY>（接入层框架用这个）、
      URL 查询串 ?key= 或 ?plugin_key=、或请求体里的 key/plugin_key 字段。
    """
    qp = dict(request.query_params)
    _check_plugin_key(x_plugin_key, qp, payload, authorization)
    # 查询串里的 key 参数也并进去（免得它被当成"学生的话"）
    if key:
        qp.setdefault("key", key)
    if plugin_key:
        qp.setdefault("plugin_key", plugin_key)

    text = _extract_request_text(payload, qp)
    if not text:
        return JSONResponse(
            {"error": {"message": "没拿到有效文本。请用 messages / content / question 等字段传学生的话",
                       "type": "invalid_request_error"}},
            status_code=400,
        )

    # 会话 id：优先用调用方给的 user / session_id 字段；没有就生成一个
    # （平台入口以单轮为主，每次新会话是可接受的）
    session_id = str(payload.get("user") or payload.get("session_id")
                     or qp.get("user") or qp.get("session_id") or "").strip()
    if not session_id:
        session_id = f"plat-{uuid.uuid4().hex[:12]}"

    log.info("OpenAI 兼容入口：session=%s text=%.60s", session_id, text.replace("\n", " "))
    try:
        out = handle_turn(text, session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("OpenAI 兼容入口编排异常")
        out = {"reply": f"我这边编排出错了：{repr(exc)[:300]}", "state": "failed"}

    content = out.get("reply") or ""
    pt = max(1, len(text) // 4)
    ct = max(1, len(content) // 4)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or "leader",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": pt + ct},
        # ★ 给"输出参数只能配一层字段"的工具留的便利字段
        "result": content,
        "reply": content,
    }


@app.get("/v1/models", include_in_schema=False)
def openai_models():
    """让接入层/客户端能列出模型（随便给一个名字就行）"""
    return {
        "object": "list",
        "data": [{"id": "leader", "object": "model",
                  "created": 1700000000, "owned_by": "ros2-leader"}],
    }


@app.get("/health")
def health():
    problems = []
    if not DASHSCOPE_API_KEY:
        problems.append("DASHSCOPE_API_KEY 未配置")

    # 三个 Partner 的调用方式：配了 AIC 走平台，否则退回直连百炼
    def _mode(aic: str, app_id: str, name: str) -> str:
        if aic:
            return f"平台（only_aic={aic[:28]}…）"
        if app_id:
            return f"直连百炼（appId={app_id[:8]}…）"
        problems.append(f"{name} 既没配 AIC 也没配 appId")
        return "未配置"

    return {
        "ok": not problems,
        "problems": problems,
        "mode": {
            "① 需求分析": _mode(REQUIREMENT_AIC, PARTNER1_APP, "PARTNER1_APP"),
            "② 项目规划": _mode(PLAN_AIC, PARTNER2_APP, "PARTNER2_APP"),
            "③ 代码生成": _mode(CODEGEN_AIC, PARTNER3_APP, "PARTNER3_APP"),
        },
        "need": {"①": NEED_REQUIREMENT, "②": NEED_PLAN, "③": NEED_CODEGEN},
        "gateway": GATEWAY_URL or "(未配置)",
        "kb": KB_URL or "(未配置)",
        "db": str(DB_PATH),
    }


@app.post("/leader", response_model=LeaderResponse,
          summary="一次对话（代码层编排）",
          description=("把学生说的话交给 Leader 编排：先分类意图，"
                       "需要产出内容时调度 Partner ①②③，最后汇总返回。\n\n"
                       "**session_id 必须跨轮带回来**，否则记不住项目上下文。"))
def leader(req: LeaderRequest, _: None = Depends(require_token)):
    session_id = (req.session_id or "").strip() or f"s-{uuid.uuid4().hex[:12]}"
    text = (req.text or "").strip()
    if not text:
        return {"reply": "请说点什么 😊", "session_id": session_id,
                "state": "ok", "mode": "", "nodes": [], "generated": [], "steps": []}
    log.info("── 会话 %s ── 输入：%s", session_id, text[:80])
    try:
        out = handle_turn(text, session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("编排异常")
        return {"reply": f"我这边编排出错了：{repr(exc)[:300]}", "session_id": session_id,
                "state": "failed", "mode": "", "nodes": [], "generated": [], "steps": []}
    log.info("── 会话 %s ── 状态=%s 步骤=%d", session_id, out.get("state"), len(out.get("steps") or []))
    return out


@app.post("/leader/reset", include_in_schema=False)
def reset(payload: dict, _: None = Depends(require_token)):
    sid = (payload.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    return {"ok": True, "session_id": sid}


@app.get("/session/{session_id}", include_in_schema=False)
def show_session(session_id: str, _: None = Depends(require_token)):
    s = load_session(session_id)
    return JSONResponse({
        "session_id": s["session_id"], "mode": s.get("mode"),
        "level": s.get("level"),
        "nodes": [n.get("node_name") for n in (s.get("nodes") or [])],
        "generated": s.get("generated"),
        "turns": s.get("turns"),
        "ask_history": s.get("ask_history"),
        "plan_md_len": len(s.get("plan_md") or ""),
    })


if __name__ == "__main__":
    import uvicorn

    init_db()
    log.info("启动 leader  %s:%s", HOST, PORT)
    log.info("  ① 需求分析 : %s", REQUIREMENT_AIC or PARTNER1_APP or "(未配置)")
    log.info("  ② 项目规划 : %s", PLAN_AIC or PARTNER2_APP or "(未配置)")
    log.info("  ③ 代码生成 : %s", CODEGEN_AIC or PARTNER3_APP or "(未配置)")
    log.info("  调用方式   : %s", "走平台（only_aic 锁定）" if REQUIREMENT_AIC
             else "直连百炼（还没配 AIC）")
    log.info("  网关       : %s", GATEWAY_URL or "(未配置)")
    log.info("  知识库      : %s", KB_URL or "(未配置)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
