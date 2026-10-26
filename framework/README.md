# 接入层框架（ACPs demo-partner）—— 补丁与约定

本项目的三个 partner 和对外身份**都不是自己写的服务**，而是用 ACPs 社区的
`demo-partner` 框架托管：一个 agent 一个目录，框架负责 AIP 协议、mTLS、心跳。

> **本仓库不包含框架本体**。请自行获取 ACPs Community 的 `demo-partner`（AIP v2.2），
> 装好后把路径告诉 `install.sh`（或用 `FRAMEWORK_DIR` 指定）。

---

## 一、框架的目录约定

```
demo-partner/
├── .venv/                      ← 框架自己的虚拟环境（我们所有脚本都用它跑）
├── .env                        ← 框架级 .env（⚠️ 见下方"补丁 1"）
└── partners/
    └── online/
        ├── ros2_analyze/       ← 我们放进去的（可由 run.sh 自动同步）
        │   ├── acs.json        ← 平台注册用的"简历"（含 AIC / 端口 / 技能）
        │   ├── config.toml     ← 端口、TLS 开关、LLM 档位
        │   ├── prompts.toml     ← 三段式提示词：decision → analysis → production
        │   ├── .env            ← 本 agent 的环境变量（LLM key / shim 地址）
        │   ├── server.pem      ← 本 agent 的证书（AIC 不同 → 证书不同！）
        │   ├── server.key
        │   └── trust-bundle.pem
        ├── ros2_plan/
        ├── ros2_codegen/
        └── leader_orchestrator/
```

启动单个 agent：

```bash
cd demo-partner
.venv/bin/python -m partners.main ros2_analyze     # 只起 ①
```

---

## 二、必须打的两个补丁

放在 `patches/`，`install.sh` 会自动执行（幂等，重复跑只会提示 skip）。

### 补丁 1 · `patch_demo_partner.py` —— 让 agent 目录的 `.env` 真正生效

**框架原生行为：** 只从**工作目录**向上找 `.env`，而且**找到第一个就 return**。

**后果：** `partners/online/<agent>/.env` 里的配置**从来没被读过**。
你以为配的是 ①，实际生效的是框架根目录的 `.env` ——
症状是"请求根本没到我预期的服务"（我们当时以为是端口放行问题，查了两天）。

**补丁做的事：** 加载完父级 `.env` 后，再用 `override=True` 加载 agent 目录的 `.env`。

**验证生效：**
```bash
journalctl -u acps-agent -n 50 --no-pager | grep -i 'env'
# 或直接看日志里 agent 连的 base_url 是不是你配的那个
```

### 补丁 2 · `patch_demo_partner_rawlog.py` —— 原始请求/响应落盘

**框架原生行为：** 只读 LLM 响应的 `choices[0].message.content`。

**后果：** prompt 传空时模型会"照模板编"，你只看到一堆 `xxx` 模板，
**却看不到到底发了什么** —— 没有原始日志，这类问题基本查不出来。

**补丁做的事：** 把每次调用的原始请求与响应写到文件里。

**日志位置：** 框架目录下的 `rawlog/`（补丁里有常量可改）。

---

## 三、升级框架时要注意

1. 先 `git stash` 你自己的改动，或直接在干净副本上升级
2. 升级后**重跑两个补丁**（幂等，已打过会 skip）
3. 重点回归三件事：
   - agent 目录 `.env` 是否生效（补丁 1）
   - ②③ 的 `prompt` 是否真的传下去了（补丁 2 + 落盘文件）
   - mTLS 是否正常（`client-cert=required` 出现在启动日志里）

---

## 四、常见坑

| 现象 | 原因 |
|---|---|
| 服务起来了，但请求被别人接走 | 补丁 1 没打，agent 的 `.env` 没生效 |
| `401 Unauthorized`（我们自己那层） | 框架只发 `Authorization: Bearer`，下游只认自定义头 —— 让下游两种都接受 |
| 启动即失败，日志里 cert 相关报错 | `server.pem` 与 ACS 里的 AIC 不匹配（每个 AIC 一张证书） |
| 平台显示离线 | 心跳没发出去（`amp-forwarder` 没起，或 Kafka 端口不通） |
