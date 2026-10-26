#!/usr/bin/env bash
# ============================================================
#  本地测一个 AIP 任务（mTLS 版本）
#
#  用法：
#     bash test-agent.sh
#     bash test-agent.sh "帮我写一个订阅 /scan 的节点"
#
#  说明：
#   开启了身份绑定后，服务器会用客户端证书的 CN 和请求里的
#   senderId 比对，两者必须一致。本脚本用你自己的智能体证书
#   作为客户端证书，所以 senderId 也填你自己的 AIC。
#
#  结果原文保存在 /tmp/_aip_last.json
# ============================================================
set -uo pipefail

CERTDIR=/opt/acps-agent/acps-community/acps-cli/private
CERT="$CERTDIR/agent-cert.pem"
KEY="$CERTDIR/agent-key.pem"
BASE=https://127.0.0.1:8443/rpc
HEALTH=https://127.0.0.1:8443/health
LAST=/tmp/_aip_last.json
MAX_WAIT="${MAX_WAIT:-600}"
INTERVAL=5

PROMPT="${1:-帮我写一个发布 hello 字符串的 ROS2 Python 节点}"

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

command -v jq >/dev/null 2>&1 || die "缺少 jq，先执行： apt install -y jq"
[[ -r "$CERT" ]] || die "读不到证书：$CERT"
[[ -r "$KEY"  ]] || die "读不到私钥：$KEY"

CURL=(curl -sk -m 60 --cert "$CERT" --key "$KEY")

# ---------- 0. 取本机 AIC（= 证书 CN）----------
AIC=$(openssl x509 -in "$CERT" -noout -subject \
      | sed -n 's/.*CN *= *\([^,]*\).*/\1/p' | tr -d ' ')
[[ -n "$AIC" ]] || die "无法从证书里读出 CN"

say "0. 前置检查"
echo "  本机 AIC : $AIC"
if ! "${CURL[@]}" -m 8 "$HEALTH" >/dev/null 2>&1; then
  die "8443 无响应（或证书被拒）。试试： systemctl status acps-agent"
fi
echo "  [OK] 智能体 https://127.0.0.1:8443 在线，mTLS 握手通过"
if ! curl -s -m 5 http://127.0.0.1:9099/health | jq -e '.status=="ok"' >/dev/null 2>&1; then
  warn "  [警告] shim(9099) 不健康，Production 阶段会失败"
else
  echo "  [OK] shim 9099 在线"
fi

# ---------- 1. 发任务 ----------
STAMP="$(date +%s)"
TASK="task-$STAMP"
SESSION="session-$STAMP"
NOW="$(date +%Y-%m-%dT%H:%M:%S+08:00)"

say "1. 提交任务"
echo "  提问   : $PROMPT"
echo "  taskId : $TASK"

payload=$(jq -n \
  --arg p "$PROMPT" --arg t "$TASK" --arg s "$SESSION" --arg now "$NOW" \
  --arg mid "msg-$TASK" --arg sid "$AIC" \
  '{jsonrpc:"2.0", id:"1", method:"rpc",
    params:{command:{
      type:"task-command", id:$mid, sentAt:$now,
      senderRole:"leader", senderId:$sid, command:"start",
      dataItems:[{type:"text", text:$p}],
      taskId:$t, sessionId:$s}}}')

"${CURL[@]}" -X POST "$BASE" -H 'Content-Type: application/json' \
  -d "$payload" -o "$LAST" || die "提交失败（网络或握手问题）"

state=$(jq -r '.result.status.state // "?"' "$LAST" 2>/dev/null)
if [[ "$state" == "?" ]]; then
  echo "  返回原文："
  head -c 900 "$LAST"; echo
  die "提交没拿到预期响应，见上方原文"
fi
echo "  已受理，初始状态：$state"

START=$(date +%s)

# ---------- 2. 轮询 ----------
say "2. 等待产出（每 ${INTERVAL}s 查一次）"
while true; do
  sleep "$INTERVAL"
  elapsed=$(( $(date +%s) - START ))
  if (( elapsed > MAX_WAIT )); then
    warn "  超时：${MAX_WAIT}s 还没完成"
    echo "  日志： journalctl -u acps-agent -n 50"
    echo "        journalctl -u bailian-shim -n 30"
    exit 1
  fi

  getpayload=$(jq -n --arg t "$TASK" --arg s "$SESSION" --arg now "$NOW" \
    --arg mid "msg-g$elapsed" --arg sid "$AIC" \
    '{jsonrpc:"2.0", id:"2", method:"rpc",
      params:{command:{
        type:"task-command", id:$mid, sentAt:$now,
        senderRole:"leader", senderId:$sid, command:"get",
        taskId:$t, sessionId:$s}}}')

  "${CURL[@]}" -X POST "$BASE" -H 'Content-Type: application/json' \
    -d "$getpayload" -o "$LAST"
  state=$(jq -r '.result.status.state // "?"' "$LAST" 2>/dev/null)
  printf '  [%4ds] %s\n' "$elapsed" "$state"

  case "$state" in
    awaiting-completion|completed)
      say "完成！总耗时 ${elapsed}s"
      echo "--- 产出预览 -------------------------------------------------"
      jq -r '.result.products[]?.dataItems[]? | select(.type=="text") | .text' "$LAST" | head -c 1500
      echo
      echo "--------------------------------------------------------------"
      echo "完整原文： $LAST"
      exit 0
      ;;
    failed|canceled|rejected)
      say "任务终止，状态：$state"
      jq '.result.status' "$LAST"
      echo "日志： journalctl -u acps-agent -n 50"
      exit 1
      ;;
  esac
done
