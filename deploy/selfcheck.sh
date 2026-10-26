#!/usr/bin/env bash
# ============================================================
#  智能体接入自检报告
#
#  逐层排查 + 用已知正常的智能体做对照，用来判定
#  "问题在我们这边" 还是 "问题在平台侧"。
#
#  用法（黑框）：
#     bash selfcheck.sh
#     bash selfcheck.sh 2>&1 | tee /tmp/selfcheck.txt
# ============================================================
set -uo pipefail

CLI_DIR=/opt/acps-agent/acps-community/acps-cli
CLI="$CLI_DIR/.venv/bin/acps-cli"
CFG="$CLI_DIR/acps-cli.toml"
CERTDIR="$CLI_DIR/private"
CERT="$CERTDIR/agent-cert.pem"
KEY="$CERTDIR/agent-key.pem"

MY_AIC=1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD
# 对照组：北京城区旅游智能体（平台上已知可用的智能体）
REF_AIC=1.2.156.3088.1.BUPT.AAAAAA.BBBBBB.CCCC.DDDD
PUB_IP=YOUR_HOST

pass() { printf '  \033[32m[OK]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[!!]\033[0m %s\n' "$*"; }
info() { printf '  [--] %s\n' "$*"; }
sec()  { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

printf '\n############ 智能体自检报告  %s ############\n' "$(date '+%F %T')"

# ---------------------------------------------------------------
sec "1. 服务层"
for s in bailian-shim acps-agent; do
  st=$(systemctl is-active "$s" 2>/dev/null || true)
  if [[ "$st" == "active" ]]; then pass "$s = active"; else fail "$s = $st"; fi
done
ss -lntp 2>/dev/null | grep -q ':8443' && pass "8443 正在监听" || fail "8443 没在监听"
ss -lntp 2>/dev/null | grep -q ':9099' && pass "9099 正在监听" || fail "9099 没在监听"

# ---------------------------------------------------------------
sec "2. 证书层"
if [[ -r "$CERT" ]]; then
  pass "证书文件可读"
  cn=$(openssl x509 -in "$CERT" -noout -subject 2>/dev/null | sed -n 's/.*CN *= *\([^,]*\).*/\1/p' | tr -d ' ')
  if [[ "$cn" == "$MY_AIC" ]]; then pass "CN 与 AIC 一致"; else fail "CN=$cn 与 AIC 不一致"; fi
  if openssl x509 -in "$CERT" -noout -text 2>/dev/null | grep -q "$PUB_IP"; then
    pass "SAN 含公网 IP $PUB_IP"
  else
    fail "SAN 不含公网 IP"
  fi
  # 客户端证书（agent-cert.pem）本来就应该是 clientAuth
  if openssl x509 -in "$CERT" -noout -text 2>/dev/null | grep -q "TLS Web Client Authentication"; then
    pass "EKU = clientAuth（客户端证书，正确）"
  else
    fail "客户端证书 EKU 不是 clientAuth"
  fi
  info "到期时间：$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null | cut -d= -f2)"
else
  fail "读不到客户端证书 $CERT"
fi
# 服务端证书（ros2_tutor/server.pem）才需要 serverAuth
SRVCERT=/opt/acps-agent/acps-community/demo-partner/partners/online/ros2_tutor/server.pem
if [[ -r "$SRVCERT" ]]; then
  if openssl x509 -in "$SRVCERT" -noout -text 2>/dev/null | grep -q "TLS Web Server Authentication"; then
    pass "服务端证书 EKU = serverAuth（正确）"
  else
    fail "服务端证书 EKU 缺少 serverAuth"
  fi
  scn=$(openssl x509 -in "$SRVCERT" -noout -subject 2>/dev/null | sed -n 's/.*CN *= *\([^,]*\).*/\1/p' | tr -d ' ')
  [[ "$scn" == "$MY_AIC" ]] && pass "服务端证书 CN 与 AIC 一致" || fail "服务端证书 CN=$scn 与 AIC 不一致"
else
  fail "读不到服务端证书 $SRVCERT"
fi

# ---------------------------------------------------------------
sec "3. 可达性 / mTLS 握手"
hl=$(curl -sk -m 8 --cert "$CERT" --key "$KEY" https://127.0.0.1:8443/health 2>/dev/null)
if [[ -n "$hl" ]]; then pass "本机 mTLS 握手成功"; else fail "本机 mTLS 握手失败"; fi
hp=$(curl -sk -m 10 --cert "$CERT" --key "$KEY" "https://$PUB_IP:8443/health" 2>/dev/null)
if [[ -n "$hp" ]]; then pass "公网 IP mTLS 握手成功"; else fail "公网 IP mTLS 握手失败"; fi
info "health 返回：${hp:-（空）}"

# ---------------------------------------------------------------
sec "4. AIP 协议层（真发一个任务）"
TS=$(date +%s); TASK="chk-$TS"; SESS="s-$TS"; NOW=$(date +%Y-%m-%dT%H:%M:%S+08:00)
curl -sk -m 30 --cert "$CERT" --key "$KEY" -X POST https://127.0.0.1:8443/rpc \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg t "$TASK" --arg s "$SESS" --arg now "$NOW" --arg sid "$MY_AIC" \
      '{jsonrpc:"2.0",id:"1",method:"rpc",params:{command:{type:"task-command",id:"m1",sentAt:$now,
        senderRole:"leader",senderId:$sid,command:"start",
        dataItems:[{type:"text",text:"ROS2 发布节点代码怎么写"}],taskId:$t,sessionId:$s}}}')" \
  -o /tmp/_sc1.json >/dev/null 2>&1
st=$(jq -r '.result.status.state // "?"' /tmp/_sc1.json 2>/dev/null)
if [[ "$st" == "?" ]]; then
  fail "提交失败，返回：$(head -c 200 /tmp/_sc1.json)"
else
  pass "任务提交成功，初始状态=$st"
fi
done_ok=0
for i in $(seq 1 12); do
  sleep 3
  curl -sk -m 30 --cert "$CERT" --key "$KEY" -X POST https://127.0.0.1:8443/rpc \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg t "$TASK" --arg s "$SESS" --arg now "$NOW" --arg sid "$MY_AIC" --arg mid "g$i" \
        '{jsonrpc:"2.0",id:"2",method:"rpc",params:{command:{type:"task-command",id:$mid,sentAt:$now,
          senderRole:"leader",senderId:$sid,command:"get",taskId:$t,sessionId:$s}}}')" \
    -o /tmp/_sc2.json >/dev/null 2>&1
  st=$(jq -r '.result.status.state // "?"' /tmp/_sc2.json 2>/dev/null)
  [[ "$st" == "awaiting-completion" || "$st" == "completed" ]] && { done_ok=1; break; }
  [[ "$st" == "failed" || "$st" == "rejected" || "$st" == "canceled" ]] && break
done
if [[ $done_ok -eq 1 ]]; then
  n=$(jq -r '[.result.products[]?.dataItems[]? | select(.type=="text") | .text] | join("") | length' /tmp/_sc2.json 2>/dev/null)
  pass "任务完成（约 $((i*3)) 秒），产出 ${n:-0} 字符"
else
  fail "任务未完成，最后状态=$st"
fi

# ---------------------------------------------------------------
sec "5. 注册层（Registry）"
reg=$("$CLI" --config "$CFG" agent list --json 2>/dev/null)
if [[ -n "$reg" ]]; then
  echo "$reg" | jq -r '.items[]? | "  [--] \(.aic)\n        approval=\(.approval_status)  active=\(.is_active)  disabled=\(.is_disabled)  visibility=\(.acs.visibility)  acs_version=\(.acs_version)  vector_id=\(.vector_id)"' 2>/dev/null
else
  fail "查不到注册信息"
fi

# ---------------------------------------------------------------
sec "6. 发现层（★ 关键对照）"

flt_my=$(jq -n --arg a "$MY_AIC" '{conditions:[{field:"aic",op:"eq",value:$a},{field:"active",op:"eq",value:true}]}')
flt_ref=$(jq -n --arg a "$REF_AIC" '{conditions:[{field:"aic",op:"eq",value:$a},{field:"active",op:"eq",value:true}]}')

echo "  --- 6.1 语义检索（自然语言）---"
sem=$("$CLI" --config "$CFG" discover query "ROS2 学习与代码开发" --limit 5 2>/dev/null)
if echo "$sem" | grep -q "$MY_AIC"; then pass "语义检索：找到我们"; else fail "语义检索：没找到我们"; fi

echo "  --- 6.2 结构化过滤：我们 ---"
f_my=$("$CLI" --config "$CFG" discover query --type filtered --filter-json "$flt_my" --limit 5 2>/dev/null)
if echo "$f_my" | grep -q "$MY_AIC"; then pass "filtered：找到我们"; else fail "filtered：没找到我们"; fi

echo "  --- 6.3 结构化过滤：北京智能体（对照组）---"
f_ref=$("$CLI" --config "$CFG" discover query --type filtered --filter-json "$flt_ref" --limit 5 2>/dev/null)
if echo "$f_ref" | grep -q "$REF_AIC"; then
  pass "对照：filtered 能找到北京智能体 → 说明 filtered 这条链路本身是好的"
else
  fail "对照：filtered 连北京智能体也找不到 → 说明 filtered 链路整体有问题（平台侧）"
fi

echo "  --- 6.4 filtered 列出全部 active 智能体 ---"
flt_all='{"conditions":[{"field":"active","op":"eq","value":true}]}'
f_all=$("$CLI" --config "$CFG" discover query --type filtered --filter-json "$flt_all" --limit 50 2>/dev/null)
n_my=$(echo "$f_all" | grep -c "$MY_AIC" || true)
n_ref=$(echo "$f_all" | grep -c "$REF_AIC" || true)
info "列表里我们出现次数=$n_my，北京出现次数=$n_ref"
echo "$f_all" | grep -oE '"aic": *"[^"]+"' 2>/dev/null | sort -u | head -20 | sed 's/^/      /'

# ---------------------------------------------------------------
sec "7. 心跳 / AMP 上报"
n=$(journalctl -u acps-agent --since "60 min ago" --no-pager 2>/dev/null | grep -ciE 'emit failed|emit error|heartbeat.*(fail|error)' || true)
if [[ "${n:-0}" -eq 0 ]]; then pass "最近 60 分钟没有上报失败"; else fail "最近 60 分钟有 $n 条上报失败"; fi
journalctl -u acps-agent --since "60 min ago" --no-pager 2>/dev/null | grep -iE 'emit failed|heartbeat.*(fail|error)' | tail -3 | sed 's/^/      /'

# ---------------------------------------------------------------
sec "8. 平台调用记录（最近 24 小时）"
tot=$(journalctl -u acps-agent --since "24 hours ago" --no-pager 2>/dev/null | grep -c 'POST /rpc' || true)
info "收到 HTTP 请求总数：${tot:-0}"
echo "  [--] 调用来源 IP（最近 5 条）："
journalctl -u acps-agent --since "24 hours ago" --no-pager 2>/dev/null \
  | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+ - "POST /rpc' | tail -5 | sed 's/^/      /'
echo "  [--] 最近的 start 任务（最近 3 条）："
journalctl -u acps-agent --since "24 hours ago" --no-pager 2>/dev/null \
  | grep 'Start command received' | tail -3 | sed 's/^/      /'

# ---------------------------------------------------------------
sec "结论怎么读"
cat <<'EOF'
  把第 6 节的三个结果对照着看：

  ┌────────────────────────┬────────────────────────┬──────────────────────────┐
  │ 6.1 语义检索（我们）    │ 6.2 filtered（我们）    │ 结论                      │
  ├────────────────────────┼────────────────────────┼──────────────────────────┤
  │ OK                     │ 失败（6.3 对照成功）    │ 我们不在结构化索引里       │
  │                        │                        │ → 平台侧要重新建索引       │
  ├────────────────────────┼────────────────────────┼──────────────────────────┤
  │ OK                     │ 失败（6.3 对照也失败）  │ filtered 链路整体坏了      │
  │                        │                        │ → 平台侧修检索服务         │
  ├────────────────────────┼────────────────────────┼──────────────────────────┤
  │ OK                     │ OK                     │ 两层索引都正常             │
  │                        │                        │ → 问题在叮当的编排/匹配层  │
  ├────────────────────────┼────────────────────────┼──────────────────────────┤
  │ 失败                   │ 失败                   │ 索引真的没有我们           │
  │                        │                        │ → 需要重新提交 ACS         │
  └────────────────────────┴────────────────────────┴──────────────────────────┘

  另外：
  - 第 1~5、7 节全 OK  → 我们的服务、证书、协议、注册都没问题
  - 第 8 节的来源 IP 如果是 <平台出口IP>，说明梧桐确实来调过我们
EOF

printf '\n############ 报告结束 ############\n'
