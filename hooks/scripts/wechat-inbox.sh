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
    # WeChat login expired server-side; restarting won't help.
    # 2026-11-19：告警闭环——首次报一次，之后每小时复报一次（wxbot.py alert
    # token failing 的状态机决定）；flag 照常 touch，供 sweeper 去重。
    _alert="$(cd "$BOT_DIR" && "$BOT_PYTHON" wxbot.py alert token failing 2>/dev/null || echo ALERT_SEND)"
    touch "$RELOGIN_FLAG"
    if [ "$_alert" = "ALERT_SEND" ]; then
      wake "微信登录态过期（SESSION_EXPIRED），需要用户重新扫码登录" '{"need_relogin":true}'
    else
      silent "receiver down, session expired, alert suppressed ($_alert)" '{}'
    fi
  else
    # 与常驻 receiver-supervisor.sh 互斥（flock），防两边同时拉起两个接收器。
    ( flock -n 9 || exit 0
      pgrep -f "[w]xbot.py receive" >/dev/null 2>&1 && exit 0
      cd "$BOT_DIR"
      # preserve pre-restart log so the next crash leaves evidence (was truncated before)
      cp -f receiver.log receiver.log.prev 2>/dev/null || true
      setsid nohup "$BOT_PYTHON" wxbot.py receive \
        > receiver.log 2>&1 < /dev/null &
      log "receiver restarted" '{}'
    ) 9>"$STATE_DIR/receiver-restart.lock"
    # fall through to inbox check below
  fi
else
  # receiver 活着 = 登录态有效；之前若报过过期，且过期条件已消失
  #（发送侧 marker 没了、日志里也没有 SESSION_EXPIRED），报一次恢复。
  if [ -f "$RELOGIN_FLAG" ]; then
    rm -f "$RELOGIN_FLAG"
    if [ ! -f "$BOT_DIR/.send_session_expired" ] && ! tail -n 20 "$BOT_DIR/receiver.log" 2>/dev/null | grep -q "SESSION_EXPIRED"; then
      _rec="$(cd "$BOT_DIR" && "$BOT_PYTHON" wxbot.py alert token recovered 2>/dev/null || echo ALERT_NONE)"
      if [ "$_rec" = "ALERT_RECOVERED" ]; then
        wake "微信登录已恢复（SESSION_EXPIRED 解除）" '{"recovered":true}'
      fi
    fi
  fi
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
        # 与常驻 receiver-supervisor.sh 互斥（flock），防两边同时拉起两个接收器。
        ( flock -n 9 || exit 0
          pgrep -f "[w]xbot.py receive" >/dev/null 2>&1 && exit 0
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
        ) 9>"$STATE_DIR/receiver-restart.lock"
      else
        log "receiver stuck but refusing to die, left for cron" '{}'
      fi
    fi
    unset _now _buf_mtime _stuck _pid _etimes _pids _keep _p
  fi
fi

# --- 1b. send-side session expiry (marker dropped by wxbot.py send on ret=-14) ---
# 2026-11-19：marker 只在 -14 连续超 1 小时才落下（stale 窗口内不落），
# 这里再过一遍告警状态机：首次报一次，之后每小时复报（旧逻辑是 flag
# 去重，但 receiver 活着时 §1 会每轮清 flag，旧逻辑会每 5 秒重复唤醒）。
if [ -f "$BOT_DIR/.send_session_expired" ]; then
  _alert="$(cd "$BOT_DIR" && "$BOT_PYTHON" wxbot.py alert token failing 2>/dev/null || echo ALERT_SEND)"
  touch "$RELOGIN_FLAG"
  if [ "$_alert" = "ALERT_SEND" ]; then
    wake "微信发送侧登录态过期（send 返回 -14），需要用户重新扫码登录" '{"need_relogin":true}'
  else
    silent "send-side session expired, alert suppressed ($_alert)" '{}'
  fi
fi

# --- 1c. hook-side auto-finish: worker died after writing the reply? ---
# 协议（2026-10-04）：worker 先写 outbox/<id>.json.tmp，写完 mv 成
# outbox/<id>.json（原子发布，文件一出现就是完整的），每次写完都 touch
# processing/<id>.alive 告诉看门狗"我还活着"。
# 代发条件：outbox/<id>.json 存在（mtime > 5s，防没走协议的直接写）+
# worker 已死（无 .alive，或 .alive 超过 30 秒没更新）。
# claim_ts 归属校验 + 发送锁保证：即使 worker 还活着，也不会重发。
# 尝试后 touch outbox 续命，失败时自然退避，不会 5 秒空转一次。
# 另：outbox 有但 processing 没了 + sent/ 有记录 = 真孤儿（已发出），直接删，
# 防以后被某次 wxbot.py send 误发。
_now="$(date +%s)"
# 顺手清理写了一半就没人管的 tmp（30 分钟前的）
# inbox 也要清：接收器原子写 inbox/<mid>.json 时用的 <path>.<pid>.tmp
find "$BOT_DIR/outbox" "$BOT_DIR/inbox" -name '*.tmp' -mmin +30 -delete 2>/dev/null || true
for _proc in "$BOT_DIR"/processing/*.json; do
  [ -e "$_proc" ] || continue
  _id="$(basename "$_proc" .json)"
  _ob="$BOT_DIR/outbox/${_id}.json"
  [ -f "$_ob" ] || continue
  _ob_mtime="$(stat -c %Y "$_ob" 2>/dev/null || echo "$_now")"
  _al="$BOT_DIR/processing/${_id}.alive"
  _al_mtime="$(stat -c %Y "$_al" 2>/dev/null || echo 0)"
  # 2026-11-19：worker 卡住超 10 分钟（回复已写 10 分钟未发、.alive 也
  # 10 分钟没更新）→ 告警闭环（状态机：满 10 分钟才报，之后每小时
  # 最多一次）。只告警，回收仍由 watchdog.sh / sweeper 做，不在本轮 mv。
  if [ "$((_now - _ob_mtime))" -gt 600 ] && { [ ! -f "$_al" ] || [ "$((_now - _al_mtime))" -gt 600 ]; }; then
    _walert="$(cd "$BOT_DIR" && "$BOT_PYTHON" wxbot.py alert worker failing 2>/dev/null || echo ALERT_NONE)"
    if [ "$_walert" = "ALERT_SEND" ]; then
      wake "微信 worker 卡住超 10 分钟（${_id}），等兜底处理" "{\"stuck_id\":\"${_id}\"}"
    fi
  fi
  if [ "$((_now - _ob_mtime))" -gt 5 ] && { [ ! -f "$_al" ] || [ "$((_now - _al_mtime))" -gt 30 ]; }; then
    _cts="$(jq -r '.claim_ts // ""' "$_proc" 2>/dev/null || true)"
    if [ -n "$_cts" ]; then
      log "hook auto-finish ${_id} (outbox $((_now - _ob_mtime))s, alive $((_now - _al_mtime))s)" '{}'
      (cd "$BOT_DIR" && "$BOT_PYTHON" "$BOT_DIR/wxbot.py" finish "$_id" "$_cts" \
        >>"$STATE_DIR/wechat-hook-finish.log" 2>&1 || true)
      [ -f "$_ob" ] && touch "$_ob"  # 还在 = 没发出去，续命后下次再试
    fi
  fi
done
# 真孤儿清理：processing 没了、outbox 还在、sent/ 有记录（已发出）
for _ob in "$BOT_DIR"/outbox/*.json; do
  [ -e "$_ob" ] || continue
  _id="$(basename "$_ob" .json)"
  if [ ! -f "$BOT_DIR/processing/${_id}.json" ] && [ -f "$BOT_DIR/sent/${_id}.json" ]; then
    log "hook cleanup orphan outbox ${_id} (already sent)" '{}'
    rm -f "$_ob" "$BOT_DIR/outbox/${_id}.parts.json"
  fi
done
unset _now _proc _id _ob _ob_mtime _al _al_mtime _cts _walert
# 2026-11-19：processing 已排空且之前报过 worker 卡住 → 报一次恢复。
if ! ls "$BOT_DIR"/processing/*.json >/dev/null 2>&1; then
  _wrec="$(cd "$BOT_DIR" && "$BOT_PYTHON" wxbot.py alert worker recovered 2>/dev/null || echo ALERT_NONE)"
  if [ "$_wrec" = "ALERT_RECOVERED" ]; then
    wake "微信 worker 卡住已恢复（队列排空）" '{"recovered":true}'
  fi
fi

# --- 1f. resident supervisor（2026-11-20 第二轮）：warm-wait 归 hook 所有。
# worker 回完只写 warm-request.json 就退出，再也不自己跑 warm-wait（worker
# 会话结束时 runtime 会清掉它名下的后台进程，background=true + setsid 三轮
# 实测仍在 5 秒～4.5 分钟内死光）。hook 用起接收器同款办法（setsid nohup，
# 见 §1）拉起 warm-wait，接收器能活几天，warm-wait 就能活几天。
# 本节两件事：① 消费 warm-claim-<id>.json（warm-wait 认领后写的 payload），
# 校验全部 msg_id 还在 processing/ 里才唤醒 worker（防 watchdog 已重排导致
# 的重复唤醒）；唤醒前先把文件改名 .consumed（wake() 会 exit，成功后本轮
# 不再回来；失败则文件已标记，靠 watchdog 兜底重排）。② 保证常驻 warm-wait
# 活着（死了且会话未完就重拉；有 warm-request 就按它起）。
# 注意：wake()/silent() 会 exit，本节的唤醒必须是本轮最后一个动作。
_warm_pid_alive() {
  [ -f "$BOT_DIR/warm.json" ] || return 1
  _wpid="$(python3 -c "import json; print(json.load(open('$BOT_DIR/warm.json')).get('pid', ''))" 2>/dev/null || echo "")"
  _whb="$(python3 -c "import json,time; print(int(time.time() - json.load(open('$BOT_DIR/warm.json')).get('heartbeat', 0)))" 2>/dev/null || echo 999)"
  [ -n "$_wpid" ] && [ "$_whb" -lt 15 ] && kill -0 "$_wpid" 2>/dev/null
}
_warm_proc_alive() {
  for _p in $(pgrep -f "[w]xbot.py warm-wait --resident" 2>/dev/null || true); do
    if [ "$(readlink "/proc/$_p/cwd" 2>/dev/null || echo "")" = "$BOT_DIR" ]; then
      return 0
    fi
  done
  return 1
}
_warm_spawn() {
  _cap="$1"
  # 2026-11-20 11:17/11:25：subshell 形 ( ... & )（无论 flock/ trailing log /
  # exit 0 还是 || true）都会让 daemon 变成主脚本的 job，stdout 是 pipe 时
  # 主脚本退出会在 do_wait 里等它（实测两轮各挂数分钟；/tmp 无 subreaper
  # 时不复现）。改成主脚本直接后台 + disown：job 从表里摘掉，bash 退出
  # 不再等它；setsid 已脱离进程组；cd 保证 daemon 的 cwd 是 BOT_DIR（hook
  # 全用绝对路径，改自身 cwd 无害；9>&- 防 daemon 继承 flock 锁 FD）。
  cd "$BOT_DIR" && {
    setsid nohup "$BOT_PYTHON" wxbot.py warm-wait --resident --cap-secs "$_cap" \
      > warm-wait.log 2>&1 < /dev/null 9>&- &
    disown
    log "resident warm-wait spawned (cap ${_cap}s)" '{}'
  }
  return 0
}
# 常驻剩余秒数（2026-11-20）：以 warm-resident.json 的 until 为准。
# warm.json 会被 worker 自跑的非 resident warm-wait 改写成 start+1800，
# 按它钳制会把常驻会话提前掐断（实测：cap 被钳到 1648 秒，会话 16:25
# 提前结束）。账本没有 until（老账本）时回落 0，调用方再按 warm.json 算。
_warm_resident_rem() {
  python3 -c "import json,time; print(int(json.load(open('$BOT_DIR/warm-resident.json')).get('until', 0) - time.time()))" 2>/dev/null || echo 0
}
shopt -s nullglob
_wc_files=( "$BOT_DIR"/warm-claim-*.json )
if [ "${#_wc_files[@]}" -gt 0 ]; then
  _wc_oldest="$(ls -t "${_wc_files[@]}" 2>/dev/null | tail -n 1)"
  _wc_ok="$(python3 - "$BOT_DIR" "$_wc_oldest" <<'PYEOF'
import json, os, sys
bot, path = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        p = json.load(f)
except (OSError, ValueError):
    print("BAD")
    sys.exit(0)
ids = p.get("msg_ids") or []
ok = bool(ids) and all(os.path.exists(os.path.join(bot, "processing", i + ".json")) for i in ids)
print("OK" if ok else "STALE")
PYEOF
)"
  if [ "$_wc_ok" = "OK" ]; then
    # 先把替代 warm-wait 拉起来（会话未完时），再唤醒——wake() 会 exit。
    if ! _warm_pid_alive && ! _warm_proc_alive; then
      _rem="$(_warm_resident_rem)"
      if [ "$_rem" -le 0 ]; then
        _rem="$(python3 -c "import json,time; print(int(json.load(open('$BOT_DIR/warm.json')).get('until', 0) - time.time()))" 2>/dev/null || echo 0)"
      fi
      if [ "$_rem" -gt 0 ] && [ "$_rem" -le 86400 ]; then
        _warm_spawn "$_rem"
      fi
    fi
    _wc_n="$(python3 -c "import json; print(len(json.load(open('$_wc_oldest')).get('msg_ids', [])))" 2>/dev/null || echo 1)"
    if mv "$_wc_oldest" "${_wc_oldest}.consumed" 2>/dev/null; then
      _wc_payload="$(cat "${_wc_oldest}.consumed" 2>/dev/null || echo "")"
      if [ -n "$_wc_payload" ]; then
        wake "warm claimed ${_wc_n} new message(s)" "$_wc_payload"
      fi
    fi
  else
    rm -f "$_wc_oldest"
  fi
fi
# 保证 warm-wait 活着（上面没唤醒时才到这里；唤醒走了 wake() 直接 exit）。
if _warm_pid_alive || _warm_proc_alive; then
  rm -f "$BOT_DIR/warm-request.json"
else
  _cap=0
  if [ -f "$BOT_DIR/warm-request.json" ]; then
    _cap="$(python3 -c "import json; print(int(json.load(open('$BOT_DIR/warm-request.json')).get('cap_secs', 0)))" 2>/dev/null || echo 0)"
    rm -f "$BOT_DIR/warm-request.json"
  fi
  _rem="$(_warm_resident_rem)"
  if [ "$_rem" -le 0 ] && [ -f "$BOT_DIR/warm.json" ]; then
    _rem="$(python3 -c "import json,time; print(int(json.load(open('$BOT_DIR/warm.json')).get('until', 0) - time.time()))" 2>/dev/null || echo 0)"
  fi
  if [ "$_rem" -gt 0 ] && { [ "$_cap" -le 0 ] || [ "$_rem" -lt "$_cap" ]; }; then _cap="$_rem"; fi
  if [ "$_cap" -gt 0 ] && [ "$_cap" -le 86400 ]; then
    _warm_spawn "$_cap"
  fi
fi
# 消费过的 claim 文件只留 1 小时备查，过期清掉。
find "$BOT_DIR" -maxdepth 1 -name 'warm-claim-*.json.consumed' -mmin +60 -delete 2>/dev/null || true
unset _wc_files _wc_oldest _wc_ok _wc_n _wc_payload _cap _rem _wpid _whb _p

# --- 1g. receiver supervisor ensure（2026-11-20 第二轮）：hook 轮询停摆时
# （executor draining，10:40–10:43 实测 3.5 分钟无轮询）§1 看门狗跑不起来，
# 接收器死了没人重启。用一个常驻 shell 循环兜底（wechat-receiver-supervisor.sh，
# 与接收器同款 setsid nohup 起法）；hook 每轮确认它活着，死了就重拉；VM 换机
# 后 hook 首轮重拉。它只管“进程死了”，SESSION_EXPIRED 时不拉（见脚本内）。
# 判活按 cwd 限定到本 BOT_DIR：全局 pgrep 会撞上别的 bot 目录的 supervisor
# （live 与沙箱测试各一个），误判“已活着”就不再拉。
_sup_running=0
for _sp in $(pgrep -f "[w]echat-receiver-supervisor" 2>/dev/null || true); do
  if [ "$(readlink "/proc/$_sp/cwd" 2>/dev/null || echo "")" = "$BOT_DIR" ]; then
    _sup_running=1
    break
  fi
done
if [ "$_sup_running" -eq 0 ]; then
  # 同 _warm_spawn：主脚本直接后台 + disown，防 hook 挂在 do_wait。
  cd "$BOT_DIR" && {
    WECHAT_BOT_DIR="$BOT_DIR" WECHAT_BOT_PYTHON="$BOT_PYTHON" setsid nohup bash "$HOME/hooks/scripts/wechat-receiver-supervisor.sh" >> "$STATE_DIR/receiver-supervisor.log" 2>&1 < /dev/null 9>&- &
    disown
    log "receiver supervisor spawned" '{}'
  }
fi
unset _sp _sup_running

# --- 1h. outbox watcher ensure（2026-11-20，发送提速）：watch-outbox 常驻
# 进程盯 outbox/，普通回复文件一出现就代发（worker 不再调 finish，省一次
# 模型动作 ~3.5s）。判活按 watch-outbox.json 心跳（<15s）+ pid，死了就
# 重拉（setsid nohup，同 _warm_spawn 起法）。死了期间回复仍由 §1c
# auto-finish 兜底，只是慢一点。判活按 cwd 限定到本 BOT_DIR。
_watch_pid_alive() {
  [ -f "$BOT_DIR/watch-outbox.json" ] || return 1
  _wopid="$(python3 -c "import json; print(json.load(open('$BOT_DIR/watch-outbox.json')).get('pid', ''))" 2>/dev/null || echo "")"
  _wohb="$(python3 -c "import json,time; print(int(time.time() - json.load(open('$BOT_DIR/watch-outbox.json')).get('heartbeat', 0)))" 2>/dev/null || echo 999)"
  [ -n "$_wopid" ] && [ "$_wohb" -lt 15 ] && kill -0 "$_wopid" 2>/dev/null
}
_watch_proc_alive() {
  for _wop in $(pgrep -f "[w]xbot.py watch-outbox" 2>/dev/null || true); do
    if [ "$(readlink "/proc/$_wop/cwd" 2>/dev/null || echo "")" = "$BOT_DIR" ]; then
      return 0
    fi
  done
  return 1
}
if ! _watch_pid_alive && ! _watch_proc_alive; then
  # 同 _warm_spawn：主脚本直接后台 + disown，防 hook 挂在 do_wait。
  cd "$BOT_DIR" && {
    setsid nohup "$BOT_PYTHON" wxbot.py watch-outbox \
      > watch-outbox.log 2>&1 < /dev/null 9>&- &
    disown
    log "outbox watcher spawned" '{}'
  }
fi
unset _wopid _wohb _wop

# --- 1d. warm-wait guard（2026-11-19）：worker 回完一轮后若主动跑了
# wxbot.py warm-wait（warm.json 心跳新鲜 <10s），本轮不认领 inbox，
# 留给 warm worker 在同一轮里直接处理，省一次唤醒 + 冷启动。
# warm worker 死掉（心跳过期）后 hook 自动恢复认领。
if [ -f "$BOT_DIR/warm.json" ]; then
  _warm_age="$(python3 -c "import json,time; print(int(time.time()-json.load(open('$BOT_DIR/warm.json')).get('heartbeat',0)))" 2>/dev/null || echo 999)"
  if [ "$_warm_age" -lt 10 ]; then
    silent "warm worker active (heartbeat ${_warm_age}s ago), skipping claim" '{}'
    exit 0
  fi
fi

# --- 1e. resident monitor（2026-11-20，第二轮补充）：常驻 warm-wait 死在空闲
# 时只记一笔，绝不唤醒（没消息可回，唤醒只烧额度）；§1f supervisor 会在会话
# 未完时重拉它，下一条消息走 hook 正常重生是最后兜底。判死放宽到 300 秒
# （思考最长实测约 264 秒，防思考中误报），且 processing 里没有 300 秒内
# 新鲜的 .alive——死在消息中途的走 watchdog 回收，这里不碰。同一轮心跳只记
# 一次（.resident-dead 存该心跳值）。本节只记日志，不复活。
if [ -f "$BOT_DIR/warm.json" ]; then
  _r_age="$(python3 -c "import json,time; print(int(time.time()-json.load(open('$BOT_DIR/warm.json')).get('heartbeat',0)))" 2>/dev/null || echo 0)"
  if [ "$_r_age" -ge 300 ]; then
    _r_alive="0"
    for _a in "$BOT_DIR"/processing/*.alive; do
      [ -e "$_a" ] || continue
      if [ "$(( $(date +%s) - $(stat -c %Y "$_a" 2>/dev/null || echo 0) ))" -lt 300 ]; then _r_alive="1"; break; fi
    done
    if [ "$_r_alive" = "0" ]; then
      _r_hb="$(python3 -c "import json; print(json.load(open('$BOT_DIR/warm.json')).get('heartbeat',''))" 2>/dev/null || echo ?)"
      if [ ! -f "$BOT_DIR/.resident-dead" ] || [ "$(cat "$BOT_DIR/.resident-dead" 2>/dev/null)" != "$_r_hb" ]; then
        echo "$(date '+%F %T') resident worker heartbeat stale ${_r_age}s (hb=$_r_hb), no live job; not waking (next message re-spawns via hook)" >> "$BOT_DIR/resident-monitor.log"
        echo "$_r_hb" > "$BOT_DIR/.resident-dead"
      fi
    fi
  fi
fi

# --- 2. inbox: claim in bash (atomic mv), then build payload in Python ---
# 认领下沉到脚本：worker 不再自己 mv，省一次模型往返。
# ack 阈值 30s = 冷启动 ~14s + 约 15s 工作时间；超时未写好回复才提示。
# 同用户连发归并：5 秒内同一个人先发图、后发问题，合并成一单（文本拼接、
# 图片继承），前序附属单直接归档 done/，避免两个 worker 各答各的。
#   归并收紧（2026-10-16，比原优化版更严）：① 组里带图的消息超过 1 条不归并
#   （两张图各有各的问题，归并会丢一张）；② 各消息到达时间跨度超 60 秒不归并
#   （接收器停摆后 inbox 积压的旧消息不是"连发"，不该拼成一单）。
# skill_hint / evolve_tail 预取：常见意图（天气/热搜/新闻/链接/基金）的命令
# 和 EVOLVE.md 最后 20 行直接塞进 payload，worker 省一次读 skill / 读账本。
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
  done
  if [ "${#claimed[@]}" -eq 0 ]; then
    silent "inbox raced, nothing claimed" '{}'
  else
    payload="$(python3 - "$BOT_DIR" "${claimed[@]}" <<'PYEOF'
import collections, json, os, re, subprocess, sys, time

bot, raw_ids = sys.argv[1], sys.argv[2:]
ACK_NOISE = "收到你的消息啦，我需要大约半分钟思考一下~"
now = int(time.time())

def safe_uid(uid):
    return re.sub(r"[^A-Za-z0-9_@.\-]", "_", str(uid or ""))[:64]

# 1. 读入所有认领的消息，按发送方分组（按到达时间 mtime 排序）
sender_groups = collections.defaultdict(list)
for i in raw_ids:
    proc_path = os.path.join(bot, "processing", i + ".json")
    try:
        with open(proc_path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        continue
    sender = d.get("from") or d.get("to") or ""
    try:
        mtime = os.path.getmtime(proc_path)
    except OSError:
        mtime = 0
    sender_groups[sender].append((i, d, mtime))

final_ids = []
msgs = {}
histories = {}

for sender, items in sender_groups.items():
    items.sort(key=lambda x: x[2])
    # 2. 同用户连发归并（收紧：组里带图超 1 条、或时间跨度超 60 秒，不归并）
    if len(items) > 1:
        n_img = sum(1 for _, d, _ in items if d.get("image"))
        span = items[-1][2] - items[0][2] if items[0][2] else 0
        mergeable = n_img <= 1 and span <= 60
    else:
        mergeable = False
    if mergeable:
        # 多条归并：以最后一条为 lead，文本换行拼接，图片继承唯一那张
        lead_id, lead_d, _ = items[-1]
        all_texts = [d.get("text", "").strip() for _, d, _ in items
                     if d.get("text", "").strip()]
        combined_text = "\n".join(all_texts)
        combined_img = ""
        for _, d, _ in items:
            if d.get("image"):
                combined_img = d.get("image")
        lead_d["text"] = combined_text
        if combined_img:
            lead_d["image"] = combined_img
        d = lead_d
        # 前序附属单直接归档 done/ 并清理 .alive（它们没起 ack/watchdog）
        for prev_id, _, _ in items[:-1]:
            prev_proc = os.path.join(bot, "processing", prev_id + ".json")
            prev_done = os.path.join(bot, "done", prev_id + ".json")
            try:
                os.makedirs(os.path.join(bot, "done"), exist_ok=True)
                os.replace(prev_proc, prev_done)
            except OSError:
                pass
            try:
                os.remove(os.path.join(bot, "processing", prev_id + ".alive"))
            except OSError:
                pass
        group = [(lead_id, d)]
    else:
        group = [(i, d) for i, d, _ in items]

    for lead_id, d in group:
        d["claim_ts"] = now
        proc_path = os.path.join(bot, "processing", lead_id + ".json")
        tmp_path = "%s.%d.tmp" % (proc_path, os.getpid())
        try:
            with open(tmp_path, "w") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp_path, proc_path)
        except OSError:
            pass

        # 2026-11-20：认领时立刻建 .alive（watchdog 判活的基准）；worker
        # 起 keepalive 后每 10 秒续 touch，worker 死则自然变 stale。
        try:
            _al = os.path.join(bot, "processing", lead_id + ".alive")
            open(_al, "a").close()
            os.utime(_al, None)
        except OSError:
            pass

        ctx = d.get("context_token") or ""
        # ack/watchdog 只给 lead 起（附属单已归档，不起）
        # watchdog_noack = 兜底已提示过，不再重复发延迟提示
        noack = bool(d.get("watchdog_noack"))
        if sender and not noack:
            try:
                subprocess.Popen(
                    ["bash", os.path.join(bot, "ack_timer.sh"),
                     lead_id, sender, ctx, "30"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except Exception:
                pass
        try:
            subprocess.Popen(
                ["bash", os.path.join(bot, "watchdog.sh"), bot, lead_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception:
            pass

        msg_info = {
            "from": sender,
            "text": d.get("text", ""),
            "image": d.get("image", ""),
            "context_token": d.get("context_token"),
            "claim_ts": now,
        }

        # 3. 确定性 skill 意图预取（best-effort：正则没命中时 worker 自行判断）
        text_content = d.get("text", "").strip()
        skill_hint = None
        m_weather = re.search(r"([\u4e00-\u9fa5]{2,10})[的]?天气", text_content)
        if m_weather:
            city = re.sub(r"(今天|明天|后天|实时|现在|请问|查一下|查|看)",
                          "", m_weather.group(1)).strip()
            skill_hint = {
                "type": "weather",
                "city": city,
                "cmd": f'curl -s "https://60s.vaneus.ccwu.cc/v2/weather?query={city}"',
            }
        elif any(k in text_content for k in ("微博热搜", "知乎热搜", "B站热搜", "抖音热搜")):
            platform = ("weibo" if "微博" in text_content else
                        "zhihu" if "知乎" in text_content else
                        "bili" if "B站" in text_content else "douyin")
            skill_hint = {
                "type": "hot-topics",
                "platform": platform,
                "cmd": f'curl -s "https://60s.vaneus.ccwu.cc/v2/{platform}"',
            }
        elif any(k in text_content for k in ("今日新闻", "60秒", "早报", "新闻简报", "每日新闻")):
            skill_hint = {
                "type": "daily-news",
                "cmd": 'curl -s "https://60s.vaneus.ccwu.cc/v2/60s"',
            }
        elif any(k in text_content for k in ("我的基金", "大盘", "行情", "大盘分析")):
            fund_arg = "--my-funds" if "我的基金" in text_content else "--market-report"
            skill_hint = {
                "type": "fund",
                "cmd": f"python skills/fund-market-analysis/scripts/analyze_fund.py {fund_arg}",
            }
        m_url = re.search(r"https?://[^\s]+", text_content)
        if m_url and not skill_hint:
            url = m_url.group(0)
            skill_hint = {
                "type": "url",
                "url": url,
                "cmd": f"skills/grok-search/scripts/fetch.js '{url}' --provider direct",
            }
        if skill_hint:
            msg_info["skill_hint"] = skill_hint

        # 简单消息检测（超短 prompt 路由）：问候/确认类短消息，不带图/链接/skill 意图
        _is_simple = False
        if not d.get("image") and not skill_hint:
            _txt = text_content.strip()
            # 纯问候/确认，长度 < 15 字；2026-11-03 补：单字确认“1/是/对/行”
            # 及口头确认也走超短路由——此前“1”漏网走全量 prompt，思考久、内存占用大，
            # 正是 11-03 08:36 那条 11 分钟认领延迟的诱因之一
            _simple_patterns = (
                "你好", "您好", "hi", "hello", "早上好", "晚上好", "在吗", "在么",
                "好的", "好", "谢谢", "ok", "OK", "收到", "嗯", "哦",
                "1", "是", "对", "行", "可以", "没问题", "好吧", "嗯嗯",
            )
            if len(_txt) < 15 and any(p == _txt or _txt.startswith(p + "，") or _txt.startswith(p + ",") for p in _simple_patterns):
                _is_simple = True
        if _is_simple:
            msg_info["simple"] = True

        msgs[lead_id] = msg_info
        final_ids.append(lead_id)

        # 4. 上下文历史：最近 15 行，过滤延迟提示噪音；
        #    最近 4 条保留 1000 字（指代就靠这几条），更早的截到 400 字。
        if sender and sender not in histories:
            lines = []
            hist_file = os.path.join(bot, "history", safe_uid(sender) + ".jsonl")
            try:
                with open(hist_file) as f:
                    raw = f.readlines()[-15:]
                filtered = []
                for ln in raw:
                    try:
                        e = json.loads(ln)
                    except ValueError:
                        continue
                    t = str(e.get("text", ""))
                    if t.strip() == ACK_NOISE:
                        continue
                    filtered.append((e.get("role"), t))
                total_f = len(filtered)
                for idx, (role, t) in enumerate(filtered):
                    limit = 1000 if (total_f - idx) <= 4 else 400
                    if len(t) > limit:
                        t = t[:limit] + "…[已截断]"
                    lines.append({"role": role, "text": t})
            except OSError:
                pass
            histories[sender] = lines

evolve_tail = []
try:
    with open(os.path.join(bot, "EVOLVE.md")) as f:
        evolve_tail = [ln.rstrip("\n") for ln in f.readlines()[-20:] if ln.strip()]
except OSError:
    pass

out = {"msg_ids": final_ids, "messages": msgs, "histories": histories}
if evolve_tail:
    out["evolve_tail"] = evolve_tail
print(json.dumps(out, ensure_ascii=False))
PYEOF
)"
    wake "inbox has ${#claimed[@]} new message(s), claimed" "$payload"
  fi
fi
