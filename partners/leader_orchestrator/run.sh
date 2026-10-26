#!/usr/bin/env bash
# =============================================================================
#  partners/leader_orchestrator/run.sh —— 启动【平台对外身份】接入层（:8449）
#
#  这个实例不是"能力"，而是【对平台的壳】：平台按 AIC 找到它，它把请求
#  转给本机的 Leader（:9200）。所以它的 default 档 base_url 指向 Leader。
#
#  用法：
#    bash run.sh
#    FRAMEWORK_DIR=/path/to/demo-partner bash run.sh
#    NO_SYNC=1 bash run.sh
# =============================================================================
set -euo pipefail

NAME="leader_orchestrator"
PORT="8449"
TITLE="平台对外身份（→ Leader :9200）"

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
  echo "→ 先填好 PARTNER_LLM_DEFAULT_BASE_URL=http://127.0.0.1:9200/v1"
  echo "  和 PARTNER_LLM_DEFAULT_API_KEY=<Leader 的 AUTH_TOKEN>，再重新执行本脚本"
  exit 0
fi

if [ "${NO_SYNC:-0}" != "1" ]; then
  mkdir -p "$ONLINE_DIR"
  rm -rf "$ONLINE_DIR/$NAME"
  cp -a "$DIR" "$ONLINE_DIR/$NAME"
  rm -rf "$ONLINE_DIR/$NAME/.venv" "$ONLINE_DIR/$NAME/__pycache__"
  echo "已安装到 $ONLINE_DIR/$NAME"
fi

echo "先确认 Leader 活着：curl -s http://127.0.0.1:9200/health || true"
echo "启动 $TITLE（端口 $PORT，AIP over mTLS）... Ctrl+C 停止"
cd "$FRAMEWORK_DIR"
exec "$PY" -m partners.main "$NAME"
