#!/usr/bin/env bash
# ============================================================
#  梧桐智能体互联平台 · ACPs Partner 适配器
#  第一天环境准备脚本（与具体业务代码无关，可重复执行）
#
#  用法：
#     sudo bash bootstrap.sh
#
#  可覆盖的环境变量：
#     AGENT_USER=acps  INSTALL_ROOT=/opt/acps-agent  PY_VERSION=3.14
# ============================================================
set -euo pipefail

AGENT_USER="${AGENT_USER:-acps}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/acps-agent}"
PY_VERSION="${PY_VERSION:-3.14}"
UV_PYTHON_DIR="${UV_PYTHON_DIR:-/opt/uv-python}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

# ---------- 前置检查 ----------
if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行： sudo bash $0" >&2
  exit 1
fi

if ! grep -qi ubuntu /etc/os-release 2>/dev/null && ! grep -qi debian /etc/os-release 2>/dev/null; then
  warn "本脚本按 Ubuntu/Debian 编写，当前系统是：$(grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
  warn "继续执行可能失败，CentOS 系请改装 python3.14 与 uv 后跳到第 4 步。"
fi

# ---------- 1. 基础工具 ----------
log "1/7 系统更新与基础工具"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y \
  curl git tmux ca-certificates \
  build-essential pkg-config libssl-dev \
  chrony jq

# ---------- 2. 时间同步 ----------
# mTLS 证书校验对时间敏感，时钟漂移会导致握手失败
log "2/7 时间同步与系统时区"
timedatectl set-timezone Asia/Shanghai || warn "设置时区失败，忽略"
systemctl enable --now chrony 2>/dev/null || warn "chrony 启动失败，请手动检查"
chronyc makestep 2>/dev/null || true

# ---------- 3. swap ----------
# 2 核 2G 通常够用，但 pip install 的内存峰值很猛，加 swap 兜底
log "3/7 检查 swap"
if [[ "$(swapon --show --noheadings 2>/dev/null | wc -l)" -eq 0 ]]; then
  echo "未检测到 swap，创建 ${SWAP_SIZE} 的 swapfile …"
  fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-acps-swappiness.conf
  echo "swap 已启用。"
else
  echo "已有 swap："
  swapon --show
fi

# ---------- 4. 服务账号 ----------
log "4/7 创建专用服务账号 ${AGENT_USER}"
if id -u "$AGENT_USER" >/dev/null 2>&1; then
  echo "账号已存在，跳过。"
else
  useradd --create-home --shell /bin/bash "$AGENT_USER"
  echo "已创建账号 ${AGENT_USER}。"
fi

# ---------- 5. uv + Python ----------
# 解释器装到 /opt 而不是某个用户的 HOME：
#   venv 会记住创建它的解释器的绝对路径，换用户跑服务时会读不到
log "5/7 安装 uv 与 Python ${PY_VERSION}"
export UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR"
mkdir -p "$UV_PYTHON_DIR"

export PATH="/root/.local/bin:${PATH}"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv 安装失败，请检查网络后重跑本脚本。" >&2
  exit 1
fi
uv --version

# --no-bin：不往 ~/.local/bin 放不可靠的软链
uv python install --no-bin "$PY_VERSION"
PYBIN="$(UV_PYTHON_PREFERENCE=only-managed uv python find "$PY_VERSION")"
echo "Python 解释器：$PYBIN"
"$PYBIN" --version

# ---------- 6. 目录结构 ----------
log "6/7 建立目录结构"
mkdir -p "$INSTALL_ROOT"/{config,certs,logs,data}
chown -R "${AGENT_USER}:${AGENT_USER}" "$INSTALL_ROOT"
chmod 750 "$INSTALL_ROOT"
chmod 700 "$INSTALL_ROOT/certs"      # 私钥目录，仅服务账号可读
printf '%s\n' "$PYBIN" > "$INSTALL_ROOT/.python-path"
chown "${AGENT_USER}:${AGENT_USER}" "$INSTALL_ROOT/.python-path"

# ---------- 7. 完成 ----------
log "7/7 环境就绪"
cat <<EOF

  Python 解释器 : ${PYBIN}
  服务账号      : ${AGENT_USER}
  安装根目录    : ${INSTALL_ROOT}
    ├── config/   配置（.env、acps.toml）
    ├── certs/    mTLS 证书（700，仅 ${AGENT_USER} 可读）
    ├── logs/
    └── data/

  下一步：
    1) 把适配器代码放到 ${INSTALL_ROOT}
    2) 建 venv（必须在 ${INSTALL_ROOT} 下执行，服务按 CWD 找 config/）：
         cd ${INSTALL_ROOT}
         sudo -u ${AGENT_USER} ${PYBIN} -m venv venv
         sudo -u ${AGENT_USER} ./venv/bin/pip install -U pip
    3) 把证书放进 ${INSTALL_ROOT}/certs/（agent-key.pem / agent-cert.pem / trust-bundle.pem）
    4) 安装 systemd 服务： deploy/acps-agent.service

  ⚠️ 别忘了在云控制台放行端口：
       轻量应用服务器 → 「防火墙」
       云服务器 ECS   → 「安全组」→ 入方向
     需要放行： 22(SSH) + 你的服务端口（默认 8443）

EOF
