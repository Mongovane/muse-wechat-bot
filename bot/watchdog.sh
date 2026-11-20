#!/usr/bin/env bash
# watchdog: worker 存活监督（2026-11-20 重写）。
# 旧版（认领后睡 20 秒、只看 .alive 文件存在）：冷启动 worker 从认领到
# 第一次 touch 要几十秒（2026-11-20 09:34 实测：re-claim 后 9 秒、首轮
# 认领后最长约 59 秒；另见子代理冷启动探针约 3~9 秒），20 秒一到就误判
# 死、把单子扔回 inbox，hook 又重唤——同一任务反复认领/判死/重排，
# 第一份丢回复、第二份晚 5~6 分钟。
# 新版：按 .alive 新鲜度判活，循环监督到任务结束、重试耗尽或超时。
#   GRACE_SECS（默认 180，环境变量 WATCHDOG_GRACE_SECS 可改）：认领后
#     宽限，覆盖冷启动 + keepalive 起步；宽限内不判死。180 秒约是最长
#     实测（59 秒）的 3 倍余量。
#   STALE_SECS（默认 60，WATCHDOG_STALE_SECS）：.alive 超过 60 秒没
#     刷新 = worker 已死（keepalive 每 10 秒 touch 一次；60 秒 = 连续
#     6 次漏 touch）。认领时 .alive 即建（_do_claim / hook §2），worker
#     活着就一直有人续 touch，所以 stale 只发生在 worker 死后。
#   每 INTERVAL 秒（默认 30，WATCHDOG_INTERVAL_SECS）检查一次；任务
#     文件消失（回完）即退出；总监督不超 MAX_TOTAL 秒（默认 1800，
#     WATCHDOG_MAX_TOTAL_SECS）。重试上限 2 次（沿用旧行为），耗尽后
#     留给 cron/兜底，不再重排。
# 双发防护仍靠 `wxbot.py finish <id> <claim_ts>` 归属校验（2026-10-04）。
set -uo pipefail

BOT_DIR="${1:?bot dir required}"
id="${2:?msg id required}"
GRACE="${WATCHDOG_GRACE_SECS:-180}"
STALE="${WATCHDOG_STALE_SECS:-60}"
INTERVAL="${WATCHDOG_INTERVAL_SECS:-30}"
MAX_TOTAL="${WATCHDOG_MAX_TOTAL_SECS:-1800}"

proc="$BOT_DIR/processing/$id.json"
alive="$BOT_DIR/processing/$id.alive"

start="$(date +%s)"
# claim_ts（认领时刻）没有就用文件 mtime 兜底。
claim_ts="$(jq -r '.claim_ts // 0' "$proc" 2>/dev/null || echo 0)"
claim_ts="${claim_ts%%.*}"
case "$claim_ts" in ''|*[!0-9]*) claim_ts=0 ;; esac
if [ "$claim_ts" -eq 0 ]; then
  claim_ts="$(stat -c %Y "$proc" 2>/dev/null || echo "$start")"
fi

while true; do
  now="$(date +%s)"
  # 已回完或被别处接手。
  [ -f "$proc" ] || exit 0
  [ "$((now - start))" -ge "$MAX_TOTAL" ] && exit 0
  # 宽限期内：只等，不判死。
  if [ "$((now - claim_ts))" -lt "$GRACE" ]; then
    sleep "$INTERVAL"
    continue
  fi
  # 宽限后：.alive 新鲜（STALE 秒内刷新过）= 活着，哪怕思考再慢也不动。
  if [ -f "$alive" ]; then
    al_mtime="$(stat -c %Y "$alive" 2>/dev/null || echo 0)"
    if [ "$((now - al_mtime))" -lt "$STALE" ]; then
      sleep "$INTERVAL"
      continue
    fi
  fi
  # .alive 缺失或超 STALE 秒没刷新 = worker 已死（或从未起来）。
  retries="$(jq -r '.watchdog_retries // 0' "$proc" 2>/dev/null || echo 0)"
  case "$retries" in ''|*[!0-9]*) retries=0 ;; esac
  if [ "$retries" -ge 2 ]; then
    exit 0
  fi
  tmp="$proc.tmp"
  if jq --argjson r $((retries + 1)) \
      '.watchdog_retries = $r | .watchdog_noack = true' \
      "$proc" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$proc"
  fi
  # Requeue for the next hook poll (5s). Atomic mv; failure means someone else got it.
  mv "$proc" "$BOT_DIR/inbox/$id.json" 2>/dev/null || true
  rm -f "$alive"
  # 2026-11-20 第二轮：这单若是 warm 认领的批次 lead，删掉它那份 warm-claim
  # 文件，免得 hook §1f 又按旧 payload 唤醒一个 worker 造成重复回。
  # 非 lead 的单子重排时文件名对不上，§1f 的全量校验会把那份文件当 STALE
  # 删掉，同样不会重复回。
  rm -f "$BOT_DIR/warm-claim-$id.json"
  exit 0
done
