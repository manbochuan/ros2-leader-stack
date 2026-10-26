# 梧桐 ACPs 适配器 · 部署手册

> 目标：把阿里云百炼的工作流，包装成一个能在[梧桐智能体互联平台](https://wt.ioa.pub/)注册的 ACPs 智能体。
>
> 架构：`梧桐 Leader →(mTLS + AIP/JSON-RPC)→ 本服务 →(HTTPS + API-KEY)→ 百炼工作流`

---

## 阶段 0 · 现在就能做的三件事（在阿里云控制台）

### 1. 记下公网 IP
控制台 → 实例列表 → 找到你的实例 → 复制**公网 IP**。

### 2. 重置 root 密码
实例详情 → 「重置密码」→ 设置一个强密码 → **必须重启一次才生效**。

> 顺手在实例详情里确认一下：地域是**华北2（北京）**，镜像是 **Ubuntu 24.04**。

### 3. 放行端口

| 你的产品 | 入口名字 | 位置 |
|---|---|---|
| 轻量应用服务器 | **防火墙** | 实例详情 → 防火墙 → 添加规则 |
| 云服务器 ECS | **安全组** | 实例详情 → 安全组 → 配置规则 → 入方向 |

需要放行：

| 端口 | 协议 | 用途 |
|---|---|---|
| 22 | TCP | SSH 登录 |
| 8443 | TCP | 梧桐平台调用你的服务（mTLS） |

**别一次全开。** 只要这两个。以后需要再加。

> 常见坑：服务明明在跑，外面连不上 —— 99% 是这里没放行。

---

## 阶段 1 · 登录服务器

两个方式，任选：

**A. 网页终端（第一次用这个）**
控制台 → 实例详情 → 「远程连接」→ Workbench → 网页里出现黑框终端 → 输 root 密码。

**B. 本地 PowerShell（Windows 自带，不用装东西）**
```powershell
ssh root@你的公网IP
```
第一次会问 `yes/no`，输 `yes`。输密码时**屏幕上不显示任何字符**，这是正常的。

> 推荐后续装 **VS Code + Remote-SSH 插件**，连上服务器直接写代码、开终端、传文件，一个窗口搞定。

---

## 阶段 2 · 跑环境准备脚本

**把 `bootstrap.sh` 传到服务器。** 在本文件所在目录打开 PowerShell：

```powershell
scp .\bootstrap.sh root@你的公网IP:/root/
```

（或者用 FinalShell / WinSCP 直接拖进去。）

**然后在服务器上执行：**

```bash
sudo bash /root/bootstrap.sh
```

脚本做这些事，可重复执行：

1. 系统更新 + 装 `curl git tmux build-essential libssl-dev chrony jq`
2. 设时区为 `Asia/Shanghai` + 开启时间同步（**mTLS 对时钟敏感，这步不能省**）
3. 没 swap 就建 2G swap（防 `pip install` 内存峰值被 OOM Killer 杀掉）
4. 建专用服务账号 `acps`
5. 装 uv + Python 3.14（装到 `/opt/uv-python`，不装在 HOME 下）
6. 建目录 `/opt/acps-agent/{config,certs,logs,data}`，`certs/` 权限 700

跑完会打印 Python 解释器的绝对路径，**记下来，后面建 venv 要用**。

---

## 阶段 3 · 验证环境

```bash
# 1. Python 版本
/opt/uv-python/cpython-3.14*/bin/python3.14 --version     # 或直接 cat /opt/acps-agent/.python-path

# 2. 时钟是否同步（偏差应为毫秒级）
chronyc tracking | head -5

# 3. swap 是否生效
free -h

# 4. 出网是否通（三个都必须通）
curl -o /dev/null -s -w 'wutong   %{http_code}  connect=%{time_connect}s\n' https://wt.ioa.pub/
curl -o /dev/null -s -w 'bailian  %{http_code}  connect=%{time_connect}s\n' https://dashscope.aliyuncs.com/
curl -o /dev/null -s -w 'pypi     %{http_code}  connect=%{time_connect}s\n' https://pypi.org/simple/

# 5. AMP 日志上报端口是否通（Kafka，注意不是 443）
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/wt.ioa.pub/19092' && echo 'AMP 19092 OK' || echo 'AMP 19092 不通'
```

`connect` 时间应该在 **0.05s 以内**（北京同城约 0.01s）。如果超过，说明地域选偏了。

---

## 阶段 4 · 下一步（还没做，等代码就绪）

- [ ] 在 https://wt.ioa.pub/ 注册并登录梧桐账号
- [ ] 在[注册平台](https://wt.ioa.pub/registry/)看一眼 ACS 要填哪些字段（尤其**服务入口**）
- [ ] 拉 ACPs v2.2.0 的 `demo-partner` 参考实现
- [ ] 写适配器：AIP 路由 / 任务状态机 / 百炼翻译层 / mTLS / AMP 日志
- [ ] 生成 `acs.json` → 提交注册 → 审核 → 拿 **AIC**
- [ ] 用 `acps-cli` 取 EAB、申请 mTLS 证书
- [ ] 证书放进 `/opt/acps-agent/certs/`，装 systemd 服务

---

## 阶段 5 · 回填给我这些信息

部署脚本的后续部分需要这些，**别把 API-KEY 发给我**：

| # | 信息 | 怎么拿 |
|---|---|---|
| 1 | 公网 IP | 控制台实例列表 |
| 2 | 是轻量还是 ECS | 控制台产品名 |
| 3 | 系统版本 | 服务器上跑 `cat /etc/os-release \| head -2` |
| 4 | Python 解释器绝对路径 | `cat /opt/acps-agent/.python-path` |
| 5 | 百炼工作流的 **app_id** | 百炼控制台 → 应用 → 应用ID |
| 6 | bootstrap 脚本的输出 | 整个贴给我，含报错 |
| 7 | 梧桐账号注册好了吗 | — |

有了 1–4 我就能把后续的部署脚本（venv、依赖、`.env`、systemd unit、端口自检）一次性写全。

---

## 排障顺序（记住这个顺序）

```
1. 服务在跑吗      systemctl status acps-agent
2. 端口在听吗      ss -lntp | grep 8443
3. 云平台放行了吗  轻量→防火墙 / ECS→安全组
4. 本机通吗        curl -k https://127.0.0.1:8443/health
5. 外网通吗        curl -k https://你的公网IP:8443/health
6. 证书对吗        openssl s_client -connect 127.0.0.1:8443 -showcerts
```

**从里往外一层层查，别跳。** 卡在哪一层，问题就在哪一层。

---

## 附：常见报错速查

| 现象 | 原因 |
|---|---|
| `Permission denied (publickey)` | 密码没重置，或重置后没重启实例 |
| `Connection timed out` | 22 端口没放行，或 IP 填错 |
| 输入密码时没有任何显示 | 正常，Linux 不回显密码 |
| `pip install` 中途被杀 | 内存不足，确认 swap 已生效 |
| mTLS 握手失败 | 时钟漂移，跑 `chronyc makestep` |
| 服务起不来，报 pydantic `Field required` | 进程的工作目录不是安装根目录，读不到 `.env` |
