#!/usr/bin/env bash
# wechat-inbox: event-driven trigger for the WeChat bot.
# - watchdog: keep `wxbot.py receive` alive (restart in bash, no agent needed)
# - inbox: wake a worker the moment new message files appear
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BOT_DIR="${WECHAT_BOT_DIR:-$HOME/workspace/wechat-bot}"
BOT_PYTHON="${WECHAT_BOT_PYTHON:-$HOME/workspace/CowAgent/venv/bin/python}"
STATE_DIR="$HOME/hooks/state"
RELOGIN_FLAG="$STATE_DIR/wechat-bot-relogin.flag"
mkdir -p "$STATE_DIR"

# --- 1. watchdog: is the receiver alive? ---
if ! pgrep -f "[w]xbot.py receive" >/dev/null 2>&1; then
  if tail -n 20 "$BOT_DIR/receiver.log" 2>/dev/null | grep -q "SESSION_EXPIRED"; then
    # WeChat login expired server-side; restarting won't help. Notify once.
    if [ -f "$RELOGIN_FLAG" ]; then
      silent "receiver down, session expired, user already notified" '{}'
    else
      touch "$RELOGIN_FLAG"
      wake "微信登录态过期（SESSION_EXPIRED），需要用户重新扫码登录" '{"need_relogin":true}'
    fi
  else
    cd "$BOT_DIR"
    setsid nohup "$BOT_PYTHON" wxbot.py receive \
      > receiver.log 2>&1 < /dev/null &
    log "receiver restarted" '{}'
    # fall through to inbox check below
  fi
else
  # receiver 活着 = 登录态有效，清除"已通知"标记，下次过期可再次提醒
  rm -f "$RELOGIN_FLAG"
fi

# --- 1b. send-side session expiry (marker dropped by wxbot.py send on ret=-14) ---
if [ -f "$BOT_DIR/.send_session_expired" ] && [ ! -f "$RELOGIN_FLAG" ]; then
  touch "$RELOGIN_FLAG"
  wake "微信发送侧登录态过期（send 返回 -14），需要用户重新扫码登录" '{"need_relogin":true}'
fi

# --- 2. inbox: any new messages? ---
shopt -s nullglob
files=( "$BOT_DIR"/inbox/*.json )
if [ "${#files[@]}" -eq 0 ]; then
  silent "receiver ok, inbox empty" '{}'
else
  ids=$(printf '%s\n' "${files[@]}" | xargs -n1 basename | sed 's/\.json$//' | jq -R . | jq -s -c .)
  wake "inbox has ${#files[@]} new message(s)" "{\"msg_ids\":$ids}"
fi
