#!/usr/bin/env bash
# =============================================================================
#  ros2-leader-stack  ·  一键部署
#
#  用法:
#     sudo bash install.sh                     # 默认装到 /opt/ros2-leader-stack
#     sudo bash install.sh --prefix /srv/rls   # 换目录
#     sudo bash install.sh -y                  # 不交互（从环境变量取配置）
#     sudo bash install.sh --no-start          # 只装不启动
#
#  幂等：重复执行只补齐缺失部分，不覆盖已存在的 .env
# =============================================================================
set -euo pipefail

# ---------- 默认参数 ----------
PREFIX="/opt/ros2-leader-stack"
INTERACTIVE=1
DO_START=1
SKIP_VENV=0
SKIP_SYSTEMD=0

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 颜色
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_HD=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_HD=""; C_OFF=""
fi
step() { echo; echo "${C_HD}==> $*${C_OFF}"; }
ok()   { echo "  ${C_OK}✓${C_OFF} $*"; }
warn() { echo "  ${C_WARN}!${C_OFF} $*"; }
die()  { echo; echo "${C_ERR}✗ $*${C_OFF}" >&2; exit 1; }

# ---------- 解析参数 ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)        PREFIX="${2:?--prefix 需要目录}"; shift 2 ;;
    --prefix=*)      PREFIX="${1#*=}"; shift ;;
    -y|--yes)        INTERACTIVE=0; shift ;;
    --no-start)      DO_START=0; shift ;;
    --skip-venv)     SKIP_VENV=1; shift ;;
    --skip-systemd)  SKIP_SYSTEMD=1; shift ;;
    -h|--help)       sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)               die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

# ---------- 第 1 步：环境检查 ----------
step "第 1 步 / 7  检查环境"

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash install.sh"
ok "root 权限"

command -v python3 >/dev/null 2>&1 || die "没找到 python3，请先安装（apt install python3 python3-venv）"
PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "python3 版本是 $PY_VER，需要 ≥ 3.10"
ok "python3 $PY_VER"

python3 -c 'import venv' >/dev/null 2>&1 || die "缺少 venv 模块：apt install python3-venv"
ok "venv 可用"

for c in openssl curl systemctl; do
  command -v "$c" >/dev/null 2>&1 || die "缺少命令：$c"
done
ok "openssl / curl / systemctl 就位"

command -v rsync >/dev/null 2>&1 && HAS_RSYNC=1 || HAS_RSYNC=0

# ---------- 第 2 步：收集配置 ----------
step "第 2 步 / 7  收集配置"

AIC_ORCH="${AIC_ORCH:-}"
CERT_DIR="${CERT_DIR:-$PREFIX/certs}"
DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}"
FRAMEWORK_DIR="${FRAMEWORK_DIR:-/opt/acps-agent/acps-community/demo-partner}"
AUTH_TOKEN="${AUTH_TOKEN:-$(openssl rand -hex 16 2>/dev/null || echo "change-me")}"

ask() {  # ask <变量名> <提示> <默认值> <是否必填 1/0>
  local __var="$1" __prompt="$2" __default="$3" __required="$4" __answer=""
  local __cur="${!__var:-}"
  if [ -n "$__cur" ]; then return 0; fi
  if [ "$INTERACTIVE" = "0" ]; then
    if [ "$__required" = "1" ] && [ -z "$__default" ]; then
      die "非交互模式下缺少 $__var（可用环境变量传入）"
    fi
    printf -v "$__var" '%s' "$__default"
    return 0
  fi
  if [ -n "$__default" ]; then
    read -r -p "  $__prompt [$__default]: " __answer || true
    __answer="${__answer:-$__default}"
  else
    read -r -p "  $__prompt: " __answer || true
  fi
  [ -n "$__answer" ] || { [ "$__required" = "1" ] && die "$__var 不能为空"; }
  printf -v "$__var" '%s' "$__answer"
}

ask AIC_ORCH "平台上的智能体 AIC（没有就留空，稍后填）" "" 0
ask CERT_DIR "证书根目录（每个 agent 一个子目录，如 certs/ros2_analyze/server.pem）" "$CERT_DIR" 0
ask DASHSCOPE_API_KEY "百炼 API Key（sk-...，自答模式用）" "" 1
ask FRAMEWORK_DIR "ACPs demo-partner 框架目录" "$FRAMEWORK_DIR" 0

echo "  AIC          = ${AIC_ORCH:-<空>}"
echo "  CERT_DIR     = $CERT_DIR"
echo "  FRAMEWORK    = $FRAMEWORK_DIR"
echo "  PREFIX       = $PREFIX"

# ---------- 第 3 步：同步文件 ----------
step "第 3 步 / 7  同步文件到 $PREFIX"

mkdir -p "$PREFIX"
if [ "$HAS_RSYNC" = "1" ]; then
  rsync -a --delete \
    --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.venv' --exclude '*.db' --exclude '.env' \
    "$REPO_DIR"/ "$PREFIX"/
else
  cp -a "$REPO_DIR"/. "$PREFIX"/
  find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
mkdir -p "$PREFIX/leader" "$CERT_DIR"
ok "代码已同步"

# 证书：每个 agent 的 AIC 不同 → 证书不能混用，按 agent 分目录
mkdir -p "$CERT_DIR"
for p in ros2_analyze ros2_plan ros2_codegen leader_orchestrator; do
  src="$CERT_DIR/$p"
  dst="$PREFIX/partners/$p"
  [ -d "$dst" ] || continue
  if [ -f "$src/server.pem" ] && [ -f "$src/server.key" ]; then
    cp -f "$src/server.pem" "$src/server.key" "$dst/" 2>/dev/null || true
    if [ -f "$src/trust-bundle.pem" ]; then cp -f "$src/trust-bundle.pem" "$dst/" 2>/dev/null || true
    elif [ -f "$CERT_DIR/trust-bundle.pem" ]; then cp -f "$CERT_DIR/trust-bundle.pem" "$dst/" 2>/dev/null || true
    fi
    chmod 600 "$dst/server.key" 2>/dev/null || true
    ok "$p 证书已就位"
  else
    warn "$p 缺少证书（$src/server.pem + server.key）—— 该 agent 起不来；用 acps-cli 按它的 AIC 申请"
  fi
done

# ---------- 第 4 步：生成 .env ----------
step "第 4 步 / 7  生成 .env（已存在则跳过，不覆盖）"

set_kv() {  # set_kv <文件> <键> <值>
  local f="$1" k="$2" v="$3"
  mkdir -p "$(dirname "$f")"
  touch "$f"
  if grep -qE "^[[:space:]]*${k}=" "$f" 2>/dev/null; then
    sed -i -E "s|^[[:space:]]*${k}=.*|${k}=${v}|" "$f"
  else
    printf '%s=%s\n' "$k" "$v" >>"$f"
  fi
}

from_example() {  # from_example <组件目录>
  local d="$1"
  if [ -f "$d/.env" ]; then warn "$d/.env 已存在，跳过"
  elif [ -f "$d/.env.example" ]; then cp "$d/.env.example" "$d/.env"; ok "$d/.env 由模板生成"
  else warn "$d 没有 .env.example，跳过"
  fi
}

from_example "$PREFIX/leader"
for p in ros2_analyze ros2_plan ros2_codegen leader_orchestrator; do
  from_example "$PREFIX/partners/$p"
done
for a in gateway bailian-shim amp-forwarder; do
  from_example "$PREFIX/adapters/$a"
done

# Leader 关键项
set_kv "$PREFIX/leader/.env" "DASHSCOPE_API_KEY" "$DASHSCOPE_API_KEY"
set_kv "$PREFIX/leader/.env" "AUTH_TOKEN"        "$AUTH_TOKEN"
set_kv "$PREFIX/leader/.env" "LEADER_DB"         "$PREFIX/leader/leader.db"
set_kv "$PREFIX/leader/.env" "RAW_DUMP_DIR"      "$PREFIX/leader/raw"
set_kv "$PREFIX/leader/.env" "GATEWAY_URL"       "http://127.0.0.1:9100"
[ -n "$AIC_ORCH" ] && set_kv "$PREFIX/leader/.env" "ORCHESTRATOR_AIC" "$AIC_ORCH"

# 各 partner 的证书目录 + shim 地址
for p in ros2_analyze ros2_plan ros2_codegen leader_orchestrator; do
  f="$PREFIX/partners/$p/.env"
  [ -f "$f" ] || continue
  set_kv "$f" "CERT_DIR" "$CERT_DIR/$p"
  if [ "$p" != "leader_orchestrator" ]; then
    set_kv "$f" "PARTNER_LLM_DEFAULT_BASE_URL" "http://127.0.0.1:9099/v1"
  else
    set_kv "$f" "PARTNER_LLM_DEFAULT_BASE_URL" "http://127.0.0.1:9200/v1"
    set_kv "$f" "PARTNER_LLM_DEFAULT_MODEL"    "leader"
    set_kv "$f" "PARTNER_LLM_DEFAULT_API_KEY"  "$AUTH_TOKEN"
  fi
done

# 适配器
set_kv "$PREFIX/adapters/gateway/.env" "LEADER_URL" "http://127.0.0.1:9200"
set_kv "$PREFIX/adapters/gateway/.env" "AUTH_TOKEN" "$AUTH_TOKEN"
ok "关键配置已写入"

mkdir -p "$PREFIX/leader/raw"
ok "干净目录就绪（$PREFIX/leader/raw）"

# ---------- 第 4.5 步：同步四个 agent 到框架 ----------
step "第 4.5 步 / 7  把四个 agent 同步进框架的 partners/online/"

if [ -d "$FRAMEWORK_DIR/partners" ]; then
  ONLINE="$FRAMEWORK_DIR/partners/online"
  mkdir -p "$ONLINE"
  for p in ros2_analyze ros2_plan ros2_codegen leader_orchestrator; do
    rm -rf "$ONLINE/$p"
    cp -a "$PREFIX/partners/$p" "$ONLINE/$p"
    rm -rf "$ONLINE/$p/venv" "$ONLINE/$p/.venv" "$ONLINE/$p/__pycache__"
    ok "$p → $ONLINE/$p"
  done
else
  warn "框架目录里没有 partners/，跳过同步（框架装好后重跑本脚本）"
fi

# ---------- 第 5 步：建 venv ----------
step "第 5 步 / 7  建虚拟环境并安装依赖"

make_venv() {  # make_venv <目录>
  local d="$1"
  [ -f "$d/requirements.txt" ] || { warn "$d 无 requirements.txt，跳过"; return 0; }
  if [ "$SKIP_VENV" = "1" ]; then warn "--skip-venv，跳过 $d"; return 0; fi
  if [ ! -x "$d/venv/bin/python" ]; then
    python3 -m venv "$d/venv" || die "创建 venv 失败：$d"
  fi
  "$d/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  if "$d/venv/bin/pip" install -q -r "$d/requirements.txt"; then
    ok "$(basename "$d") 依赖装好"
  else
    die "依赖安装失败：$d/requirements.txt"
  fi
}

make_venv "$PREFIX/leader"
make_venv "$PREFIX/adapters/gateway"
make_venv "$PREFIX/adapters/bailian-shim"
make_venv "$PREFIX/adapters/amp-forwarder"

# ---------- 第 6 步：systemd ----------
step "第 6 步 / 7  安装 systemd 服务"

install_unit() {  # install_unit <仓库内 unit 文件> <服务名>
  local src="$1" name="$2"
  [ -f "$src" ] || { warn "找不到 $src，跳过 $name"; return 0; }
  sed -e "s|/opt/acps-agent|$PREFIX|g" \
      -e "s|/opt/ros2-leader-stack|$PREFIX|g" \
      -e "s|/root/acps-agent|$PREFIX|g" \
      -e "s|^User=acps|User=root|" \
      "$src" > "/etc/systemd/system/${name}.service"
  systemctl daemon-reload
  systemctl enable "${name}.service" >/dev/null 2>&1 || true
  ok "${name}.service 已安装"
}

if [ "$SKIP_SYSTEMD" = "1" ]; then
  warn "--skip-systemd，跳过"
else
  install_unit "$PREFIX/leader/leader.service"          "leader"
  install_unit "$PREFIX/adapters/gateway/gateway.service" "acps-gateway"
  install_unit "$PREFIX/adapters/bailian-shim/bailian-shim.service" "bailian-shim"
  install_unit "$PREFIX/adapters/amp-forwarder/amp-forwarder.service" "amp-forwarder"

  # acps-agent 托管的是【框架目录】里的 agent，路径不能跟着 $PREFIX 走 → 单独生成
  FW_PY="$FRAMEWORK_DIR/.venv/bin/python"
  [ -x "$FW_PY" ] || FW_PY="python3"
  cat >/etc/systemd/system/acps-agent.service <<EOF
[Unit]
Description=ACPs Partner Agents (ros2-leader-stack)
Documentation=https://wt.ioa.pub/docs.html
After=network-online.target bailian-shim.service
Wants=network-online.target bailian-shim.service

[Service]
Type=simple
User=root
WorkingDirectory=$FRAMEWORK_DIR
ExecStart=$FW_PY -m partners.main
Restart=always
RestartSec=5
TimeoutStopSec=25
StandardOutput=journal
StandardError=journal
SyslogIdentifier=acps-agent

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable acps-agent.service >/dev/null 2>&1 || true
  ok "acps-agent.service 已生成（托管 $FRAMEWORK_DIR/partners/online 下全部 agent）"
fi

# ---------- 第 7 步：框架补丁 ----------
step "第 7 步 / 7  给 ACPs 框架打补丁"

if [ -d "$FRAMEWORK_DIR" ]; then
  FW_PY="$FRAMEWORK_DIR/.venv/bin/python"
  [ -x "$FW_PY" ] || FW_PY="python3"
  for p in "$PREFIX/framework/patches/patch_demo_partner.py" \
           "$PREFIX/framework/patches/patch_demo_partner_rawlog.py"; do
    if [ -f "$p" ]; then
      ( cd "$FRAMEWORK_DIR" && "$FW_PY" "$p" ) && ok "已应用 $(basename "$p")" \
        || warn "$(basename "$p") 执行报错（可能已打过，看上面输出）"
    fi
  done
else
  warn "没找到框架目录 $FRAMEWORK_DIR —— 接入层需要它。"
  warn "请先安装 ACPs demo-partner 框架，然后重跑：sudo bash install.sh --skip-venv"
fi

# ---------- 启动 ----------
if [ "$DO_START" = "1" ]; then
  step "启动服务"
  for s in bailian-shim acps-gateway acps-agent leader amp-forwarder; do
    if systemctl list-unit-files | grep -q "^${s}.service"; then
      systemctl restart "$s" 2>/dev/null || true
      sleep 1
      if systemctl is-active --quiet "$s"; then ok "$s 运行中"
      else warn "$s 没起来 —— journalctl -u $s -n 50 --no-pager"
      fi
    else
      warn "$s 未安装"
    fi
  done

  echo
  echo "  下一步："
  echo "    sudo bash verify.sh                 # 逐项自检"
  echo "    sudo bash verify.sh --ask \"什么是 ROS2 的话题？\""
  echo
  echo "  配置文件位置："
  echo "    $PREFIX/leader/.env"
  echo "    $PREFIX/partners/*/.env"
  echo "    $PREFIX/adapters/*/.env"
else
  warn "--no-start，未启动。手动：systemctl start leader acps-gateway bailian-shim acps-agent amp-forwarder"
fi

echo
echo "${C_OK}完成。${C_OFF}如有红色 ✗ 请按提示处理后重跑本脚本（幂等，安全）。"
