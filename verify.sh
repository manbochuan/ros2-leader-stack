#!/usr/bin/env bash
# =============================================================================
#  ros2-leader-stack  ·  一键自检
#
#  用法:
#     sudo bash verify.sh                       # 逐项自检
#     sudo bash verify.sh --full                # 额外跑一次完整项目流水线（约 2 分钟）
#     sudo bash verify.sh --ask "什么是 ROS2 的话题？"
#     PREFIX=/srv/rls sudo bash verify.sh        # 换了安装目录
# =============================================================================
set -uo pipefail

PREFIX="${PREFIX:-/opt/ros2-leader-stack}"
GATEWAY_PORT="${GATEWAY_PORT:-9100}"
LEADER_PORT="${LEADER_PORT:-9200}"
PUBLIC_HOST="${PUBLIC_HOST:-}"       # 想测外网可达就设成你的公网 IP
AIC_ORCH="${AIC_ORCH:-}"
FULL=0
ASK_TEXT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ask)  ASK_TEXT="${2:?...}"; shift 2 ;;
    --full) FULL=1; shift ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_WA=$'\033[33m'; C_HD=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_NO=""; C_WA=""; C_HD=""; C_OFF=""
fi
PASS=0; FAIL=0; SKIP=0

head2() { echo; echo "${C_HD}── $* ──${C_OFF}"; }
pass() { PASS=$((PASS+1)); echo "  ${C_OK}✓${C_OFF} $*"; }
fail() { FAIL=$((FAIL+1)); echo "  ${C_NO}✗${C_OFF} $*"; }
skip() { SKIP=$((SKIP+1)); echo "  ${C_WA}-${C_OFF} $*"; }
hint() { echo "      ${C_WA}→${C_OFF} $*"; }

env_get() {  # env_get <文件> <键>
  [ -f "$1" ] || { echo ""; return; }
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | sed -E "s|^[[:space:]]*$2=[[:space:]]*||; s|^[\"']||; s|[\"']$||"
}

json_get() {  # json_get <python表达式>（从 stdin 读 json）
  python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("__ERR__", e); raise SystemExit(0)
print(eval(sys.argv[1], {"d": d, "len": len, "str": str}))
' "$1" 2>/dev/null
}

echo "${C_HD}ros2-leader-stack 自检${C_OFF}   PREFIX=$PREFIX"

# ---------------------------------------------------------------------------
head2 "1/7  服务状态"
for s in leader acps-gateway bailian-shim acps-agent amp-forwarder; do
  if ! systemctl list-unit-files 2>/dev/null | grep -q "^${s}.service"; then
    skip "$s 未安装（可选服务可以不管）"
    continue
  fi
  if systemctl is-active --quiet "$s"; then
    pass "$s 运行中"
  else
    fail "$s 未运行"
    hint "journalctl -u $s -n 60 --no-pager"
  fi
done

# ---------------------------------------------------------------------------
head2 "2/7  端口监听"
check_port() {  # check_port <端口> <名字> <是否必须>
  local port="$1" name="$2" need="$3"
  if ss -tlnp 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    pass "$name :$port 在监听"
  elif [ "$need" = "1" ]; then
    fail "$name :$port 没在监听"
    hint "看对应服务日志；或 ss -tlnp | grep $port"
  else
    skip "$name :$port 未监听（非必需）"
  fi
}
check_port 9200 "Leader"        1
check_port "$GATEWAY_PORT" "gateway"   1
check_port 9099 "shim"          1
check_port 8449 "leader_orchestrator" 0
check_port 9021 "① 需求分析"    0
check_port 9022 "② 项目规划"    0
check_port 9023 "③ 代码生成"    0

# ---------------------------------------------------------------------------
head2 "3/7  外网可达（可选）"
if [ -z "$PUBLIC_HOST" ]; then
  skip "未设置 PUBLIC_HOST，跳过（想测：PUBLIC_HOST=1.2.3.4 sudo bash verify.sh）"
else
  for port in 8449 9021 9022 9023; do
    if timeout 6 bash -c "echo | openssl s_client -connect ${PUBLIC_HOST}:${port} -servername ${PUBLIC_HOST} >/dev/null 2>&1"; then
      pass "${PUBLIC_HOST}:${port} TLS 握手成功"
    else
      fail "${PUBLIC_HOST}:${port} 连不上"
      hint "九成是两层防火墙：云安全组 + ufw（ufw status | grep ${port}）"
    fi
  done
fi

# ---------------------------------------------------------------------------
head2 "4/7  Leader 直连"
TOKEN="$(env_get "$PREFIX/leader/.env" AUTH_TOKEN)"
if [ -z "$TOKEN" ]; then
  fail "读不到 $PREFIX/leader/.env 的 AUTH_TOKEN"
else
  RESP="$(curl -s --max-time 60 -X POST "http://127.0.0.1:${LEADER_PORT}/leader" \
          -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' \
          -d '{"text":"什么是 ROS2 的话题？"}' || true)"
  STATE="$(printf '%s' "$RESP" | json_get 'd.get("state")')"
  LEN="$(printf '%s' "$RESP" | json_get 'len(d.get("reply") or "")')"
  if [ "$STATE" = "ok" ] && [ "${LEN:-0}" -gt 20 ] 2>/dev/null; then
    pass "Leader 自答正常（state=ok，${LEN} 字）"
  else
    fail "Leader 直连异常：${RESP:0:200}"
    hint "journalctl -u leader -n 40 --no-pager"
  fi
fi

# ---------------------------------------------------------------------------
head2 "5/7  网关 → 平台 → 8449（端到端一跳）"
GW_TOKEN="$(env_get "$PREFIX/adapters/gateway/.env" AUTH_TOKEN)"
[ -n "$GW_TOKEN" ] || GW_TOKEN="$(env_get "$PREFIX/adapters/gateway/.env" MY_TOKEN)"
if [ -z "$AIC_ORCH" ]; then AIC_ORCH="$(env_get "$PREFIX/leader/.env" ORCHESTRATOR_AIC)"; fi
if [ -z "$AIC_ORCH" ]; then
  skip "未提供 AIC_ORCH，跳过（AIC_ORCH=1.2.156... sudo bash verify.sh）"
elif [ -z "$GW_TOKEN" ]; then
  skip "网关 .env 里没有 AUTH_TOKEN / MY_TOKEN，跳过"
else
  RESP="$(curl -s --max-time 300 -X POST "http://127.0.0.1:${GATEWAY_PORT}/call_partner" \
          -H "X-Auth-Token: $GW_TOKEN" -H 'Content-Type: application/json' \
          -d "{\"aic\":\"$AIC_ORCH\",\"input\":\"什么是 ROS2 的话题？\"}" || true)"
  OKF="$(printf '%s' "$RESP" | json_get 'd.get("ok")')"
  TXT="$(printf '%s' "$RESP" | json_get '(d.get("text") or "")[:80]')"
  if [ "$OKF" = "True" ]; then
    pass "端到端打通：$TXT"
  else
    fail "端到端失败：${RESP:0:220}"
    hint "journalctl -u acps-gateway -n 40 --no-pager"
  fi
fi

# ---------------------------------------------------------------------------
head2 "6/7  会话库与关键目录"
if [ -f "$PREFIX/leader/leader.db" ]; then
  MODE="$(python3 -c "
import sqlite3
c=sqlite3.connect('$PREFIX/leader/leader.db')
print(c.execute('PRAGMA journal_mode').fetchone()[0])
" 2>/dev/null)"
  N="$(python3 -c "
import sqlite3
c=sqlite3.connect('$PREFIX/leader/leader.db')
print(c.execute('select count(*) from sessions').fetchone()[0])
" 2>/dev/null || echo '?')"
  if [ "$MODE" = "wal" ]; then pass "leader.db 存在，journal_mode=wal（多线并发不锁库），会话数=$N"
  else fail "leader.db 的 journal_mode=$MODE（应为 wal，看 leader 日志里有没有 PRAGMA 失败）"
  fi
else
  skip "leader.db 还没生成（先让 Leader 处理一次请求）"
fi
if [ -d "$PREFIX/leader/raw" ]; then
  CNT="$(ls -1 "$PREFIX/leader/raw" 2>/dev/null | wc -l)"
  if [ "$CNT" -eq 0 ]; then pass "解析失败落盘目录为空（说明解析一次成功）"
  else warn_note="有 $CNT 个解析失败的原始输出，看：ls -l $PREFIX/leader/raw"; echo "  ${C_WA}!${C_OFF} $warn_note"
  fi
else
  skip "raw 目录还没建（首次解析失败时才会用到）"
fi

# ---------------------------------------------------------------------------
if [ -n "$ASK_TEXT" ]; then
  head2 "7/7  试跑一句话"
  echo "  输入：$ASK_TEXT"
  T0=$(date +%s)
  RESP="$(curl -s --max-time 600 -X POST "http://127.0.0.1:${LEADER_PORT}/leader" \
          -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' \
          -d "{\"text\":$(printf '%s' "$ASK_TEXT" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}" || true)"
  T1=$(date +%s)
  printf '%s' "$RESP" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  输出不是 JSON")
    raise SystemExit(0)
r = d.get("reply") or ""
print("  state =", d.get("state"), " 长度 =", len(r))
print("  ---- 前 40 行 ----")
for line in r.splitlines()[:40]:
    print("  " + line)
'
  echo "  （实际墙钟耗时 $((T1-T0)) 秒）"
elif [ "$FULL" = "1" ]; then
  head2 "7/7  完整项目流水线（约 2 分钟）"
  T0=$(date +%s)
  RESP="$(curl -s --max-time 600 -X POST "http://127.0.0.1:${LEADER_PORT}/leader" \
          -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' \
          -d '{"text":"帮我做一个避障小车"}' || true)"
  T1=$(date +%s)
  printf '%s' "$RESP" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d.get("reply") or ""
print("  state        :", d.get("state"))
print("  长度         :", len(r))
print("  有规划书     :", "项目规划书" in r)
print("  有代码块     :", "```python" in r)
print("  有解析失败   :", "解析失败" in r)
' || fail "返回不是 JSON"
  echo "  （墙钟耗时 $((T1-T0)) 秒）"
else
  head2 "7/7  试跑"
  skip "没指定 --ask / --full，跳过"
fi

# ---------------------------------------------------------------------------
echo
if [ "$FAIL" -eq 0 ]; then
  echo "${C_OK}全部通过${C_OFF}（通过 $PASS，跳过 $SKIP）"
  exit 0
else
  echo "${C_NO}有 $FAIL 项失败${C_OFF}（通过 $PASS，跳过 $SKIP）—— 按上面 → 提示逐条查"
  exit 1
fi
