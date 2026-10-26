#!/usr/bin/env bash
# =============================================================================
#  partners/ros2_codegen/run.sh —— 启动【③ 代码生成】接入层（:9023）
#
#  做什么：
#    1) 首次运行从 .env.example 生成 .env（不会覆盖已存在的）
#    2) 把本目录同步到框架的 partners/online/ros2_codegen
#    3) 用框架的 venv 前台启动这一个 agent（Ctrl+C 停止）
#
#  用法：
#    bash run.sh
#    FRAMEWORK_DIR=/path/to/demo-partner bash run.sh
#    NO_SYNC=1 bash run.sh
# =============================================================================
set -euo pipefail

NAME="ros2_codegen"
PORT="9023"
TITLE="③ 代码生成"

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
echo "提示：③ 会被每个节点调用一次，Leader 侧默认最多 4 路并发"
cd "$FRAMEWORK_DIR"
exec "$PY" -m partners.main "$NAME"
