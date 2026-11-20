#!/usr/bin/env bash
# ack_timer.sh — typing keepalive：让“对方正在输入…”持续到回复来。
#
# 背景：sendtyping 真渲染（2026-10-16 Peter 实测），但指示灯 TTL 只有几秒，
# 单次发送不覆盖整个思考。ack_timer 每 2 秒重发一次 typing，保持指示灯。
#
# 2026-10-16 去掉 ack 文字（"收到你的消息啦，我需要大约半分钟思考一下~"）：
# 指示灯已覆盖反馈，文字提示是多余的消息噪音。
#
# 用法: ack_timer.sh <msg_id> <to> <context_token>
#
# worker 认领消息后立刻在后台启动：
#   setsid nohup bash ~/workspace/wechat-bot/ack_timer.sh <id> <to> <ctx> \
#     > /dev/null 2>&1 < /dev/null &
#
# 退出条件：processing/<msg_id>.json 已不在（= 已回完，移到 done/，
# 或被看门狗回收），或运行超 300 秒（worker 早该答完）。
# 竞态说明：回复发出（cancel）与 keepalive（show）之间有毫秒级竞态，
# 回复后可能多显示一次指示灯几秒，无害（指示灯随 TTL 自行过期）。
set -u
ID="${1:?msg_id required}"
TO="${2:?to required}"
CTX="${3:?context_token required}"

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WECHAT_BOT_PYTHON:-$HOME/workspace/CowAgent/venv/bin/python}"

START="$(date +%s)"

# 2026-11-19 根因修复：上一版写的是 `if ! cd "$BASE" && "$PY" ...`，
# bash 里 `!` 只否定 `cd` 这一段，cd 成功时整个条件为假、typing 调用
# 永远不执行，循环只剩 sleep——指示灯只在接收器那一下闪过，之后全程
# 不亮，且没有任何失败日志。不要再把 `!` 和 `&&` 写在同一条件里。
# 正确做法：先 cd（失败直接退出），再单独判断 typing 的返回值。
cd "$BASE" || exit 1

while true; do
  # 已回完（processing 被移到 done/）或被看门狗回收 → 停
  [ -f "$BASE/processing/$ID.json" ] || exit 0
  ELAPSED="$(( $(date +%s) - START ))"
  [ "$ELAPSED" -ge 300 ] && exit 0

  # keepalive：每轮发一次 typing（首轮立即发，覆盖接收器首次发送后的空隙）
  # 2026-11-19 typing 回归：指示灯 TTL 只有几秒，旧 cadence 约 1.6s 调用 +
  # 3s sleep ≈ 4.6s，抖动就断档；sleep 改 2s（≈3.6s 一轮）。
  # 失败不再静默丢弃：记入 ack_timer.log（只留最后 200 行），否则
  # keepalive 失效时接收器日志仍显示 TYPING True，无从发现。
  if ! "$PY" wxbot.py typing "$TO" 1 >/dev/null 2>&1; then
    echo "$(date '+%F %T') ack_timer typing FAIL $ID" >> "$BASE/ack_timer.log"
    tail -n 200 "$BASE/ack_timer.log" > "$BASE/ack_timer.log.tmp" 2>/dev/null \
      && mv "$BASE/ack_timer.log.tmp" "$BASE/ack_timer.log"
  fi

  sleep 2
done
