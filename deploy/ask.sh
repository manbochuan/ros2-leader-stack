#!/usr/bin/env bash
# ============================================================
#  直接问百炼工作流，打印「耗时 + 返回内容」
#
#  用途：绕过我们所有代码，单独测工作流本身的快慢和输出。
#
#  用法：
#     bash ask.sh
#     bash ask.sh "帮我写一个hello节点"
#     bash ask.sh "帮我做一个避障小车项目"
# ============================================================
set -uo pipefail

SHIM_DIR=/opt/acps-agent/bailian-shim
PROMPT="${1:-帮我写一个发布 hello 字符串的 ROS2 Python 节点}"
OUT=/tmp/bailian_answer.json

cd "$SHIM_DIR" || { echo "找不到 $SHIM_DIR"; exit 1; }

if ! command -v jq >/dev/null 2>&1; then
  echo "缺少 jq，先跑： apt install -y jq"
  exit 1
fi

APP_ID="$(awk -F= '/^BAILIAN_APP_ID=/{print $2}' .env)"
API_KEY="$(awk -F= '/^DASHSCOPE_API_KEY=/{print $2}' .env)"

if [[ -z "$APP_ID" || -z "$API_KEY" ]]; then
  echo "读取 app_id / API-KEY 失败，检查 $SHIM_DIR/.env"
  exit 1
fi

echo "==================================================="
echo "  提问：$PROMPT"
echo "  app_id：$APP_ID"
echo "==================================================="
echo
echo "调用中，请耐心等（工作流可能要 1~3 分钟，屏幕会一直不动，这是正常的）……"
echo

# -o 存内容，-w 打印耗时；不要用 -o /dev/null，否则看不到返回
curl -s -m 400 \
  -o "$OUT" \
  -w 'HTTP=%{http_code}   纯百炼耗时=%{time_total}s\n' \
  -X POST "https://dashscope.aliyuncs.com/api/v1/apps/${APP_ID}/completion" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg p "$PROMPT" '{input:{prompt:$p}, parameters:{}}')"

STATUS=$?

echo
echo "=================== 百炼返回 ==================="

if [[ $STATUS -ne 0 ]]; then
  echo "[curl 退出码 $STATUS ：超时或被中断]"
  echo "原始内容（如果有）："
  head -c 600 "$OUT" 2>/dev/null
elif [[ ! -s "$OUT" ]]; then
  echo "[返回是空的]"
else
  # 优先打印 output.text；如果没有，就把整个 JSON 打出来
  TEXT="$(jq -r '.output.text // empty' "$OUT" 2>/dev/null)"
  if [[ -n "$TEXT" ]]; then
    printf '%s\n' "$TEXT"
    echo
    echo "--- 元信息 ---"
    jq -r '"finish_reason: \(.output.finish_reason // "?")   session_id: \(.output.session_id // "?")"' "$OUT" 2>/dev/null
  else
    head -c 1500 "$OUT"
  fi
fi

echo "================================================"
echo "完整 JSON 存在：$OUT"
