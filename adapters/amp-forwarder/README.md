# AMP 日志转发器

## 为什么需要它

平台靠**心跳**判断智能体是否在线。官方手册原文：

> **心跳一停，别人按「只要在线」去发现时就找不到你，即使业务进程还在。**

而 ACPs 的架构是这样的：

```
[写] Agent：Emitter → 本地 amp_*.jsonl 文件      ← 我们这边一直在写 ✅
        ↓  Forwarder 尾追转发                     ← ★ 这一环我们缺了
[转] Kafka 主题 amp.heartbeat / amp.metrics / ...
        ↓  Monitor Writer 入库；heartbeat 做 alive-sync
[查] 发现结果里的在线状态
```

**没有 Forwarder，日志就躺在本地，平台永远收不到心跳。**

## 它做什么

用纯 Python 复刻官方 Fluent Bit 配置的等价逻辑：

| 本地文件 | Kafka 主题 |
|---|---|
| `amp_heartbeat_*.jsonl` | `amp.heartbeat` |
| `amp_metrics_*.jsonl` | `amp.metrics` |
| `amp_access_*.jsonl` | `amp.access` |
| `amp_message_*.jsonl` | `amp.message` |
| `amp_system_*.jsonl` | `amp.system` |
| `amp_audit_*.jsonl` | `amp.audit`（额外补 `_fb_ts`，对齐官方 `timestamp_key`） |

---

## 部署

### 1. 上传并建环境

```bash
# 本地
scp -r .\amp-forwarder root@YOUR_HOST:/opt/acps-agent/

# 服务器
cd /opt/acps-agent/amp-forwarder
PYBIN=$(cat /opt/acps-agent/.python-path)
"$PYBIN" -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

cp .env.example .env
```

### 2. 先探测 Kafka 连通性（重要）

```bash
./venv/bin/python forwarder.py --probe
```

它会依次尝试 `PLAINTEXT` → `SSL` → `SASL_SSL`，并告诉你哪个能通：

```
尝试 PLAINTEXT 连接 ['wt.ioa.pub:19092'] ...
  [失败] PLAINTEXT：...
尝试 SSL 连接 ['wt.ioa.pub:19092'] ...
  [成功] 协议=SSL   amp.heartbeat 分区=[0, 1, 2]
==> 建议把 KAFKA_SECURITY_PROTOCOL 设为 SSL
```

**按提示改 `.env` 里的 `KAFKA_SECURITY_PROTOCOL`。**

### 3. 手工跑一次，看心跳有没有发出去

```bash
./venv/bin/python forwarder.py
```

**预期**（约 15 秒内）：

```
开始跟踪 amp_heartbeat_ros2_tutor.jsonl  ->  amp.heartbeat
开始跟踪 amp_metrics_ros2_tutor.jsonl    ->  amp.metrics
...
累计已发=4 失败=0   amp_heartbeat_ros2_tutor.jsonl=1  ...
```

**看到 `amp_heartbeat...=1` 就说明心跳发出去了。** 按 `Ctrl+C` 停。

### 4. 装成常驻服务

```bash
cp amp-forwarder.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now amp-forwarder
systemctl status amp-forwarder --no-pager
journalctl -u amp-forwarder -n 30 --no-pager
```

---

## 验证心跳到平台了

**等 1~2 分钟**（平台要做 alive-sync），然后：

```bash
cd /opt/acps-agent/acps-community/acps-cli
./.venv/bin/acps-cli --config acps-cli.toml discover query "ROS2 学习与代码开发" --limit 3 2>/dev/null \
  | jq '.result | keys, .result.aliveMap'
```

**如果 `aliveMap` 里出现你的 AIC（或以前没有、现在有了）→ 心跳到平台了。**

然后去叮当提问，看候选列表里有没有你。

---

## 排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Kafka 连接失败` | 协议不对 | 跑 `--probe`，按提示改协议 |
| `日志目录不存在` | `AMP_LOG_DIR` 配错 | 改成 demo-partner 的 `logs/` 目录 |
| 一直在发但没有 `累计已发` | 日志文件没更新 | 检查 `acps-agent` 是否在跑（`systemctl status acps-agent`） |
| `发送失败` | 主题没权限/不存在 | 看错误信息，把 `amp.heartbeat` 相关报错发出来 |
| 心跳发了但平台还是找不到你 | alive-sync 有延迟 | 等 1~2 分钟再看 `aliveMap` |

---

## 停用

```bash
systemctl disable --now amp-forwarder
```

如果想清掉读取进度（下次从头开始读）：

```bash
rm -rf /var/lib/amp-forwarder
```
