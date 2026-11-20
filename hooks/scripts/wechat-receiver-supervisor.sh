#!/usr/bin/env bash
# wechat-receiver-supervisor: OS-level backstop for hook §1 watchdog.
# 2026-11-20: 10:40–10:43 executor draining 导致 hook 轮询停摆约 3.5 分钟，
# 接收器 10:39:57 死后（ilink getupdates ProxyError）无人重启，直到轮询恢复
# 才在 10:43:30 重拉。常驻循环每 30 秒检查一次接收器进程，死了就按 §1
# 同款办法重拉——它是普通 OS 进程，不走 runtime executor，轮询停摆时照常工作。
# 只管“进程死了”；SESSION_EXPIRED 时不拉（登录过期重拉也没用，等人工扫码）。
# 与 hook §1 的两次重拉共用一把 flock，防两边同时拉起两个接收器。
# 由 hook §1g 常驻：每轮确认它活着，死了就重拉；VM 换机后 hook 首轮重拉。
set -uo pipefail

BOT_DIR="${WECHAT_BOT_DIR:-$HOME/workspace/wechat-bot}"
BOT_PYTHON="${WECHAT_BOT_PYTHON:-$HOME/workspace/CowAgent/venv/bin/python}"
STATE_DIR="$HOME/hooks/state"
LOCK="$STATE_DIR/receiver-restart.lock"
LOG="$STATE_DIR/receiver-supervisor.log"
# 测试用覆盖（默认生产值）：轮询间隔、接收器进程匹配式。watchdog.sh 同款做法。
INTERVAL="${WECHAT_RECEIVER_SUPERVISOR_INTERVAL:-30}"
PATTERN="${WECHAT_RECEIVER_PATTERN:-[w]xbot.py receive}"
mkdir -p "$STATE_DIR"

while true; do
  if ! pgrep -f "$PATTERN" >/dev/null 2>&1; then
    (
      flock -n 9 || exit 0
      pgrep -f "$PATTERN" >/dev/null 2>&1 && exit 0
      if tail -n 20 "$BOT_DIR/receiver.log" 2>/dev/null | grep -q "SESSION_EXPIRED"; then
        exit 0
      fi
      cd "$BOT_DIR" || exit 0
      cp -f receiver.log receiver.log.prev 2>/dev/null || true
      setsid nohup "$BOT_PYTHON" wxbot.py receive > receiver.log 2>&1 < /dev/null &
      echo "$(date '+%F %T') supervisor restarted receiver" >> "$LOG"
    ) 9>"$LOCK"
  fi
  sleep "$INTERVAL"
done
