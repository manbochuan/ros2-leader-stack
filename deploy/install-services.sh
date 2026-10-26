#!/usr/bin/env bash
# ============================================================
#  把 bailian-shim 和 ACPs 智能体装成 systemd 常驻服务
#
#  用法（在服务器上，root）：
#      sudo bash install-services.sh
#
#  幂等：可以重复执行
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SHIM_DIR=/opt/acps-agent/bailian-shim
AGENT_DIR=/opt/acps-agent/acps-community/demo-partner

ok()   { printf '\033[32m  [OK] %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  [!!] %s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行： sudo bash $0"
  exit 1
fi

step "1/5 检查前置文件"
fail=0
[[ -x "$SHIM_DIR/venv/bin/uvicorn" ]] && ok "shim 的 venv" || { bad "缺 $SHIM_DIR/venv/bin/uvicorn"; fail=1; }
[[ -f "$SHIM_DIR/.env" ]] && ok "shim 的 .env" || { bad "缺 $SHIM_DIR/.env"; fail=1; }
[[ -x "$AGENT_DIR/.venv/bin/python" ]] && ok "demo-partner 的 venv" || { bad "缺 $AGENT_DIR/.venv/bin/python"; fail=1; }
[[ -f "$AGENT_DIR/.env" ]] && ok "demo-partner 的 .env" || { bad "缺 $AGENT_DIR/.env"; fail=1; }
[[ -f "$AGENT_DIR/partners/online/ros2_tutor/config.toml" ]] && ok "ros2_tutor 配置" || { bad "缺 ros2_tutor 配置"; fail=1; }
[[ -f "$HERE/acps-agent.service" && -f "$HERE/bailian-shim.service" ]] && ok "unit 文件" || { bad "unit 文件不全"; fail=1; }

if [[ $fail -ne 0 ]]; then
  echo
  echo "前置检查没通过，已中止——什么都没改。"
  exit 1
fi

step "2/5 停掉手工 nohup 起的进程"
if pkill -f "uvicorn shim:app" 2>/dev/null; then ok "已停旧 shim"; else ok "shim 本来就没在跑"; fi
if pkill -f "partners.main" 2>/dev/null; then ok "已停旧智能体"; else ok "智能体本来就没在跑"; fi
sleep 2

step "3/5 安装 unit 文件"
install -m 644 "$HERE/bailian-shim.service" /etc/systemd/system/bailian-shim.service && ok "bailian-shim.service"
install -m 644 "$HERE/acps-agent.service"  /etc/systemd/system/acps-agent.service  && ok "acps-agent.service"

step "4/5 启用并启动"
systemctl daemon-reload
if systemctl enable --now bailian-shim.service >/dev/null 2>&1; then ok "bailian-shim 已启动并设为开机自启"; else bad "bailian-shim 启动失败"; fi
sleep 3
if systemctl enable --now acps-agent.service >/dev/null 2>&1; then ok "acps-agent 已启动并设为开机自启"; else bad "acps-agent 启动失败"; fi
sleep 6

step "5/5 体检"
for s in bailian-shim acps-agent; do
  state=$(systemctl is-active "$s" 2>/dev/null || true)
  enabled=$(systemctl is-enabled "$s" 2>/dev/null || true)
  printf '  %-14s 运行状态=%-8s 开机自启=%s\n' "$s" "${state:-未知}" "${enabled:-未知}"
done

echo
echo "  端口监听："
ss -lntp 2>/dev/null | grep -E ':(8443|9099)' || bad "没有监听 8443/9099"

echo
echo "  健康检查："
printf '    8443 -> '
curl -s -m 5 http://127.0.0.1:8443/health || printf '无响应'
echo
printf '    9099 -> '
curl -s -m 5 http://127.0.0.1:9099/health | head -c 220 || printf '无响应'
echo

cat <<'EOF'

=========== 完成 ===========

常用命令：
  systemctl status acps-agent         查看智能体状态
  systemctl status bailian-shim       查看 shim 状态
  systemctl restart acps-agent        重启智能体
  journalctl -u acps-agent -f         实时看智能体日志（替代 tail -f /tmp/partner.log）
  journalctl -u bailian-shim -n 50    看 shim 最近 50 行

两个服务都设了 Restart=always：进程崩了会在 3~5 秒后自动拉起。
EOF
