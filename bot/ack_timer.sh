#!/usr/bin/env bash
# ack_timer.sh — typing keepalive：让“对方正在输入…”持续到回复来。
#
# 背景：sendtyping 真渲染（2026-10-16 Peter 实测），但指示灯 TTL 只有几秒，
# 单次发送不覆盖整个思考。ack_timer 每 3 秒重发一次 typing，保持指示灯。
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

while true; do
  # 已回完（processing 被移到 done/）或被看门狗回收 → 停
  [ -f "$BASE/processing/$ID.json" ] || exit 0
  ELAPSED="$(( $(date +%s) - START ))"
  [ "$ELAPSED" -ge 300 ] && exit 0

  # keepalive：每轮发一次 typing（首轮立即发，覆盖接收器首次发送后的空隙）
  cd "$BASE" && "$PY" wxbot.py typing "$TO" 1 >/dev/null 2>&1 || true

  sleep 3
done
