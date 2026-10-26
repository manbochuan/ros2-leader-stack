# ROS2 学习编排智能体（Leader 的平台入口）

> 这个实例很特殊：**它本身不做业务逻辑**，只把请求转发给 **Leader 服务（:9200）**。
> 目的：让**叮当/平台上的其他智能体**能够发现并调用我们的编排能力。

---

## 一、它和 ros2_tutor 的区别

| | **ros2_tutor**（现有，:8443） | **本实例**（新，:8449） |
|---|---|---|
| **定位** | **能力型**：教学 + 代码生成 | **编排型**：路由 + 多步调度 |
| **后端** | shim → 百炼教学工作流 | **Leader :9200** |
| **产出** | 教学文章 / 代码 | **编排结果**（自答 / 转平台 / 流水线汇总） |
| **门卫** | 判断领域（沾 ROS2 就放行） | **一律放行**（领域判断在 Leader 里做） |

**→ 两个都保留，叮当能调到**两类**能力。**

---

## 二、三阶段设计（`prompts.toml`）

| 阶段 | 做什么 | 为什么 |
|---|---|---|
| **`[decision]`** | **一律 accept**（只拒绝空输入） | **领域判断交给 Leader**（代码，确定），这层不做判断 |
| **`[analysis]`** | **一字不改复制** | Leader 需要**原始请求**来分类，任何加工都会让它判断错 |
| **`[production]`** | `llm_profile="default"` → 指向 **Leader 的 OpenAI 兼容入口** | 见下 |

---

## 三、⭐ 关键：Leader 有个 OpenAI 兼容入口

**接入层框架只会发 OpenAI 格式（`POST /v1/chat/completions`），所以 Leader 加了这个入口：**

```
接入层 [production]
   ↓ POST http://127.0.0.1:9200/v1/chat/completions
     {"model": "leader", "messages": [{"role":"user","content":"..."}]}
   ↓
Leader 的 /v1/chat/completions
   · 取最后一条 user 消息 → 交给 handle_turn()
   · 把 reply 包成 OpenAI 响应返回
```

**→ 所以本实例的 `.env` 要这样配（★ 和 ros2_tutor 不同）：**

```ini
APP_ENV=development

# fast 档：百炼小模型（decision / analysis 用）
PARTNER_LLM_FAST_API_KEY=sk-你的百炼APIKEY
PARTNER_LLM_FAST_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
PARTNER_LLM_FAST_MODEL=qwen-turbo

# default 档：★ 指向 Leader（不是 shim）
PARTNER_LLM_DEFAULT_API_KEY=sk-local-leader
PARTNER_LLM_DEFAULT_BASE_URL=http://127.0.0.1:9200/v1
PARTNER_LLM_DEFAULT_MODEL=leader
```

```bash
chmod 600 .env
```

---

## 四、部署步骤

```bash
# 1. 目录
mkdir -p /opt/acps-agent/acps-community/demo-partner/partners/online/leader_orchestrator
cd /opt/acps-agent/acps-community/demo-partner/partners/online/leader_orchestrator

# 2. 放入三个文件
#    prompts.toml / config.toml / acs.json  （本目录的三个文件）

# 3. 写 .env（见第三节），chmod 600

# 4. 证书：注册后拿到 server.pem / server.key，放到本目录
#    trust-bundle.pem 可以从 ros2_tutor 那边复制：
#    cp ../ros2_tutor/trust-bundle.pem .

# 5. 改 acs.json：填平台签发的 AIC

# 6. 确认 Leader 在跑
systemctl is-active leader
curl -s http://127.0.0.1:9200/health | python3 -m json.tool

# 7. 重启接入层（框架会加载新 agent）
systemctl restart acps-agent
journalctl -u acps-agent -n 30 --no-pager

# 8. 本机快速验证（TLS 未开时可以直接 curl）
curl -s -X POST http://127.0.0.1:8449/rpc \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"start","params":{"command":{"dataItems":[{"type":"text","text":"什么是 ROS2 的话题？"}]}}}' \
  | head -c 400
```

---

## 五、注册后在平台上的样子

```
梧桐平台
├── ros2_tutor                :8443   …AXZERQ…   能力型（教学）
├── ROS2 需求分析              :8446   待注册     能力型
├── ROS2 项目规划              :8447   待注册     能力型
├── ROS2 代码生成              :8448   待注册     能力型
└── ROS2 学习编排智能体         :8449   ★待注册   编排型（转发 Leader）
```

**→ 注册完之后：**
- **叮当可以直接调「ROS2 学习编排智能体」→ 用上 Leader 的完整编排**
- **百炼主 agent 也可以用 `call_partner(aic=<它的AIC>)` 精确调它**

---

## 六、完整调用链（注册后）

```
用户在叮当提问
   ↓
平台 → 【ROS2 学习编排智能体】（:8449 接入层）
          [decision] 一律放行
          [analysis] 一字不改
          [production] → Leader :9200/v1/chat/completions
   ↓
Leader :9200
   ├─ 概念/报错/闲聊  → 调 qwen-flash 自答
   ├─ 其他领域        → 网关 :9100 → 平台 → 别的搭档
   └─ ROS2 复杂需求   → ①②③ 流水线 → 汇总
   ↓
原路返回 → 用户看到结果
```

---

## 七、⚠️ 已知限制

| # | 限制 | 说明 |
|---|---|---|
| **1** | **会话状态** | 平台每次调用是新任务 → Leader 每次新会话 → **多轮编排（A/B/C/D）在这个入口失效**。<br>叮当网页以单轮为主，可接受。 |
| **2** | **输入的包装** | 框架的 `[production]` 只能引用 `{{requirements}}`（整段 JSON），所以 Leader 收到的是<br>`{"original_request": "原话"}` 而非纯原话。规则表是关键匹配，包一层不影响分类。 |
| **3** | **耗时** | 一次编排可能 20~90 秒（转平台找搭档要等）。平台侧要有足够的超时容忍。 |
