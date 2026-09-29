#!/usr/bin/env bash
# ack_timer.sh — "对方正在输入"的替代方案：延迟提示。
#
# 背景：ilink 协议的 send_typing 调通了（ret=0）但微信客户端不渲染，
# 所以用一条延迟的文字提示来填补"思考中"的沉默期。
#
# 用法: ack_timer.sh <msg_id> <to> <context_token> [delay秒，默认10] [提示文案]
#
# worker 认领消息后立刻在后台启动：
#   setsid nohup bash ~/workspace/wechat-bot/ack_timer.sh <id> <to> <ctx> 10 \
#     > /dev/null 2>&1 < /dev/null &
#
# delay 秒后：
#   - processing/<msg_id>.json 已不在（= 已回完，移到 done/）→ 静默退出，绝不打扰；
#   - outbox/<msg_id>.json 已存在（= 回复已写好，马上就发）→ 静默退出；
#   - 否则（= 还在思考）→ 发一条"正在想"提示，让用户知道没在干等。
set -u
ID="${1:?msg_id required}"
TO="${2:?to required}"
CTX="${3:?context_token required}"
DELAY="${4:-10}"
TEXT="${5:-收到，正在想，稍等…}"

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WECHAT_BOT_PYTHON:-$HOME/workspace/CowAgent/venv/bin/python}"

sleep "$DELAY"

# 已回完，或回复已写好马上就发 → 不需要提示
[ -f "$BASE/processing/$ID.json" ] || exit 0
[ -f "$BASE/outbox/$ID.json" ] && exit 0

OUT="$BASE/outbox/ack_$ID.json"
"$PY" - "$OUT" "$TO" "$CTX" "$TEXT" <<'PYEOF'
import json, sys
out, to, ctx, text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(out, "w") as f:
    json.dump({"to": to, "context_token": ctx, "text": text}, f, ensure_ascii=False)
PYEOF

cd "$BASE" && "$PY" wxbot.py send >/dev/null 2>&1
