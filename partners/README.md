# partners/ —— 三个能力 + 一个对外身份

四个目录结构完全一样，区别只有 **名字 / 端口 / default 档指向谁**：

| 目录 | 角色 | 端口 | default 档指向 | AIC |
|---|---|---|---|---|
| `ros2_analyze/` | ① 需求分析 | 9021 | shim（→ 百炼工作流 ①） | 需自备 |
| `ros2_plan/` | ② 项目规划 | 9022 | shim（→ 百炼工作流 ②） | 需自备 |
| `ros2_codegen/` | ③ 代码生成 | 9023 | shim（→ 百炼工作流 ③） | 需自备 |
| `leader_orchestrator/` | 平台上的"这个智能体" | 8449 | **Leader（:9200）** | 需自备 |

## 每个目录里有什么

| 文件 | 作用 |
|---|---|
| `acs.json` | **平台注册用的简历**：AIC、技能、endPoint、证书要求。注册后平台按它匹配 |
| `config.toml` | 端口、TLS 开关、并发、LLM 两档（fast / default） |
| `prompts.toml` | 接入层三段式提示词：`decision → analysis → production` |
| `.env.example` | 环境变量模板（LLM key、shim 地址、证书目录） |
| `run.sh` | **可执行入口**：同步到框架 + 前台启动这一个 agent |
| `CONTRACT.md` | ①②③ 的**输入/输出契约**（从零建工作流用这个） |
| **`workflow.dsl.zip`** | 从百炼导出的**原始 DSL 包**，可以直接【导入百炼 DSL】 |
| **`WORKFLOW.md`** | 该工作流的节点构成、数据流、导入后要补什么 |
| `workflow.template.yml` | DSL 的可读副本（GitHub 上直接看提示词与脚本节点） |

## 怎么跑起来

**方式一：只起一个（调试推荐）**

```bash
cd partners/ros2_analyze
cp .env.example .env && vi .env        # 填 LLM key
bash run.sh                            # 第一次会生成 .env 后退出，再执行一次即可
```

**方式二：四个一起（生产推荐）**

```bash
sudo bash ../../install.sh             # 会装 systemd 并启动 acps-agent（托管全部 agent）
sudo systemctl status acps-agent
```

## 一句话记住谁是"门卫"谁是"能力"

```
8449 leader_orchestrator  ← 平台上的"智能体"，AIC 挂在它身上，但它只会转给 Leader
9021/9022/9023            ← 真正干活的三个能力，每个有自己的 AIC 和证书
```

**→ 平台说"ROS2 学习编排智能体在线"，指的是 8449 那个壳；Leader 在它后面，平台并不知道 9200 存在。**

---

## ⚠️ 关于 `acs.json` 里的 `aic`

| 文件 | `aic` 现在是什么 | 注册时怎么做 |
|---|---|---|
| `ros2_analyze` / `ros2_plan` / `ros2_codegen` | **空字符串 `""`** | 直接提交，平台按你的账号**分配** AIC 并写回 |
| `leader_orchestrator` | `1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD`（占位符） | 换成你自己的（或清空让平台分配） |

> 空字符串是**实测可用**的（平台接受），占位符只是让你看清楚格式。
> **如果你的平台版本对格式校验严格，优先用空字符串。**

`endPoints[].url` 里的 `YOUR_HOST` 也别忘了换成你的公网 IP 或域名。
