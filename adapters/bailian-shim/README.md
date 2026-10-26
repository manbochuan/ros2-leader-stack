# 百炼转换层（bailian-shim）部署说明

## 它解决什么问题

梧桐要求智能体实现 ACPs/AIP 协议（mTLS、任务状态机、AMP 日志上报…），
百炼工作流只会"一问一答"。中间必须有一层适配。

本目录是**适配链路的第二段**：

```
梧桐平台 ──mTLS/AIP──> demo-partner ──OpenAI格式──> bailian-shim ──百炼格式──> 百炼工作流
                          （官方代码，不改）        （本目录）              （你的工作流）
```

因为 demo-partner 的 LLM 地址是**环境变量注入**的，所以把它的
`PARTNER_LLM_DEFAULT_BASE_URL` 指向本服务即可，**demo-partner 一行代码都不用改**。

---

## 一、上传并安装

在**你本地**执行（把整个 bailian-shim 目录传到服务器）：

```powershell
scp -r .\bailian-shim root@你的公网IP:/opt/acps-agent/
```

在**服务器上**执行：

```bash
cd /opt/acps-agent/bailian-shim

# 用 bootstrap.sh 装好的 Python 3.14 建 venv（注意必须用 acps 账号建）
PYBIN=$(cat /opt/acps-agent/.python-path)
sudo -u acps "$PYBIN" -m venv venv
sudo -u acps ./venv/bin/pip install -U pip
sudo -u acps ./venv/bin/pip install -r requirements.txt

# 配置
sudo -u acps cp .env.example .env
chmod 600 .env
sudo chown acps:acps .env
nano .env        # 填 DASHSCOPE_API_KEY，其余按注释确认
```

**`.env` 里必须确认的两项：**

| 变量 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` | 百炼 API-KEY |
| `BAILIAN_INPUT_FIELD` | **工作流入参字段名**。默认 `prompt`；如果你在工作流「开始」节点里把变量命名成别的（`query`/`question`…），必须改成一样的 |

> `BAILIAN_APP_ID` 已经预填为 `your-bailian-app-id`。

## 二、先手工跑一次（重要，别跳过）

```bash
cd /opt/acps-agent/bailian-shim
sudo -u acps ./venv/bin/uvicorn shim:app --host 127.0.0.1 --port 9099
```

**另开一个终端**，测三个命令：

```bash
# 1. 配置是否完整
curl -s http://127.0.0.1:9099/health | jq

# 2. 模型列表
curl -s http://127.0.0.1:9099/v1/models | jq -r '.data[0].id'

# 3. 真正打一次百炼（最关键）
curl -s http://127.0.0.1:9099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-local-shim' \
  -d '{"model":"bailian-workflow","messages":[{"role":"user","content":"写一个 ROS 2 的 Python 发布者节点"}]}' \
  | jq -r '.choices[0].message.content'
```

**第 3 条能打印出一段 ROS 代码，就说明链路通了。**

如果报错，对照下表：

| 报错 | 原因 |
|---|---|
| `InvalidApiKey` / 401 | `DASHSCOPE_API_KEY` 填错 |
| `Model not exist` / `App not exist` | `BAILIAN_APP_ID` 不对 |
| 参数错误、缺少变量 | `BAILIAN_INPUT_FIELD` 和工作流入参名不一致 |
| 超时 | 工作流跑太久，调大 `BAILIAN_TIMEOUT` |

手工测试通过后按 `Ctrl+C` 停掉。

## 三、装成常驻服务

```bash
cp bailian-shim.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bailian-shim
systemctl status bailian-shim --no-pager

# 看日志
journalctl -u bailian-shim -f
```

## 四、接上 demo-partner

在 demo-partner 的 `.env` 里设置：

```bash
PARTNER_LLM_DEFAULT_API_KEY=sk-local-shim
PARTNER_LLM_DEFAULT_BASE_URL=http://127.0.0.1:9099/v1
PARTNER_LLM_DEFAULT_MODEL=bailian-workflow

# fast 档可以指同一个（你的工作流只有一个）
PARTNER_LLM_FAST_API_KEY=sk-local-shim
PARTNER_LLM_FAST_BASE_URL=http://127.0.0.1:9099/v1
PARTNER_LLM_FAST_MODEL=bailian-workflow
```

启动 demo-partner，它的所有大模型调用就都走你的百炼工作流了。

---

## 五、可调开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `MESSAGE_MODE` | `last_user` | `all` 会把整段对话拼给工作流（通常不需要） |
| `USE_SESSION` | `0` | 设 `1` 时把 OpenAI 的 `user` 字段当百炼 `session_id`，启用云端多轮 |
| `LOG_LEVEL` | `INFO` | 排查问题时改 `DEBUG` |
| `SHIM_API_KEY` | `sk-local-shim` | 本层自己的鉴权，改了两边要同步 |

---

## 六、排障顺序

```
1. 服务在跑吗      systemctl status bailian-shim
2. 端口在听吗      ss -lntp | grep 9099
3. 配置完整吗      curl -s http://127.0.0.1:9099/health | jq
4. 能打通百炼吗    手工 curl 第 3 条
5. 看调用日志      journalctl -u bailian-shim -n 100 --no-pager
```

日志里每次成功调用都会打一行 `ok rid=... prompt_chars=... out_chars=... 1.23s`，
凭这一行就能判断是"没收到请求"还是"百炼返回慢"。
