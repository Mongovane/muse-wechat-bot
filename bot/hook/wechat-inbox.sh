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
    # preserve pre-restart log so the next crash leaves evidence (was truncated before)
    cp -f receiver.log receiver.log.prev 2>/dev/null || true
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

# --- 2. inbox: claim in bash (atomic mv), start ack, then wake ---
# 认领下沉到脚本：worker 不再自己 mv，省一次模型往返。
# ack 阈值 30s = 冷启动 ~14s + 约 15s 工作时间；超时未写好回复才提示。
shopt -s nullglob
files=( "$BOT_DIR"/inbox/*.json )
if [ "${#files[@]}" -eq 0 ]; then
  silent "receiver ok, inbox empty" '{}'
else
  claimed=()
  for f in "${files[@]}"; do
    id="$(basename "$f" .json)"
    mv "$f" "$BOT_DIR/processing/$id.json" 2>/dev/null || continue
    claimed+=("$id")
    now="$(date +%s)"
    jq --argjson ts "$now" '.claim_ts = $ts' \
      "$BOT_DIR/processing/$id.json" > "$BOT_DIR/processing/$id.json.tmp" \
      && mv "$BOT_DIR/processing/$id.json.tmp" "$BOT_DIR/processing/$id.json"
    to="$(jq -r '.from // .to // ""' "$BOT_DIR/processing/$id.json" 2>/dev/null)" || to=""
    ctx="$(jq -r '.context_token // ""' "$BOT_DIR/processing/$id.json" 2>/dev/null)" || ctx=""
    setsid nohup bash "$BOT_DIR/ack_timer.sh" "$id" "$to" "$ctx" 30 >/dev/null 2>&1 < /dev/null &
  done
  if [ "${#claimed[@]}" -eq 0 ]; then
    silent "inbox raced, nothing claimed" '{}'
  else
    payload="$(python3 - "$BOT_DIR" "${claimed[@]}" <<'PYEOF'
import json, os, sys
bot, ids = sys.argv[1], sys.argv[2:]
msgs = {}
for i in ids:
    try:
        d = json.load(open(os.path.join(bot, "processing", i + ".json")))
    except (OSError, ValueError):
        continue
    msgs[i] = {"from": d.get("from"), "text": d.get("text", ""),
               "image": d.get("image", ""),
               "context_token": d.get("context_token")}
print(json.dumps({"msg_ids": ids, "messages": msgs}, ensure_ascii=False))
PYEOF
)"
    wake "inbox has ${#claimed[@]} new message(s), claimed" "$payload"
  fi
fi
