#!/usr/bin/env bash
# wechat-sweeper-gate: 兜底清扫的门控（2026-10-16 起，替代每 3 分钟的 agent cron）。
# - 看门狗（bash 内完成，不唤醒 agent）：接收器死了 → 重启；卡死（buf.txt > 3 分钟
#   未更新且进程已运行 > 3 分钟）→ kill 重启。逻辑与 wechat-inbox.sh 相同。
# - 唤醒条件（只在真有漏网时唤醒 agent）：inbox 有 > 1 分钟未处理的文件；
#   processing 认领超时（有 .alive 给 10 分钟，无 .alive 给 2 分钟）；
#   登录态过期（接收侧 SESSION_EXPIRED 且接收器已死；发送侧 -14 标记）。
# - proactive 的 outbox 文件单独存在不是唤醒条件（flash/早报/卡片 worker 自己发，
#   兜底不碰）；只有 processing 超时的单才唤醒（有 outbox → finish，无 outbox → 重处理）。
# - 去重：接收侧过期用共享 relogin flag（接收器死时 5 秒 hook 不清它，无循环）；
#   发送侧 -14 用 gate 本地 mtime 去重（marker mtime 未变不重复唤醒，永不循环）。
# - dry-run（HATCH_HOOK_DRY_RUN=1）时不做任何修改性动作（不重启、不 kill、不写
#   任何标记/状态文件），只汇报决策。
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BOT_DIR="${WECHAT_BOT_DIR:-$HOME/workspace/wechat-bot}"
BOT_PYTHON="${WECHAT_BOT_PYTHON:-$HOME/workspace/CowAgent/venv/bin/python}"
STATE_DIR="${HOOKS_STATE_DIR:-$HOME/hooks/state}"
RELOGIN_FLAG="$STATE_DIR/wechat-bot-relogin.flag"
GATE_SEND_NOTIFIED="$STATE_DIR/wechat-sweeper-gate-send-expired.mtime"
DRY="${HATCH_HOOK_DRY_RUN:-0}"
mkdir -p "$STATE_DIR"

now="$(date +%s)"
reasons=()
payload_session_expired="false"
payload_send_expired="false"

# --- 1. 看门狗：接收器是否存活（bash 内处理） ---
if ! pgrep -f "[w]xbot.py receive" >/dev/null 2>&1; then
  if tail -n 20 "$BOT_DIR/receiver.log" 2>/dev/null | grep -q "SESSION_EXPIRED"; then
    payload_session_expired="true"
    if [ -f "$RELOGIN_FLAG" ]; then
      silent "receiver down, session expired, user already notified" '{}'
      exit 0
    else
      if [ "$DRY" != "1" ]; then touch "$RELOGIN_FLAG"; fi
      wake "微信登录态过期（SESSION_EXPIRED），需要用户重新扫码登录" '{"need_relogin":true}'
      exit 0
    fi
  else
    if [ "$DRY" = "1" ]; then
      log "dry-run: receiver down, restart suppressed" '{}'
    else
      cd "$BOT_DIR"
      cp -f receiver.log receiver.log.prev 2>/dev/null || true
      setsid nohup "$BOT_PYTHON" wxbot.py receive \
        > receiver.log 2>&1 < /dev/null &
      log "receiver restarted" '{}'
    fi
    # fall through to queue checks below
  fi
else
  # 接收器活着 = 登录态有效，清除"已通知"标记，下次过期可再次提醒
  # （与 wechat-inbox.sh 相同语义；注意：不要在这里查日志里的 SESSION_EXPIRED——
  # 活着的接收器每轮清 flag，日志残留行会导致每轮重复唤醒。死接收器的情况上面已处理。）
  if [ "$DRY" != "1" ]; then rm -f "$RELOGIN_FLAG"; fi
  # 卡死检测：buf.txt 健康时每次长轮询返回（<= ~40s）都会重写；receiver.log
  # 空闲静默是正常的，绝不能拿它的 mtime 判卡死。
  _buf_mtime="$(stat -c %Y "$BOT_DIR/buf.txt" 2>/dev/null || echo 0)"
  if [ "$((now - _buf_mtime))" -gt 180 ]; then
    _stuck=1
    for _pid in $(pgrep -f "[w]xbot.py receive" || true); do
      _etimes="$(ps -o etimes= -p "$_pid" 2>/dev/null | tr -d ' ')"
      if [ -n "$_etimes" ] && [ "$_etimes" -lt 180 ]; then _stuck=0; fi
    done
    if [ "$_stuck" -eq 1 ]; then
      if [ "$DRY" = "1" ]; then
        log "dry-run: receiver stuck (buf.txt stale $((now - _buf_mtime))s), restart suppressed" '{}'
      else
        log "receiver stuck (buf.txt stale $((now - _buf_mtime))s), restarting" '{}'
        _pids="$(pgrep -f "[w]xbot.py receive" || true)"
        if [ -n "$_pids" ]; then kill $_pids 2>/dev/null || true; fi
        sleep 3
        _pids="$(pgrep -f "[w]xbot.py receive" || true)"
        if [ -n "$_pids" ]; then kill -9 "$_pids" 2>/dev/null || true; sleep 1; fi
        if ! pgrep -f "[w]xbot.py receive" >/dev/null 2>&1; then
          cd "$BOT_DIR"
          cp -f receiver.log receiver.log.prev 2>/dev/null || true
          setsid nohup "$BOT_PYTHON" wxbot.py receive \
            > receiver.log 2>&1 < /dev/null &
          sleep 2
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
          log "receiver stuck but refusing to die, left for next poll" '{}'
        fi
      fi
    fi
    unset _buf_mtime _stuck _pid _etimes _pids _keep
  fi
fi

# --- 2. 发送侧登录态过期（send 返回 -14 的标记；5 秒 hook 也会查，这里是备用） ---
# gate 本地 mtime 去重：同一 marker 只唤醒一次；新过期事件（marker mtime 变了）再唤醒。
if [ -f "$BOT_DIR/.send_session_expired" ]; then
  _marker_mtime="$(stat -c %Y "$BOT_DIR/.send_session_expired" 2>/dev/null || echo 0)"
  _notified_mtime="$(cat "$GATE_SEND_NOTIFIED" 2>/dev/null || echo "")"
  if [ "$_notified_mtime" != "$_marker_mtime" ]; then
    payload_send_expired="true"
    if [ "$DRY" != "1" ]; then echo "$_marker_mtime" > "$GATE_SEND_NOTIFIED"; fi
    reasons+=("send-side session expired (.send_session_expired)")
  fi
  unset _marker_mtime _notified_mtime
fi

# --- 3. 漏网：inbox 里超过 1 分钟未处理的文件 ---
stale_inbox=()
shopt -s nullglob
for _f in "$BOT_DIR"/inbox/*.json; do
  _mt="$(stat -c %Y "$_f" 2>/dev/null || echo "$now")"
  if [ "$((now - _mt))" -gt 60 ]; then
    stale_inbox+=("$(basename "$_f" .json)")
  fi
done
if [ "${#stale_inbox[@]}" -gt 0 ]; then
  reasons+=("stale inbox: ${stale_inbox[*]}")
fi

# --- 4. 漏网：processing 里超时的认领 ---
# 有 .alive = worker 已启动，给 10 分钟；无 .alive = worker 没起来，给 2 分钟。
# 超时起点用 claim_ts（hook 认领时写入），缺失则退回文件 mtime。
timed_out=()
for _p in "$BOT_DIR"/processing/*.json; do
  [ -e "$_p" ] || continue
  _id="$(basename "$_p" .json)"
  _cts="$(jq -r '.claim_ts // 0' "$_p" 2>/dev/null || echo 0)"
  if [ -z "$_cts" ] || [ "$_cts" = "0" ] || [ "$_cts" = "null" ]; then
    _cts="$(stat -c %Y "$_p" 2>/dev/null || echo "$now")"
  fi
  _age="$((now - _cts))"
  if [ -f "$BOT_DIR/processing/${_id}.alive" ]; then _limit=600; else _limit=120; fi
  if [ "$_age" -gt "$_limit" ]; then
    timed_out+=("$_id")
  fi
done
if [ "${#timed_out[@]}" -gt 0 ]; then
  reasons+=("timed-out processing: ${timed_out[*]}")
fi

# --- 5. 决策：有漏网才唤醒 ---
if [ "${#reasons[@]}" -gt 0 ]; then
  _payload="$(python3 -c 'import json,sys; print(json.dumps({"stale_inbox": sys.argv[1].split() if sys.argv[1] else [], "timed_out_processing": sys.argv[2].split() if sys.argv[2] else [], "session_expired": sys.argv[3]=="true", "send_session_expired": sys.argv[4]=="true"}))' "${stale_inbox[*]}" "${timed_out[*]}" "$payload_session_expired" "$payload_send_expired")"
  wake "sweeper: ${reasons[*]}" "$_payload"
else
  silent "receiver ok, no stale inbox/processing" '{}'
fi
