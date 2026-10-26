# ros2-leader-stack

> **一个"会派活"的 ROS2 学习智能体** —— Leader 负责判断和调度，三个专业工作流负责干活。
>
> 面向 ROS2 课程/实训场景：学生随口说一句「帮我做一个避障小车」，系统自动完成
> **需求分析 → 项目规划 → 代码生成 → 汇总成规划书 + 学习指南 + 可运行代码**，
> 并作为一个标准 AIP 智能体注册到智能体互联平台上，可被其他智能体（或对话前端）调用。

<p align="center">
  <b>Python 3.10+ · FastAPI · SQLite · ACPs/AIP v2.2 · 百炼工作流 · systemd</b>
</p>

---

## 目录

- [这是什么](#这是什么)
- [架构](#架构)
- [它能干什么（五条路）](#它能干什么五条路)
- [快速开始：一键部署](#快速开始一键部署)
- [目录结构](#目录结构)
- [三个 Partner 是什么](#三个-partner-是什么)
- [配置项](#配置项)
- [一键部署脚本做了什么](#一键部署脚本做了什么)
- [验证](#验证)
- [实测数据](#实测数据)
- [我们踩过的坑](#我们踩过的坑)
- [已知限制](#已知限制)
- [安全提醒](#安全提醒)
- [License](#license)

---

## 这是什么

一句话：**Leader 不聪明，但它不会犯错。**

| 角色 | 是谁 | 干什么 | 在哪 |
|---|---|---|---|
| 决策 + 调度 | **Leader** (`leader.py`) | 规则表判意图、SQLite 记会话、派活、汇总、渲染 | 本机 `:9200` |
| 能力 ① | **ros2_analyze** | 把"我想做个避障小车"变成机器可读的规格 JSON | 百炼工作流 + 接入层 `:9021` |
| 能力 ② | **ros2_plan** | 把规格变成节点清单 + 话题契约 + 学习指南 | 百炼工作流 + 接入层 `:9022` |
| 能力 ③ | **ros2_codegen** | 按节点规格生成代码（带 AST 自检 + 自动修） | 百炼工作流 + 接入层 `:9023` |
| 对外身份 | **leader_orchestrator** | 平台上的那个"智能体"：接 AIP 请求、验证书、转给 Leader | 接入层 `:8449` |
| 适配器 | **gateway / shim / amp-forwarder** | 普通 HTTP ↔ AIP+mTLS / OpenAI ↔ 百炼 / 心跳上报 | `:9100` `:9099` |

> **关键设计：决策用代码，产出用模型。**
> 意图分类、流程编排、状态管理、结果渲染全是确定性的代码 —— 可复现、可观测、不会跳步；
> 真正的"智能"（分析、规划、写代码）交给三个专业工作流。

---

## 架构

```
        学生 / 对话前端 / 其他智能体
                     │  AIP over mTLS（带证书）
                     v
        ┌──────────────────────────────────┐
        │  智能体互联平台（注册 / 发现 / 审计） │
        └──────────────────────────────────┘
                     │  按 AIC 找地址
                     v
        ┌──────────────────────────────────┐
        │ 8449 leader_orchestrator（接入层）  │  ← 平台上注册的"这个智能体"
        │  · 验证书、验身份（identity_binding）│     AIC 挂在它身上
        │  · AIP JSON-RPC → 普通 HTTP        │
        └──────────────────────────────────┘
                     │  HTTP
                     v
        ┌──────────────────────────────────┐
        │ 9200 Leader（本项目的核心）         │  ← 领导层 + 编排层
        │  · 剥包装 → 规则分类 → 分派         │
        │  · SQLite 记会话（WAL，多线不锁库）  │
        │  · 汇总 / 渲染规划书 / 代码汇总      │
        └──────────────────────────────────┘
              │                      │
      自己答（概念/报错）        要专家干
              v                      v
          直接回复         ┌──────────────────┐
                          │ 9100 gateway      │  普通 HTTP → AIP + mTLS
                          └──────────────────┘
                                   │
                     ┌─────────────┼─────────────┐
                     v             v             v
                 9021 ①        9022 ②        9023 ③
               需求分析       项目规划       代码生成
                     │             │             │
                     └──── 9099 bailian-shim ────┘   OpenAI 格式 ↔ 百炼工作流
                                   │
                                   v
                          百炼上的三个工作流（真正的大模型）
```

---

## 它能干什么（五条路）

同一个入口，五条完全不同的内部路径 —— **用户不需要知道自己在走哪条**：

| 学生说 | 规则判定 | 实际走的路线 | 实测耗时 |
|---|---|---|---|
| 「什么是 ROS2 的话题？」 | 概念讲解 | Leader 自答（不调任何专家） | ≈ 4 s |
| 「我跑节点时报错了」 | 报错排查 | Leader 自答 + 知识库检索 | ≈ 10 s |
| 「帮我写一个发布 hello 的节点」 | 单节点 | Leader → ③（只生成一个节点） | ≈ 65 s |
| 「帮我做一个避障小车」 | 项目需求 | Leader → ① → ② → ③（并发）→ 渲染 | ≈ 120–210 s |
| 「推荐几个北京好吃的」 | 未命中 | Leader → ① 当门卫 → 拒绝 → 转平台找搭档 | ≈ 25 s |

还有一条**混合请求**分支：一句话里既有 ROS2 又有别的领域（例如"先写个 hello 节点，再写个 Java 贪吃蛇"），
Leader 会把请求拆两半 —— ROS2 自己做，另一半经平台转给合适的搭档，找不到就明确道歉，不硬答。

---

## 快速开始：一键部署

### 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Linux（Ubuntu 20.04+ / Debian 11+），需要 `systemd` |
| Python | 3.10+（带 `venv`） |
| 网络 | 能访问智能体互联平台（AIP）与你自己的百炼应用 |
| 其他 | `openssl`、`curl`；四个端口可对外（默认 8449 / 9021-9023，可按需改） |

### 三步

```bash
git clone https://github.com/<you>/ros2-leader-stack.git
cd ros2-leader-stack

# ① 一键部署（会问你几个必填项：平台 AIC、证书路径、百炼 API Key、接入层框架路径）
sudo bash install.sh

# ② 自检（逐项打勾，出问题会直接告诉你查哪）
sudo bash verify.sh

# ③ 跑一句试试
sudo bash verify.sh --ask "帮我做一个避障小车"
```

**`install.sh` 是幂等的**：重复执行只会补齐缺失的部分，不会覆盖你已改过的 `.env`。

> ⚠️ 本仓库**不含任何密钥与证书**。`*.env.example` 是模板，真实 `.env`、`*.pem`、`*.key`
> 都在 `.gitignore` 里 —— 请在自己的机器上生成/填写。

### 只需要 Leader（不接平台）

只想在本地玩编排层、不注册到平台？可以跳过接入层：

```bash
cd leader
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 填 DASHSCOPE_API_KEY（直连百炼模式）
python leader.py          # 监听 9200（HOST/PORT 可用环境变量覆盖）
```

然后 `curl` 打 `POST /leader`，body `{"text":"帮我做一个避障小车"}`。

---

## 注册到平台前，必改这 5 个占位符

仓库里所有敏感值都已替换成占位符（真实 IP / AIC / 令牌 / AppID **不在仓库里**），
**照着改完才跑得起来**：

| 占位符 | 出现在哪 | 换成什么 |
|---|---|---|
| `YOUR_HOST` | `partners/*/acs.json` 的 `endPoints[].url` | 你的公网 IP 或域名 |
| `1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD` | `partners/leader_orchestrator/acs.json`、gateway 的 `.env.example` 等 | 你在平台上申请到的 AIC。①②③ 的 `acs.json` 里 `aic` 是**空字符串**——直接提交，平台会按账号分配并写回（实测可用）；只有 `leader_orchestrator` 那份可以先留占位符替换 |
| `your-bailian-app-id` | `adapters/bailian-shim/.env`、`leader/.env` | 百炼应用的 `app_id` |
| `sk-your-dashscope-key` | 各 `.env` | 百炼 API Key |
| `change-me-gateway-token` / `AUTH_TOKEN` | gateway、leader 的 `.env` | 自己生成：`openssl rand -hex 24` |

```bash
# 一条命令找出全部占位符
grep -rn "YOUR_HOST\|AAAAAA\|your-bailian-app-id\|sk-your-dashscope-key\|change-me" \
     --include='*.json' --include='*.env*' --include='*.toml' --include='*.md' .
```

> **AIC 与证书是绑定的**：一个 AIC 只能配一张 `server.pem`。所以 `certs/` 下按 agent 分目录，
> 四个 agent 就是四张证书 —— 别想着复用。

---

## 目录结构

```
ros2-leader-stack/
├── README.md                  ← 你正在看的
├── LICENSE                    ← MIT
├── install.sh                 ← ★ 一键部署
├── verify.sh                  ← ★ 一键自检 + 试跑
│
├── leader/                    ← 【编排层核心】规则表 / 状态机 / 渲染
│   ├── leader.py              ← 约 1700 行，全流程都在这
│   ├── requirements.txt
│   ├── .env.example
│   └── leader.service         ← systemd 单元
│
├── partners/                  ← 【三个能力 partner】+ 对外身份
│   ├── ros2_analyze/          ← ① 需求分析（:9021）
│   │   ├── acs.json           ← 平台注册用的"简历"
│   │   ├── config.toml        ← 端口 / 超时 / LLM 选择
│   │   ├── prompts.toml       ← 接入层三段式提示词
│   │   ├── .env.example
│   │   ├── run.sh             ← ★ 可执行：启动该 partner 的接入层
│   │   └── CONTRACT.md        ← ★ 输入/输出契约（在百炼复现工作流用这个）
│   ├── ros2_plan/             ← ② 项目规划（:9022）
│   ├── ros2_codegen/          ← ③ 代码生成（:9023）
│   └── leader_orchestrator/   ← 平台上的那个智能体（:8449）
│
├── adapters/                  ← 【适配器三件套】
│   ├── gateway/               ← 普通 HTTP → AIP + mTLS（:9100）
│   ├── bailian-shim/          ← OpenAI 格式 ↔ 百炼工作流（:9099，仅本机）
│   └── amp-forwarder/         ← 心跳/遥测 → 平台 Kafka
│
├── framework/                 ← ACPs 接入层框架的补丁（必打！见下）
│   ├── README.md
│   └── patches/
│
├── deploy/                    ← 参考用的更细的部署脚本（老版本，保留备查）
├── docs/                      ← 架构说明书、数据流、②③契约规范
└── extra/                     ← 早期方案留档（主 agent 直接调 Leader，已被平台方案取代）
```

---

## 三个 Partner 是什么

三个 partner 都是**"百炼工作流 + 一个接入层壳"**的组合：

```
接入层（本仓库 partners/<name>/）          百炼工作流（你在百炼上建）
  · 收 AIP 请求、验证书                      · 真正的提示词与模型
  · 三段式：决策 → 分析 → 产出               · 输出必须带标记段（见 CONTRACT.md）
  · 通过 shim 转成百炼能懂的格式
```

| Partner | 输入 | 输出 | 关键约束 |
|---|---|---|---|
| **① ros2_analyze** | 学生原话 | 一个 JSON：`{valid, need_ask, data:{project, modules:[…]}}` | 不合格就 `valid:false`；信息不足就 `need_ask:true` |
| **② ros2_plan** | ① 的 JSON | 三段拼接：学习指南正文 + `<!--NODE_LIST_START-->…` + `<!--PLAN_JSON_START-->…` | 两个标记段**必须成对**；节点必须有 `node_name` |
| **③ ros2_codegen** | 单个节点规格 JSON | `<!--ENV_START-->{"valid","code","file_name","teaching"}<!--ENV_END-->` | 代码要能通过 AST 检查，最多自修 3 轮 |

**每个 partner 目录下的 `CONTRACT.md` 是复现工作流的完整契约**（输入字段、标记段格式、真实样例、常见错误）。
你可以在百炼上按它建一个工作流，然后把 `config.toml` 里的 `app_id` 换掉即可。

**可执行入口 `run.sh`**：ACPs 框架一个进程能托管多个 agent，所以三个 partner 共用一个框架进程。
`run.sh` 做三件事：把本目录同步到框架的 `partners/online/<name>`、首次运行从 `.env.example` 生成 `.env`、前台启动框架。

```bash
cd partners/ros2_analyze && sudo bash run.sh        # 只起这一个（调试用）
sudo bash install.sh && sudo systemctl start acps-agent   # 或者三个一起起（生产用）
```

---

## 复现三个工作流（导入 DSL，最快的一条路）

`partners/*/workflow.dsl.zip` 是从百炼导出的**原始 DSL 包**，可以直接导入，不用照着契约从零搭：

```bash
① 百炼控制台 → 工作流 → 右上角【⋯ 更多】→【导入百炼 DSL】→ 选 workflow.dsl.zip
② 导入后补三件事（见下）
③ 发布 → 发布渠道 → 复制 app_id → 填到 adapters/bailian-shim/.env 的 BAILIAN_APPS
```

| Partner | 节点数 | 用的模型 | 依赖的外部插件 |
|---|---|---|---|
| ① 需求分析 | 9 | `qwen3.8-flash` / `qwen3.8-max` | — |
| ② 项目规划 | 8 | `qwen3.7-flash` / `qwen3.8-flash` | ROS2知识库检索 |
| ③ 代码生成 | 18 | `qwen3-coder-flash` / `qwen3.7-flash` / `qwen3.8-flash` | ROS2知识库检索 |

### 导入后必须手动补的三件事

| # | 事项 | 为什么 |
|---|---|---|
| 1 | **重新关联知识库插件** | DSL 只存了插件 ID，换账号后那个 ID 不存在，要绑你自己的「ROS2知识库检索」 |
| 2 | **确认模型已开通** | 上面那些模型你账号里没有的话，在对应节点换成你有的（能力会略有差异） |
| 3 | **输入输出对齐契约** | 输入要吃 `PARTNER` 的 JSON，输出必须带标记段 —— 见 `partners/<name>/CONTRACT.md` |

> **要导入请用 `workflow.dsl.zip`，别用解压后的 yml** —— 百炼要求包里带 `md5.txt`，改过 md5 就失效。
> `workflow.template.yml` 只是同内容的**可读副本**（在 GitHub 上能直接看提示词和脚本节点），
> 每个目录还有一份 `WORKFLOW.md` 讲节点构成与数据流。

---

## 配置项

所有配置都是 `.env`（`KEY=VALUE`），仓库里只放 `.env.example`。

### leader/.env（最常改的几个）

| 变量 | 说明 | 示例 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 百炼 API Key（自答模式用） | `sk-xxx` |
| `AUTH_TOKEN` | Leader 的访问令牌（接入层要带） | `change-me` |
| `PLUGIN_KEY` | 兼容旧插件的鉴权（可选） | |
| `REQUIREMENT_AIC` / `PLAN_AIC` / `CODEGEN_AIC` | 三个 partner 的 AIC（走平台时必须填） | `1.2.156.…` |
| `REQUIREMENT_APP` / `PLAN_APP` / `CODEGEN_APP` | 三个百炼应用 app_id（直连百炼时用） | |
| `GATEWAY_URL` | 适配器地址 | `http://127.0.0.1:9100` |
| `LEADER_DB` | 会话库路径 | `/opt/ros2-leader-stack/leader/leader.db` |
| `MAX_CONCURRENCY` | ③ 的并发数 | `4` |

### adapters/gateway/.env

| 变量 | 说明 |
|---|---|
| `MY_AIC` | 网关自己的 AIC（调用方身份） |
| `MY_CERT` / `MY_KEY` / `CA_BUNDLE` | mTLS 客户端证书 |
| `DEFAULT_TIMEOUT_SEC` | 外层超时（默认 1200，要盖住 Leader 的整条流水线） |
| `ASK_PLATFORM_TIMEOUT_SEC` | 单搭档超时（默认 300） |

### partners/*/.env

| 变量 | 说明 |
|---|---|
| `PARTNER_LLM_*` | 接入层自己用的模型（决策/分析两段） |
| `SHIM_URL` | 百炼 shim 地址（默认 `http://127.0.0.1:9099`） |

### 端口一览

| 端口 | 服务 | 是否对外 |
|---|---|---|
| 8449 | leader_orchestrator（平台入口） | ✅ 必须 |
| 9021 / 9022 / 9023 | ① ② ③ | ✅ 必须 |
| 9200 | Leader | ❌ 仅本机 |
| 9100 | gateway | ❌ 仅本机 |
| 9099 | shim | ❌ 仅本机 |

> **两层防火墙都要开**：云厂商安全组 + 本机 `ufw`。我们在这里卡了整整两天（见"踩过的坑"）。

---

## 一键部署脚本做了什么

`install.sh` 按顺序做 7 件事，任何一步失败都会打印原因并停下（不留半成品）：

1. **检查环境**：root、python3 ≥ 3.10、venv、openssl、curl、systemd
2. **建目录**：默认 `/opt/ros2-leader-stack`（`--prefix` 可改），把仓库内容同步过去
3. **生成 `.env`**：交互式问 4 个必填项（AIC / 证书目录 / 百炼 Key / 框架路径），写进各组件
4. **建 venv**：leader、gateway、shim、amp-forwarder 各一个，`pip install -r`
5. **装 systemd**：4 个 unit + 1 个 acps-agent unit，路径与用户自动替换
6. **打框架补丁**：`framework/patches/`（见下，**不打补丁会比死还难受**）
7. **启动 + 自检**：`systemctl enable --now`，然后调用 `verify.sh`

### 为什么必须打框架补丁

我们用的是 ACPs 社区的 demo-partner 框架，有两个行为会坑死人：

| 补丁 | 不打会发生什么 |
|---|---|
| `patch_demo_partner.py` | 框架只读**工作目录**的 `.env`，而且"找到第一个就 return" —— 每个 agent 目录的 `.env` **从来没生效**。症状：请求根本没到你的服务，被别的 agent 接走了 |
| `patch_demo_partner_rawlog.py` | 只会读 LLM 的 `choices[0].message.content`，**prompt 传空就静默产出模板**。补丁加了原始请求/响应落盘，否则永远查不出为什么输出是 `xxx 模板` |

两个补丁都是**幂等**的（已打过会提示 skip）。

---

## 验证

`verify.sh` 会逐项打勾：

```
[1/7] 服务状态         leader / acps-agent / acps-gateway / bailian-shim / amp-forwarder
[2/7] 端口监听         8449 9021 9022 9023 9200 9100 9099
[3/7] 外网可达（可选）  Test-NetConnection / nc -z
[4/7] Leader 直连        POST /leader  {"text":"什么是 ROS2 的话题？"}
[5/7] 网关 → 平台 → 8449  POST /call_partner {"aic":"…","input":"…"}
[6/7] 完整流水线         POST /leader  {"text":"帮我做一个避障小车"}
[7/7] 会话库             leader.db / journal_mode=wal / codegen 的 raw 目录
```

任何一项失败都会打印**下一步该看哪个日志**，而不是只说"失败"。

---

## 实测数据

在一台 2C4G 的云服务器上（百炼工作流为 qwen 系列）：

| 场景 | 耗时 | 输出规模 |
|---|---|---|
| 概念自答 | 4.0 s | 300–900 字 |
| 单节点代码 | 65 s | 代码 + 教学说明 |
| 完整项目（①→②→③） | 119–209 s | 约 9,600–11,000 字（规划书 + 指南 + N 份代码） |
| 双线并发（两个学生同时问） | 两个 session 各自 ok | 锁库报错 0 次（WAL 生效） |

---

## 我们踩过的坑

按"如果不知道会浪费多久"排序：

| # | 现象 | 真正的原因 |
|---|---|---|
| 1 | 外网连不上 8449/9021-9023，但本机 `openssl s_client` 能连 | **两层防火墙都没放行**：安全组 + `ufw`。8443 早放行了，所以一直没怀疑 |
| 2 | 请求被旧 agent 接走，Leader 日志里一个字都没有 | 框架只读工作目录 `.env` 且"首个即返回"，agent 目录的 `.env` 从未生效 |
| 3 | Leader 返回 401 | 框架只发 `Authorization: Bearer`，Leader 只认 `X-Plugin-Key` |
| 4 | ② 输出一堆 `xxx` 模板 | 调 ②③ 时 `prompt` 传空，而直连百炼只有 `input.prompt` 这条通道可靠 |
| 5 | ② 跑到 60 秒被掐断 | 网关单搭档 60 秒 / 总预算 90 秒，而 ② 要 60–70 秒 |
| 6 | 整条流水线 406 秒后 `Request timed out` | 接入层调 Leader 的 HTTP 客户端只有 120 秒（还带 SDK 重试） |
| 7 | 同一条规划书刷屏 3–4 遍 | 前端会把"上一轮候选结果"整段塞进下一轮输入，被规则表误判成新项目 → 已加**剥包装** |

---

## 已知限制

| 限制 | 说明 | 想改怎么改 |
|---|---|---|
| 平台侧**每轮都是新会话** | 接入层没传 `session_id`，Leader 只能自己生成 `plat-xxxx` | 让接入层把平台会话 id 传下来 |
| 只做 **Python**，不产 C++ | ③ 的提示词只训练/约束在 rclpy | 给 ③ 加 C++ 分支（工作量较大） |
| **非流式** | ACS 里 `capabilities.streaming = false`；Leader 与 ③ 都是同步等结果 | 改 `streaming: true` + Leader 支持逐段吐字 |
| 完整项目要 2–3 分钟 | 三个工作流各 60 秒量级，串行为主 | ② 与 ③ 更难并行（③ 依赖 ② 的节点清单）；可先只生成 2 个节点再"继续" |
| 百炼应用**需要自建** | 本仓库不含工作流本体（只有契约） | 按 `partners/*/CONTRACT.md` 在百炼上建，再改 `app_id` |

---

## 安全提醒

- 仓库**不含**任何密钥、证书、私钥、数据库；`.env`、`*.pem`、`*.key`、`*.db`、`raw/` 都在 `.gitignore` 里
- `9200 / 9100 / 9099` **不要**对公网开放，只给本机
- 对外只开 `8449 / 9021-9023`，并配好 mTLS（平台侧会校验证书链与 AIC 一致性）
- 备份 `leader.db` 时记得连 `leader.db-wal` 一起拷，或先 `PRAGMA wal_checkpoint(TRUNCATE)`

---

## 致谢

- 接入层与 AIP/ACPs 协议：[ACPs Community](https://github.com/acps-community) 的 demo-partner 框架
- 平台与协议文档：智能体互联平台（AIP v2.2）
- 三个工作流与本文档的输入输出契约：由项目组同学在百炼上实现

---

## License

[MIT](LICENSE) —— 随便用，出问题别找我（真的，这是课程项目，不是生产级产品）。
