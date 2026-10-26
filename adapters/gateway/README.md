# ACPs 出站网关（acps-gateway）

## 它解决什么问题

百炼那边的插件 / HTTP 节点只会发**普通 HTTP 请求**，不会 mTLS、不会 AIP。

而梧桐平台上的 Partner 要求：**客户端证书 + JSON-RPC 2.0 的 AIP 报文 + 异步轮询**。

本服务站在中间：

```
百炼插件/HTTP节点 ──HTTP(JSON)──→ 本服务 ──mTLS + AIP──→ 梧桐 Partner
                                     ↑
                          客户端证书在这里被使用，从不离开服务器
```

---

## 接口一览

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET/POST | `/ping` | ❌ 免 | 连通性测试，百炼插件建好后先用它验证 |
| GET | `/health` | ❌ 免 | 健康检查（证书在不在、配置了几个搭档） |
| POST | `/wf/<搭档名>` | ✅ | **百炼主要用这个**。Body 直接放结构化字段 |
| POST | `/invoke` | ✅ | 通用调用，自己指定 endpoint / skill |
| POST | `/discover` | ✅ | 按需求查平台上有哪些智能体可用 |

**鉴权**：除 `/ping` `/health` 外，请求头必须带 `X-Auth-Token: <AUTH_TOKEN>`。

### 调用示例

```bash
# 连通性测试
curl http://127.0.0.1:9100/ping

# 调「代码生成」搭档（对百炼最友好的形式）
curl -X POST http://127.0.0.1:9100/wf/ros2_codegen \
  -H 'X-Auth-Token: 你的token' \
  -H 'Content-Type: application/json' \
  -d '{"input":"帮我写一个发布 hello world 字符串的 ROS2 节点"}'

# 也可以直接传结构化字段
curl -X POST http://127.0.0.1:9100/wf/ros2_codegen \
  -H 'X-Auth-Token: 你的token' \
  -H 'Content-Type: application/json' \
  -d '{
        "node_name": "hello_world_publisher",
        "class_name": "HelloWorldPublisher",
        "what_to_do": "每秒向 /chatter 发布 hello world 字符串",
        "publish_topics": [{"name": "/chatter", "msg_type": "std_msgs/msg/String"}],
        "subscribe_topics": []
      }'

# 查平台上有哪些智能体能做这件事
curl -X POST http://127.0.0.1:9100/discover \
  -H 'X-Auth-Token: 你的token' \
  -H 'Content-Type: application/json' \
  -d '{"need":"帮我做一个避障小车项目","limit":5}'
```

**返回格式**：

```json
{
  "ok": true,
  "target": "ros2_codegen",
  "task_id": "gw-...",
  "state": "awaiting-completion",
  "text": "```python\n...\n```",
  "elapsed_sec": 6.24
}
```

---

## 部署

### 1. 上传

```powershell
# 本地 PowerShell
scp -r C:\Users\mike\Documents\deepseek-harness\default-workspace\wutong-agent\acps-gateway root@YOUR_HOST:/opt/acps-agent/
```

### 2. 建环境

```bash
cd /opt/acps-agent/acps-gateway
PYBIN=$(cat /opt/acps-agent/.python-path)
"$PYBIN" -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

cp .env.example .env
# targets.json 已经在包里了，不用改（以后加搭档时才改）
```

### 3. 生成一个真正的密钥填进 .env

```bash
cd /opt/acps-agent/acps-gateway
TOKEN=$(openssl rand -hex 24)
sed -i "s|^AUTH_TOKEN=.*|AUTH_TOKEN=$TOKEN|" .env
chmod 600 .env
echo "================================================"
echo "请把这个 token 记下来，百炼插件里要用："
echo "$TOKEN"
echo "================================================"
grep -E '^(AUTH_TOKEN|MY_AIC|GATEWAY_PORT)=' .env
```

### 4. ⚠️ 放行 9100 端口（两处都要做）

**① 主机防火墙（黑框）**

```bash
ufw allow 9100/tcp
ufw status | grep 9100
```

**② 阿里云安全组（管控台网页）**

```
云服务器 ECS → 安全组 sg-7xv0lywysp5j6p180tlk → 配置规则 → 入方向 → 手动添加
    协议类型：自定义 TCP
    端口范围：9100/9100
    授权对象：0.0.0.0/0
    描述：ACPs 出站网关（百炼插件调用）
```

**这两步缺一不可。** 之前 8443 就是漏了主机防火墙，卡了很久。

### 5. 手工试跑

```bash
cd /opt/acps-agent/acps-gateway
./venv/bin/python gateway.py
```

另开一个窗口：

```bash
# 本机
curl -s http://127.0.0.1:9100/ping | python3 -m json.tool

# 公网（这条通了，百炼才能通）
curl -s http://YOUR_HOST:9100/ping | python3 -m json.tool
```

**预期返回**：

```json
{
  "ok": true,
  "service": "acps-gateway",
  "my_aic": "1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD",
  "targets": ["ros2_analyze", "ros2_codegen", "ros2_plan"]
}
```

### 6. 装成常驻服务

```bash
cd /opt/acps-agent/acps-gateway
cp acps-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now acps-gateway
sleep 5
systemctl is-active acps-gateway
journalctl -u acps-gateway -n 20 --no-pager
```

### 7. 试一次真实出站调用（这是关键验证）

```bash
cd /opt/acps-agent/acps-gateway
TOKEN=$(grep '^AUTH_TOKEN=' .env | cut -d= -f2)

curl -s -X POST http://127.0.0.1:9100/wf/ros2_codegen \
  -H "X-Auth-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"input":"帮我写一个发布 hello world 字符串的 ROS2 节点"}' \
  | python3 -m json.tool | head -40
```

**这条返回 `"ok": true` 且 `text` 里有 Python 代码 → 出站链路完全打通。**

---

## 搭档配置（targets.json）

```json
{
  "ros2_codegen": {
    "aic": "1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD",
    "endpoint": "https://YOUR_HOST:8443/rpc",
    "skill": "ros2-assistant.single-node-generator",
    "timeout_sec": 180
  }
}
```

**现在三个名字都指向同一个智能体**（因为目前只注册了这一个）。

**以后把工作流拆开、分别注册成搭档，只要改这里的 `aic` 和 `endpoint` 即可，网关代码一行不用动。**

改完 `targets.json` 后重启：

```bash
systemctl restart acps-gateway
```

---

## 排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 百炼报连接超时 | 端口没放行 | 检查 ufw **和** 阿里云安全组 |
| `401 X-Auth-Token 不正确` | Header 没配或值不对 | 检查百炼插件的 Header 配置 |
| `404 未配置搭档` | 名字写错 | 看 `/ping` 返回的 `targets` 列表 |
| `502` + 证书错误 | 客户端证书路径不对 | 看 `/health` 的 `cert_ok` |
| `ok:false` + 超时 | 上游工作流太慢 | 看 `elapsed_sec`，或调大 `timeout_sec` |
| 出站调用失败但百炼无感 | — | `journalctl -u acps-gateway -f` 实时看 |

**实时看日志：**

```bash
journalctl -u acps-gateway -f
```

---

## 安全提醒

1. **`.env` 权限必须是 600**（里面有客户端私钥路径和鉴权 token）
2. **`AUTH_TOKEN` 不要用默认值**，一定要换成随机串
3. **私钥永远不要复制到百炼或任何第三方**
4. 如果担心被扫，可以在安全组里**只允许百炼的出口 IP 访问 9100**（需要先查到百炼的出口 IP 段）

---

## 后续可以加的东西

| 想加什么 | 说明 |
|---|---|
| **循环调用防护** | 不把自己/上游当候选，限制嵌套深度（多级协作时必需） |
| **调用日志落盘** | 每次出站调用记一条（谁、调谁、耗时、成功否） |
| **并行调用** | `POST /wf_many` 一次收 N 个任务，内部并发调 N 个搭档 |
| **结果缓存** | 同样的输入短时间内不重复调（省钱） |
| **HTTPS** | 如果将来百炼要求 https，买个域名 + 免费证书即可 |
