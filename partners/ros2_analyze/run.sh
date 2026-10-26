#!/usr/bin/env bash
# =============================================================================
#  partners/ros2_analyze/run.sh —— 启动【① 需求分析】接入层（:9021）
#
#  做什么：
#    1) 首次运行从 .env.example 生成 .env（不会覆盖已存在的）
#    2) 把本目录同步到框架的 partners/online/ros2_analyze
#    3) 用框架的 venv 前台启动这一个 agent（Ctrl+C 停止）
#
#  用法：
#    bash run.sh                                   # 默认框架路径
#    FRAMEWORK_DIR=/path/to/demo-partner bash run.sh
#    NO_SYNC=1 bash run.sh                          # 已在框架目录里，跳过同步
# =============================================================================
set -euo pipefail

NAME="ros2_analyze"
PORT="9021"
TITLE="① 需求分析"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="${FRAMEWORK_DIR:-/opt/acps-agent/acps-community/demo-partner}"
ONLINE_DIR="$FRAMEWORK_DIR/partners/online"
PY="$FRAMEWORK_DIR/.venv/bin/python"

die() { echo "✗ $*" >&2; exit 1; }

[ -d "$FRAMEWORK_DIR" ] || die "找不到框架目录：$FRAMEWORK_DIR
  先安装 ACPs demo-partner 框架，或用 FRAMEWORK_DIR=... 指定"
[ -x "$PY" ] || PY="python3"

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "已生成 $DIR/.env"
  echo "→ 先填好里面的 PARTNER_LLM_FAST_* / PARTNER_LLM_DEFAULT_*，再重新执行本脚本"
  exit 0
fi

if [ "${NO_SYNC:-0}" != "1" ]; then
  mkdir -p "$ONLINE_DIR"
  rm -rf "$ONLINE_DIR/$NAME"
  cp -a "$DIR" "$ONLINE_DIR/$NAME"
  rm -rf "$ONLINE_DIR/$NAME/.venv" "$ONLINE_DIR/$NAME/__pycache__"
  echo "已安装到 $ONLINE_DIR/$NAME"
fi

echo "启动 $TITLE（端口 $PORT，AIP over mTLS）... Ctrl+C 停止"
cd "$FRAMEWORK_DIR"
exec "$PY" -m partners.main "$NAME"
