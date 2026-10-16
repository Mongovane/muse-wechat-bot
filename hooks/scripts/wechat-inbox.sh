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
unset _now _proc _id _ob _ob_mtime _al _al_mtime _cts

# --- 2. inbox: claim in bash (atomic mv), then build payload in Python ---
# 认领下沉到脚本：worker 不再自己 mv，省一次模型往返。
# ack 文字已于 2026-10-16 去掉（指示灯已覆盖反馈），ack_timer 只做 keepalive。
# 同用户连发归并：5 秒内同一个人先发图、后发问题，合并成一单（文本拼接、
# 图片继承），前序附属单直接归档 done/，避免两个 worker 各答各的。
#   归并收紧（2026-10-16，比原优化版更严）：① 组里带图的消息超过 1 条不归并
#   （两张图各有各的问题，归并会丢一张）；② 各消息到达时间跨度超 60 秒不归并
#   （接收器停摆后 inbox 积压的旧消息不是"连发"，不该拼成一单）。
# evolve_tail 预取：EVOLVE.md 最后 20 行直接塞进 payload，worker 省一次读账本。
# skill 命令不在 payload 里（纯数据原则），在 hook-worker.md 的 quick-ref 里。
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

        ctx = d.get("context_token") or ""
        # ack/watchdog 只给 lead 起（附属单已归档，不起）
        # watchdog_noack = 兜底 worker 已起过 keepalive，不再重复起一个
        noack = bool(d.get("watchdog_noack"))
        if sender and not noack:
            try:
                subprocess.Popen(
                    ["bash", os.path.join(bot, "ack_timer.sh"),
                     lead_id, sender, ctx],
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
        # payload 保持 100% 纯数据，不含可执行命令（受信任 Prompt + 纯数据原则）：
        # skill 命令在 hook-worker.md 的 quick-ref 里（受信任 prompt），worker 自己
        # 按用户消息选命令、填城市/平台/URL（LLM 分类比正则准）。此处不做 skill_hint。

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
