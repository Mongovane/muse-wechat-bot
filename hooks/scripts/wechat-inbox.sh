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
  # --- 1a. stuck detection: alive but not polling? ---
  # buf.txt is rewritten on EVERY getupdates return (<= ~40s when healthy,
  # even with zero messages). receiver.log is silent during healthy idle, so
  # log mtime is NOT a stuck signal (2026-09-30 12:15: receiver stuck 29 min
  # inside a hung POST, misjudged as "quiet period" until manual kill).
  _now="$(date +%s)"
  _buf_mtime="$(stat -c %Y "$BOT_DIR/buf.txt" 2>/dev/null || echo 0)"
  if [ "$((_now - _buf_mtime))" -gt 180 ]; then
    _stuck=1
    for _pid in $(pgrep -f "[w]xbot.py receive" || true); do
      _etimes="$(ps -o etimes= -p "$_pid" 2>/dev/null | tr -d ' ')"
      # a freshly started receiver hasn't had time to rewrite buf.txt yet
      if [ -n "$_etimes" ] && [ "$_etimes" -lt 180 ]; then _stuck=0; fi
    done
    if [ "$_stuck" -eq 1 ]; then
      log "receiver stuck (buf.txt stale $((_now - _buf_mtime))s), restarting" '{}'
      _pids="$(pgrep -f "[w]xbot.py receive" || true)"
      if [ -n "$_pids" ]; then kill $_pids 2>/dev/null || true; fi
      sleep 3
      _pids="$(pgrep -f "[w]xbot.py receive" || true)"
      if [ -n "$_pids" ]; then kill -9 $_pids 2>/dev/null || true; sleep 1; fi
      if ! pgrep -f "[w]xbot.py receive" >/dev/null 2>&1; then
        cd "$BOT_DIR"
        cp -f receiver.log receiver.log.prev 2>/dev/null || true
        setsid nohup "$BOT_PYTHON" wxbot.py receive \
          > receiver.log 2>&1 < /dev/null &
        sleep 2
        # dedup: the setsid/nohup dance has left 2 same-name instances before;
        # keep the oldest, kill the rest
        _pids="$(pgrep -f "[w]xbot.py receive" || true)"
        if [ "$(echo "$_pids" | wc -w)" -gt 1 ]; then
          _keep="$(echo "$_pids" | head -n 1)"
          for _p in $_pids; do
            if [ "$_p" != "$_keep" ]; then kill -9 "$_p" 2>/dev/null || true; fi
          done
          log "receiver dedup after stuck-restart: kept $_keep" '{}'
        fi
        log "receiver restarted after stuck" '{}'
      else
        log "receiver stuck but refusing to die, left for cron" '{}'
      fi
    fi
    unset _now _buf_mtime _stuck _pid _etimes _pids _keep _p
  fi
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
    # Skip ack on watchdog requeue (user already got one at 30s on first attempt).
    noack="$(jq -r '.watchdog_noack // false' "$BOT_DIR/processing/$id.json" 2>/dev/null)" || noack="false"
    if [ "$noack" != "true" ]; then
      setsid nohup bash "$BOT_DIR/ack_timer.sh" "$id" "$to" "$ctx" 30 >/dev/null 2>&1 < /dev/null &
    fi
    # Fast-fail watchdog: requeue to inbox if the worker never starts (no .alive in 75s).
    setsid nohup bash "$BOT_DIR/watchdog.sh" "$BOT_DIR" "$id" >/dev/null 2>&1 < /dev/null &
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
