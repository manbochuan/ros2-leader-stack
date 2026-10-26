# Leader —— ROS2 学习智能体的代码层编排服务

## 它解决什么问题

原来的「主 agent」是百炼的智能体应用，**编排全靠提示词**。实测下来三个硬伤：

| 现象 | 实测证据 |
|---|---|
| **大模型会跳步** | 让它按提示词走两步（`discover_partner` → `call_partner`），它只走第一步，把候选列表当答案贴出来了 |
| **大模型会忘状态** | 让它记住「节点清单」「已生成节点」，多轮后就开始重复生成 |
| **大模型不守上下文纪律** | 提示词里写「不要保留规划书全文」，它照样保留 |

所以把**编排**从提示词搬到代码：

```
百炼主 agent  = 「嘴」   人设、语气、展示结果、概念问答
本服务        = 「脑」   意图分类、会话状态、调度 Partner ①②③、汇总
百炼工作流    = 「手」   真正生成需求规格 / 规划书 / 代码
```

**关键点：「决策」由代码做（确定），「生成」交给云端大模型（智能）。**

---

## 架构

```
学生在百炼主 agent 提问
        ↓  插件工具 leader_orchestrate(text, session_id)
┌───────────────────────────────────────────────┐
│  本服务（你服务器上的 9200 端口）                │
│                                               │
│  ① 意图分类（规则表 + 兜底）                    │
│  ② 会话状态（SQLite：节点清单/已生成/15轮窗口）  │
│  ③ 调度：                                      │
│      项目需求 → Partner① → Partner② → Partner③ │
│      概念/闲聊 → 自己答（调 qwen-flash）        │
│      报错排查 → 知识库检索 + 自己答              │
│  ④ 汇总渲染（规划书模板 + 代码合并）             │
└──────────────────┬────────────────────────────┘
                   │ HTTP（DashScope API）
                   ↓
        百炼的三个 Partner 工作流应用
```

> **模型始终在云端**，本服务只发 HTTP 请求。服务器上不跑任何模型。

---

## 部署

### 1. 建目录 + 虚拟环境

```bash
mkdir -p /opt/acps-agent/leader
cd /opt/acps-agent/leader

python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

（`requirements.txt`、`leader.py`、`.env.example` 从本地上传）

### 2. 配置

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

**要填的：**

| 变量 | 说明 |
|---|---|
| `AUTH_TOKEN` | 随机串，和百炼插件里的 `X-Auth-Token` 一致 |
| `DASHSCOPE_API_KEY` | 百炼 API Key |
| `PARTNER1_APP` / `PARTNER2_APP` / `PARTNER3_APP` | 三个 Partner 的 appId |
| `BIZ_MODE` | 传参形态（**目前未最终确认**，见下） |
| `KB_URL` | 知识库检索接口（报错排查用） |

### 3. 起服务

```bash
cp leader.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now leader
sleep 5
systemctl is-active leader
journalctl -u leader -n 20 --no-pager
```

### 4. 放行端口

```bash
ufw allow 9200/tcp
# 阿里云安全组也要加 9200
```

### 5. 验证

```bash
TOKEN=$(grep '^AUTH_TOKEN=' /opt/acps-agent/leader/.env | cut -d= -f2)

curl -s http://127.0.0.1:9200/health | python3 -m json.tool

curl -s -X POST http://127.0.0.1:9200/leader \
  -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"帮我做一个避障小车"}' | python3 -m json.tool | head -40
```

---

## 接口

### `POST /leader` —— 一次对话

```json
// 请求
{"text": "帮我做一个避障小车", "session_id": "s-abc123"}

// 响应
{
  "reply": "……要展示给学生的话……",
  "session_id": "s-abc123",        // ★ 下一轮必须带回来
  "state": "ok",                   // ok / need_ask / clarify / failed
  "mode": "project",
  "nodes": ["obstacle_detector_node", "motion_controller_node"],
  "generated": [],
  "steps": [{"step": "① 需求分析", "ok": true, "elapsed": 10.8}]
}
```

### `GET /health` —— 健康检查
### `POST /leader/reset` —— 清掉某个会话（调试）
### `GET /session/{id}` —— 看会话状态（调试）

---

## 意图分类怎么做的

**规则表优先（0 成本、0 延迟、确定）：**

| 意图 | 关键词（部分） | 走哪条路 |
|---|---|---|
| **报错排查** | 报错 / 错误 / error / traceback / nan / 跑不起来 | 知识库检索 + Leader 自答 |
| **概念讲解** | 什么是 / 怎么理解 / 区别 / 为什么 | Leader 自答 |
| **项目需求** | 项目 / 系统 / 小车 / 机器人 / 搭建 / 帮我做 | **Partner ① → ② → ③** |
| **单节点** | 写一个 / 节点 / 发布者 / 订阅者 | **Partner ① → ② → ③** |
| **闲聊** | 你好 / 谢谢 / 在吗 | Leader 自答 |

**规则没命中** → 反问学生澄清（也可以用 `ROUTER_MODEL` 兜底，见"待办"）。

**为什么规则够用：** 大部分"下一步"不是靠理解得来的，而是**数据结构决定的**：

```
① 返回 need_ask = true   → 把追问转给学生           （不用判断）
① 返回 data              → 调 ②                     （不用判断）
② 返回 node_list         → 并发调 ③ × N             （不用判断）
③ 返回 passed = false    → 标注"存疑"，不阻塞其余    （不用判断）
```

**整条流水线只有"入口分类"一处需要判断。**

---

## ⚠️ 已知未决：Partner 的传参形态

**同学的三个 Partner 是百炼「工作流应用」，开始节点有自定义变量：**

| Partner | 需要的变量 |
|---|---|
| ① 需求分析 | `leader_context`、`history`（另有 `input.prompt`） |
| ② 项目规划 | `requirement_json`（**只吃这个**，不吃 prompt） |
| ③ 代码生成 | `node_spec_json`（**只吃这个**） |

**这些变量怎么通过 API 传进去，目前实测未通：**

| 试过的形态 | 结果 |
|---|---|
| 顶层 `biz_params.requirement_json`（字符串） | ❌ 输出 `xxx` 模板 |
| `input.biz_params.requirement_json`（字符串） | ❌ 输出 `xxx` 模板 |
| `input.biz_params.requirement_json`（对象） | ❌ 输出 `xxx` 模板 |
| 不传 `flow_stream_mode` | ❌ 输出 `xxx` 模板 |

**但同一个工作流在百炼控制台「测试运行」里跑是**正常的**（输出完整规划 + 两个标记）。**

**→ 最可疑的假设：控制台跑的是「草稿版」，API 跑的是「已发布版」—— 如果改造后没重新发布，API 拿到的就是旧版本。**

**→ 待确认：让同学看那个工作流「最后一次发布是什么时候」、有没有"未发布的修改"。**

**`BIZ_MODE` 就是为这个留的开关**，确认后改 `.env` 一行即可，代码不用动。

---

## 待办（同学文档标注的"Leader 待补项"）

| # | 事项 | 状态 |
|---|---|---|
| 1 | **确认 Partner 传参形态** | ⏳ 卡住（见上） |
| 2 | **幂等**（`node_name` 作幂等键，同一节点本轮只保留一份成功结果） | ⬜ 未做 |
| 3 | **成本熔断**（单轮 token 预算上限，超了降级不阻塞） | ⬜ 未做 |
| 4 | **异步 + 进度反馈**（"正在规划…"/"正在生成第 2/4 个节点…"） | ⬜ 未做 |
| 5 | **意图分类的小模型兜底**（规则没命中时调 `ROUTER_MODEL`） | ⬜ 未做（现在只反问） |
| 6 | **会话状态的过期清理**（现在只增不删） | ⬜ 未做 |
| 7 | **单元测试**（分类、解析、状态机） | ⬜ 未做 |

---

## 和别的组件的关系

| 组件 | 作用 | 本服务怎么用它 |
|---|---|---|
| **百炼主 agent** | 对话外壳 | 它调本服务的 `/leader` |
| **百炼 Partner ①②③** | 生成内容 | 本服务直连 DashScope API 调它们 |
| **acps-gateway**（9100） | 调梧桐平台上的搭档 | 【待接】Leader 判断"超出 ROS2 范围"时，转给网关的 `ask_platform` |
| **bailian-shim**（9099） | Partner 接入层 | 【待用】如果 Partner 要上梧桐，走它 |
