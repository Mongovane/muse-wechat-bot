#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone WeChat bot reusing CowAgent's ilink protocol implementation
(~/workspace/CowAgent/channel/weixin/weixin_api.py).

Subcommands:
  qr          Fetch login QR code -> qr.json (+ qr.png)
  wait_login  Poll QR status until confirmed/expired/timeout -> credentials.json
  receive     Daemon: long-poll getUpdates -> inbox/<msg_id>.json
              (text, server-transcribed voice, images downloaded from CDN)
  send        Send outbox/*.json -> sent/ (all files; explicit full send).
              Use `wxbot.py send <fn>` or cmd_send(only=<fn>) to send exactly one file.
  finish      Worker one-shot: send ONLY outbox/<id>.json, move
              processing/<id>.json -> done/, append latency entry.
              FINISH_FAIL leaves processing/ for the fallback sweeper.
              finish must never flush other outbox files (2026-10-16:
              full-send-in-finish leaked flash/briefing/cards into replies).
              Before sending, finish re-checks inbox for newer messages
              from the same sender; if any arrived, it merges them into
              this job and returns 2 (FINISH_RECHECK) so the worker can
              regenerate the reply instead of answering stale context.
  bind        Open a 600s owner-binding window (first sender wins).
  approve-owner <id> / list-unknown / remove-owner <id>
              Owner whitelist management (main chat only, never from WeChat).
  health [--offline]  One-line status, exit 0/1/2.
  watchdog [--max-age-mins 30]  Dead-man switch on buf.txt freshness.
  alert <token|poll|worker> <failing|recovered>  Alert closed-loop state.
  stats       Offline snapshot (no server calls).
  preflight   Environment pre-check (no login, no network).
  smoke-test [--no-send]  End-to-end self-check.
  prune-media [--dry-run]  Media retention cleanup (30d + 500MB).
  warm-wait [--idle-secs 180] [--cap-secs 1800] [--resident] [--max-claims 100]
              Watch inbox for follow-ups after a reply (opt-in).
  claim       Claim inbox messages like hook §2, but without waking
              anyone (warm-wait companion: prints the wake payload JSON).
  watch-outbox [--poll-secs 0.2] [--retry-secs 60] [--max-secs N]
              Resident outbox watcher: send a numeric reply file the
              moment it appears (worker no longer calls finish — saves
              one model action, ~3.5s). Only numeric replies with a
              processing/<id>.json; proactive never touched. max-secs
              is for tests only (run N seconds then exit).
  keepalive <msg_id> [--interval-secs 10] [--max-secs 1200]
              Worker liveness heartbeat: touch processing/<msg_id>.alive
              every interval seconds until the job file leaves processing/
              (finished) or max_secs elapses. Start it with a foreground
              exec (NOT background=true — it must die with the worker).
"""
import sys, os, json, time, uuid, re, fcntl, base64, subprocess
import datetime

sys.path.insert(0, os.environ.get("COWAGENT_HOME", "/home/hatch/workspace/CowAgent"))
from channel.weixin.weixin_api import (  # noqa: E402
    WeixinApi, DEFAULT_BASE_URL, upload_media_to_cdn,
    download_media_from_cdn, CDN_BASE_URL,
)
try:  # CDN 断点续传用（失败则回退到 download_media_from_cdn 单次下载）
    from channel.weixin.weixin_api import (  # noqa: E402
        _aes_ecb_decrypt as _cow_decrypt,
        _get_cdn_session as _cow_cdn_session,
    )
except Exception:  # pragma: no cover
    _cow_decrypt = None
    _cow_cdn_session = None

BASE = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(BASE, "credentials.json")
BUF = os.path.join(BASE, "buf.txt")
INBOX = os.path.join(BASE, "inbox")
OUTBOX = os.path.join(BASE, "outbox")
SENT = os.path.join(BASE, "sent")
DONE = os.path.join(BASE, "done")
PROCESSING = os.path.join(BASE, "processing")
SEND_LOCK = os.path.join(BASE, "send.lock")
RECEIVE_LOCK = os.path.join(BASE, "receive.lock")
# sendtyping 票据缓存：getconfig 拿到的 typing_ticket。
# 2026-11-19 typing 回归：本地原先按 20 小时缓存（无来源的猜测），
# 同协议上游 ilink.py 实测票据有效期约 9 分钟（缓存 540 秒）。
# 20 小时旧票据调 sendtyping 仍回 {"ret": 0}，但客户端是否继续渲染
# 无法从 ret 判断——票据过期是这条链上唯一无依据的假设，改成 540 秒
# 并按 fetched_at 计时；无 fetched_at 的旧缓存条目视为过期重取。
TYPING_TICKETS_FILE = os.path.join(BASE, "typing_tickets.json")
TYPING_TICKET_TTL = 540
LATENCY_LOG = os.path.join(os.path.expanduser("~"),
                           "workspace/goals/muse/hidden_files/wechat-latency.jsonl")
SHARED_POOL = os.path.join(os.path.expanduser("~"),
                           "workspace/goals/muse/hidden_files/pushed-shared.json")
MEDIA = os.path.join(BASE, "media")
QRFILE = os.path.join(BASE, "qr.json")
QRPNG = os.path.join(BASE, "qr.png")
SESSION_EXPIRED = -14
CHUNK = 3500
SEEN_MAX = 1000  # 接收器内存去重集上限，超了按 LRU 丢最老的（磁盘兜底见 _is_msg_already_processed）
# ilink 回复配额：每条用户入站后 bot 最多发约 10 条，第 11 条起 ret=-2
# （2026-10-08 全网调研确认：Thysrael/Horizon PR#159、clawock、腾讯维护者亲口确认）。
# 本地账本：proactive 发送按优先级排队，ack 在配额 >=5 时跳过。
# QUOTA_MAX 是文档值（约 10 条/入站，实际阈值服务端浮动），本地不做强制门控。
QUOTA_FILE = os.path.join(BASE, "quota.json")
QUOTA_MAX = 10
QUOTA_ACK_LIMIT = 5
# NOTE 2026-11-19：QUOTA_PRIORITY_LIMIT 已删除。proactive 按优先级做配额
# 门控会违反 2026-10-08 Peter 的决定（"有快讯就推，不做本地预判，
# 服务端是最终裁决"）。优先级只用于 cmd_send 的排序，不做发送门控。
# --- 主人白名单 / 发送幂等 / 引用缓存 / 告警（2026-11-19）---
OWNER_FILE = os.path.join(BASE, "owner.json")
UNKNOWN_FILE = os.path.join(BASE, "unknown_senders.json")
UNKNOWN_MAX = 100
BIND_FILE = os.path.join(BASE, "bind_state.json")
BIND_WINDOW_SECS = 600
SEND_LEDGER = os.path.join(BASE, "send_ledger.json")
SEND_LEDGER_LOCK = os.path.join(BASE, "send_ledger.lock")
SEND_LEDGER_TTL = 24 * 3600
SEND_AUDIT = os.path.join(BASE, "send_audit.jsonl")
SEND_AUDIT_CAP = 1000
QUOTE_CACHE_FILE = os.path.join(BASE, "quote_cache.json")
QUOTE_CACHE_TTL = 30 * 86400
QUOTE_CACHE_CAP = 10000
TOKEN_STALE_FILE = os.path.join(BASE, "token_stale.json")
TOKEN_STALE_WINDOW = 3600  # -14 连续出现 <1h 算 stale（静默退避），>1h 算 expired
ALERT_STATE_FILE = os.path.join(BASE, "alert_state.json")
ALERT_RULES = {  # kind -> (debounce_secs, repeat_secs)
    "token": (0, 3600), "poll": (600, 3600), "worker": (600, 3600),
}
WATCHDOG_STATE_FILE = os.path.join(BASE, "watchdog_state.json")
WATCHDOG_OFF_FILE = os.path.join(BASE, "watchdog.off")
WARM_FILE = os.path.join(BASE, "warm.json")
WARM_IDLE_SECS = 180
WARM_CAP_SECS = 1800
# 常驻（2026-11-20）：warm-resident.json 记会话 {"session_start", "claims"}，
# 24h 上限 + 认领满 100 条轮换——worker 会话随处理的每条消息变长，无上限
# 常驻会让上下文无限膨胀、状态变 stale；轮换后下一条消息走 hook 另唤新 worker。
WARM_RESIDENT_FILE = os.path.join(BASE, "warm-resident.json")
WARM_RESIDENT_CAP_SECS = 86400
WARM_RESIDENT_MAX_CLAIMS = 100
# 常驻认领让步（2026-11-20 混合版）：发现新消息后先等这么多秒再复查认领，
# 让 worker 自跑的 warm-wait（1 秒轮询）先抢；没 worker 在等时只慢这几秒。
WARM_RESIDENT_DEFER_SECS = 2
# 自适应让步（2026-11-20）：刚有 worker 回完（done 文件 30 秒内）时，它的
# warm-wait 正在启动——实测 worker 发完到跑起 warm-wait 慢到 17 秒，2 秒
# 让步盖不住；这时多等 20 秒让它先抢。没 worker 刚回完时只等 2 秒。
WARM_RESIDENT_DEFER_LONG_SECS = 20
WARM_RECENT_DONE_SECS = 30
# 常驻请求（2026-11-20 第二轮）：worker 回完只写 warm-request.json 就退出，
# hook supervisor 读它拉起 hook 所有的 warm-wait。worker 绝不自己跑 warm-wait。
WARM_REQUEST_FILE = os.path.join(BASE, "warm-request.json")
WARM_CLAIM_PREFIX = os.path.join(BASE, "warm-claim-")
# outbox watcher（2026-11-20，发送提速）：常驻进程盯 outbox/，普通回复
# 文件一出现就代发，worker 不用再调 finish（省一次模型动作 ~3.5s）。
# 只发纯数字文件名的普通回复（processing/<id>.json 必须在）；proactive
# （flash-/briefing-/market-/digest-）只由显式 send 发，绝不代发。
# 失败（配额 -2 等）留文件给兜底，WATCH_OUTBOX_RETRY_SECS 内不重复尝试。
WATCH_OUTBOX_FILE = os.path.join(BASE, "watch-outbox.json")
WATCH_OUTBOX_POLL_SECS = 0.2
WATCH_OUTBOX_RETRY_SECS = 60
MEDIA_MAX_BYTES = 100 * 1024 * 1024

for d in (INBOX, OUTBOX, SENT, DONE, MEDIA, PROCESSING):
    os.makedirs(d, exist_ok=True)


def _atomic_write_json(path, obj, indent=None, mode=None):
    """Write JSON atomically via a unique .tmp file and os.replace.

    多进程/多线程并发写同一文件时，裸 open("w") 会读到半截 JSON；
    tmp + os.replace 保证读者要么看到旧完整版，要么看到新完整版。
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def safe_user_id(user_id):
    """Normalize user_id into a safe filename stem (shared by wxbot and hooks)."""
    return re.sub(r"[^A-Za-z0-9_@.\-]", "_", str(user_id or ""))[:64]


def load_creds():
    with open(CREDS) as f:
        return json.load(f)


def save_creds(creds):
    _atomic_write_json(CREDS, creds, indent=2, mode=0o600)


def _load_typing_tickets():
    try:
        with open(TYPING_TICKETS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ensure_typing_ticket(api, user_id):
    """获取或复用缓存的 typing_ticket（有效期 TYPING_TICKET_TTL=540 秒）。

    2026-10-16 借鉴 nanoclaw #1553 实测：getconfig 请求体必须显式带 ilink_user_id，
    否则返回 ret=-2 errmsg='ilink_user_id required'。票据损坏/过期时重新获取。
    2026-11-19：有效期以 fetched_at + TTL 计；无 fetched_at 的旧条目
    （20 小时时代的缓存）一律视为过期重取。
    """
    if not user_id:
        return ""
    now = time.time()
    tickets = _load_typing_tickets()
    cached = tickets.get(str(user_id))
    if isinstance(cached, dict):
        try:
            tk = cached.get("ticket", "")
            exp = float(cached.get("expires_at", 0) or 0)
            fetched = float(cached.get("fetched_at", 0) or 0)
        except (TypeError, ValueError):
            tk, exp, fetched = "", 0, 0
        if tk and fetched and now < exp and now < fetched + TYPING_TICKET_TTL:
            return tk
    ticket = ""
    try:
        if hasattr(api, "_post"):
            r = api._post("ilink/bot/getconfig", {
                "ilink_user_id": str(user_id),
                "base_info": {"channel_version": "2.0.1"},
            }, retries=0)
            if isinstance(r, dict):
                ticket = str(r.get("typing_ticket", "") or "")
        elif hasattr(api, "get_config"):
            r = api.get_config(str(user_id))
            if isinstance(r, dict):
                ticket = str(r.get("typing_ticket", "") or "")
    except Exception:
        return ""
    if ticket:
        tickets[str(user_id)] = {
            "ticket": ticket,
            "fetched_at": now,
            "expires_at": now + TYPING_TICKET_TTL,
        }
        try:
            _atomic_write_json(TYPING_TICKETS_FILE, tickets)
        except Exception:
            pass
    return ticket


def send_typing_indicator(api, user_id, command=1):
    """发送或取消微信原生“对方正在输入…”状态（command: 1=显示, 2=取消）。

    2026-10-16 借鉴 nanoclaw #1553 与 agentserver：请求体同时携带 ilink_user_id
    与 to_user_id，并同时传 command 与 status 字段以兼容不同网关节点。
    完全不消耗 10 条 sendmessage 回复配额；任何异常静默忽略，绝不阻塞主流程。
    注意：本轮只发一次（无 keepalive），长思考时指示灯可能中途过期——
    ack_timer 每 3 秒 keepalive 续上（ack 文字已于 2026-10-16 去掉，指示灯已覆盖反馈）。
    仅普通回复路径调用：proactive（快讯/早报/卡片）的取消会误关正在思考的回复的指示灯。
    proactive（快讯/早报/卡片）的取消会误关正在思考的回复的指示灯。
    """
    if not user_id or not hasattr(api, "_post"):
        return False
    try:
        ticket = _ensure_typing_ticket(api, user_id)
        if not ticket:
            return False
        r = api._post("ilink/bot/sendtyping", {
            "ilink_user_id": str(user_id),
            "to_user_id": str(user_id),
            "typing_ticket": ticket,
            "command": int(command),
            "status": int(command),
            "base_info": {"channel_version": "2.0.1"},
        }, retries=0)
        return isinstance(r, dict) and r.get("ret", 0) == 0
    except Exception:
        return False


def cmd_typing(user_id, command=1):
    """CLI 入口：wxbot.py typing <user_id> <command>。供 ack_timer.sh keepalive 调用。"""
    creds = load_creds()
    api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                    token=creds.get("token", ""))
    return send_typing_indicator(api, user_id, command=command)


def _load_quota():
    """读配额账本：{"sent_since_inbound": int}。文件不存在返回 0。"""
    try:
        with open(QUOTA_FILE) as f:
            return int(json.load(f).get("sent_since_inbound", 0))
    except (OSError, ValueError, KeyError):
        return 0


def _save_quota(n):
    _atomic_write_json(QUOTA_FILE, {"sent_since_inbound": int(n)})


def _quota_reset():
    """用户入站：配额清零。"""
    _save_quota(0)


def _quota_bump():
    """一次 sendmessage 成功：配额消耗 +1。"""
    _save_quota(_load_quota() + 1)


# --- 主人白名单（2026-11-19，借鉴上游 ilink.py owner.json/bind）---
# 代码层强制：没配 owner.json 时默认拒绝所有人；名单外的消息在接收阶段
# 就丢弃——不入队、不唤醒、不下载媒体、不重置配额；发信人 ID 只进待批
# 名单等人工确认。加白只能从主聊天发起（approve-owner），微信里说得
# 再好听也不算数。
def load_owner():
    """-> {"owner_id": str, "owner_ids": [str]}；文件缺失/损坏则两者为空。"""
    try:
        with open(OWNER_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            ids = d.get("owner_ids") or (
                [d["owner_id"]] if d.get("owner_id") else [])
            ids = [str(x) for x in ids if x]
            return {"owner_id": d.get("owner_id") or (ids[0] if ids else ""),
                    "owner_ids": ids}
    except (OSError, ValueError):
        pass
    return {"owner_id": "", "owner_ids": []}


def save_owner(owner_id, owner_ids):
    ids = sorted({str(x) for x in (owner_ids or []) if x})
    _atomic_write_json(OWNER_FILE, {
        "owner_id": owner_id or (ids[0] if ids else ""),
        "owner_ids": ids}, indent=2)


def is_owner(uid):
    return bool(uid) and str(uid) in load_owner()["owner_ids"]


def load_unknown_senders():
    try:
        with open(UNKNOWN_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def note_unknown_sender(uid):
    """记待批发信人：只记 {first_seen, last_seen, count}，绝不存消息内容。"""
    if not uid:
        return
    try:
        d = load_unknown_senders()
        now = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        ent = d.get(str(uid)) or {"first_seen": now, "last_seen": now,
                                  "count": 0}
        ent["last_seen"] = now
        ent["count"] = int(ent.get("count", 0) or 0) + 1
        d[str(uid)] = ent
        if len(d) > UNKNOWN_MAX:  # 只留最近 100 个发信人
            d = dict(sorted(d.items(), key=lambda kv: kv[1].get("last_seen", ""),
                            reverse=True)[:UNKNOWN_MAX])
        _atomic_write_json(UNKNOWN_FILE, d)
    except Exception as e:
        print("UNKNOWN_SENDER_FAIL", uid, e, flush=True)


def load_bind_state():
    """绑定窗口状态；带 expires_at 且已过期的窗口读作空（并顺手清掉状态文件）。
    没有 expires_at 的旧格式状态视为未过期（由调用方判断）。"""
    try:
        with open(BIND_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            exp = d.get("expires_at")
            if exp is None or float(exp) > time.time():
                return d
    except (OSError, ValueError):
        pass
    if os.path.exists(BIND_FILE):
        clear_bind_state()
    return {}


def save_bind_state(d):
    _atomic_write_json(BIND_FILE, d)


def clear_bind_state():
    try:
        os.remove(BIND_FILE)
    except OSError:
        pass


def cmd_bind():
    """开 600 秒绑定窗口：第一个发信人成为主人（上游 bind 的本地版）。"""
    owner = load_owner()
    if owner["owner_ids"]:
        print("ALREADY_BOUND", owner["owner_id"], flush=True)
        return 0
    now = time.time()
    st = load_bind_state()
    if st.get("expires_at", 0) > now:
        pid = st.get("pid")
        alive = False
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = True
            except (OSError, ValueError):
                alive = False
        if alive:
            print("BIND_ALREADY_OPEN", "expires in %ds"
                  % int(st["expires_at"] - now), flush=True)
            return 0
    save_bind_state({"opened_at": now, "expires_at": now + BIND_WINDOW_SECS,
                     "pid": os.getpid()})
    print("BIND_OK", "window %ds, first sender becomes owner" % BIND_WINDOW_SECS,
          flush=True)
    return 0


def _send_bind_welcome(api, to, ctx):
    """绑定成功后连发欢迎消息（尽力而为，一条失败不影响其余）。"""
    texts = [
        "你好，我是 Muse，你的个人 AI 助理。",
        "你现在是我的主人：只有你发来的消息会唤醒我，其他人发消息我不会理会。",
        "直接跟我说话就行，需要我做的事、想查的东西、想记的事都可以交给我。",
        "如果以后想加别的微信号当主人，在主聊天里说一声就行。",
    ]
    sent = 0
    for t in texts:
        try:
            r = api.send_text(to, t, ctx)
            if isinstance(r, dict) and r.get("ret", 0) == 0:
                sent += 1
        except Exception as e:
            print("BIND_WELCOME_FAIL", e, flush=True)
    print("BIND_WELCOME_SENT", sent, "/", len(texts), flush=True)
    return sent


def cmd_approve_owner(uid):
    """主聊天专用：把待批名单里的 id 加进白名单（绝不从微信里接受此操作）。"""
    if not uid:
        print("usage: wxbot.py approve-owner <user_id>", flush=True)
        return 1
    owner = load_owner()
    if str(uid) in owner["owner_ids"]:
        print("OWNER_ALREADY", uid, flush=True)
        return 0
    ids = owner["owner_ids"] + [str(uid)]
    save_owner(owner["owner_id"] or str(uid), ids)
    d = load_unknown_senders()
    if str(uid) in d:
        del d[str(uid)]
        try:
            _atomic_write_json(UNKNOWN_FILE, d)
        except Exception:
            pass
    print("OWNER_APPROVED", uid, flush=True)
    return 0


def cmd_list_unknown():
    d = load_unknown_senders()
    print(json.dumps(d, ensure_ascii=False, indent=2), flush=True)
    return 0


def cmd_remove_owner(uid):
    if not uid:
        print("usage: wxbot.py remove-owner <user_id>", flush=True)
        return 1
    owner = load_owner()
    ids = [x for x in owner["owner_ids"] if x != str(uid)]
    if len(ids) == len(owner["owner_ids"]):
        print("OWNER_NOT_FOUND", uid, flush=True)
        return 1
    save_owner(ids[0] if ids else "", ids)
    print("OWNER_REMOVED", uid, flush=True)
    return 0


def _is_proactive(fn):
    """proactive 发送（快讯/早报/卡片/ack）vs 用户回复（数字 msg_id 文件名）。"""
    return fn.startswith(("ack_", "flash-", "briefing-", "market-", "digest-"))


def render_qr_png(content, path=QRPNG):
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=8, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    qr.make_image().save(path)
    return path


def cmd_qr():
    api = WeixinApi()
    r = api.fetch_qr_code()
    qrcode, content = r.get("qrcode", ""), r.get("qrcode_img_content", "")
    if not qrcode:
        print("QR_FAIL", r)
        sys.exit(1)
    _atomic_write_json(QRFILE, {"qrcode": qrcode, "content": content, "ts": time.time()})
    render_qr_png(content)
    print("QR_OK")


def cmd_wait_login(timeout=480, max_refresh=10):
    api = WeixinApi()
    refresh = 0
    while refresh <= max_refresh:
        with open(QRFILE) as f:
            q = json.load(f)
        deadline = time.time() + min(timeout, 110)
        while time.time() < deadline:
            try:
                s = api.poll_qr_status(q["qrcode"])
            except Exception as e:
                print("POLL_ERROR", e, flush=True)
                time.sleep(3)
                continue
            st = s.get("status", "wait")
            if st == "confirmed":
                print("CONFIRMED_KEYS", sorted(s.keys()), flush=True)
                bot_token = s.get("bot_token", "")
                bot_id = s.get("ilink_bot_id", "")
                if not bot_token or not bot_id:
                    print("LOGIN_FAIL missing token/bot_id")
                    sys.exit(1)
                save_creds({
                    "token": bot_token,
                    "base_url": s.get("baseurl", DEFAULT_BASE_URL),
                    "bot_id": bot_id,
                    "user_id": s.get("ilink_user_id", ""),
                })
                print("LOGIN_OK", flush=True)
                return
            if st == "expired":
                break
            if st == "scaned":
                print("SCANNED", flush=True)
            time.sleep(2)
        # QR expired or ~110s passed -> refresh
        refresh += 1
        if refresh > max_refresh:
            print("LOGIN_GIVEUP")
            sys.exit(1)
        print("QR_REFRESH", refresh, flush=True)
        cmd_qr()
    print("LOGIN_GIVEUP")
    sys.exit(1)


def extract_text(msg):
    parts = []
    for item in msg.get("item_list", []) or []:
        if item.get("type") == 1:
            t = (item.get("text_item") or {}).get("text", "")
            if t:
                parts.append(t)
    return "".join(parts).strip()


MSGCACHE = os.path.join(BASE, "msg_cache.json")
MSGCACHE_MAX = 300
HISTORY_DIR = os.path.join(BASE, "history")
HISTORY_MAX_LINES = 60  # 每人最多保留 60 条（约 30 轮），超出从头丢弃


def load_msg_cache():
    try:
        with open(MSGCACHE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# 回复送达账本（2026-11-04 去重粒度修复）：inbound msg_id → 送达记录。
# 旧逻辑按回复文本查 msg_cache，同一句话发给不同消息会误判 FINISH_DUP
# （11-04 "1" 的"链路是通的。"撞上 11-03 同文回复，确认被拦；且同单
# 文字已出、图片待发时也会误拦、丢掉待发图片）。新逻辑只认本单 id：
# 只有本单整单成功送达过才抑制；不同消息、部分送达都不抑制。
# 文件名是纯数字的 outbox 任务就是普通回复（flash-/briefing-/market-/
# ack_/reply-/*-digest 都有前缀，不会误认）。只在整单成功归档时写，
# 部分送达不写（靠 parts 账本续发剩余分片）。
REPLY_MAP = os.path.join(BASE, "reply_map.json")
REPLY_MAP_KEEP_DAYS = 30


def load_reply_map():
    """Load the reply-delivery ledger (fail-open: corrupt/missing → {})."""
    try:
        with open(REPLY_MAP) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print("REPLY_MAP_LOAD_FAIL", e, flush=True)
        return {}


def record_reply_delivered(msg_id, text):
    """Record that the reply for inbound msg_id was fully delivered.

    Called only when _send_one_file archives a pure-digit reply file to
    sent/ (all parts delivered). Load-modify-write under SEND_LOCK, so no
    concurrent writer can lose the update. Prunes entries older than
    REPLY_MAP_KEEP_DAYS (ids are unique, pruning is hygiene only and can
    never suppress a different message's reply; unparseable entries are
    kept, never deleted on a quirk). Failures only warn: the send already
    succeeded; a missed record just means a future finish might resend
    (the pre-fix risk level), never a loss.
    """
    if not msg_id:
        return
    try:
        m = load_reply_map()
        now = time.time()
        cutoff = now - REPLY_MAP_KEEP_DAYS * 86400
        pruned = {}
        for k, v in m.items():
            try:
                ts = datetime.datetime.fromisoformat(
                    v.get("ts", "")).timestamp()
            except Exception:
                pruned[k] = v
                continue
            if ts >= cutoff:
                pruned[k] = v
        pruned[str(msg_id)] = {
            "ts": datetime.datetime.fromtimestamp(now).isoformat(),
            "text": (text or "")[:120],
        }
        _atomic_write_json(REPLY_MAP, pruned)
    except Exception as e:
        print("REPLY_MAP_FAIL", msg_id, e, flush=True)


def append_history(user_id, role, text):
    """Append one turn to the per-user conversation log.

    CowAgent keeps Session.messages in memory inside a long-lived process.
    Our workers are short-lived (one agent invocation per wake-up), so the
    session lives on disk: history/<user_id>.jsonl, append-only, trimmed
    from the head when over HISTORY_MAX_LINES (like discard_exceeding).
    Workers read the tail on each wake-up to get conversation context.
    """
    if not user_id or not text:
        return
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        safe = safe_user_id(user_id)
        if not safe:
            return
        path = os.path.join(HISTORY_DIR, safe + ".jsonl")
        line = json.dumps({"ts": time.time(), "role": role, "text": text},
                          ensure_ascii=False) + "\n"
        with open(path, "a") as f:
            f.write(line)
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > HISTORY_MAX_LINES:
            # 截头用 tmp + os.replace：裸 open("w") 截断期间读者会看到空文件
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w") as f:
                f.writelines(lines[-HISTORY_MAX_LINES:])
            os.replace(tmp, path)
    except Exception as e:
        print("HISTORY_FAIL", e, flush=True)


def cache_msg_text(cache, mid, text):
    """Cache recent message texts (both directions) so quoted messages can be resolved.

    Merges with the on-disk file before writing: the receiver holds a long-lived
    in-memory copy while cmd_send (a separate short-lived process) also writes.
    Without the merge, whichever process writes last silently wipes the other's
    entries (this broke quote-reply resolution for bot-sent messages).
    """
    if not mid or not text:
        return
    try:
        with open(MSGCACHE) as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            for k, v in disk.items():
                if k not in cache:
                    cache[k] = v
    except Exception:
        pass
    # LRU：重复缓存的条目移到末尾（最新），否则它会按旧位置被当成最老条目驱逐。
    s_mid = str(mid)
    if s_mid in cache:
        cache.pop(s_mid, None)
    cache[s_mid] = text
    while len(cache) > MSGCACHE_MAX:
        cache.pop(next(iter(cache)))
    try:
        _atomic_write_json(MSGCACHE, cache)
    except Exception as e:
        print("CACHE_SAVE_FAIL", e, flush=True)


# --- 引用缓存（2026-11-19，借鉴上游 quote_cache.json）---
# 旧的 300 条 msg_cache 太小，旧消息被引用时经常解析不到原文。
# 新缓存 30 天 / 1 万条，by_id + by_svr 双索引，机器人自己发出去的
# 消息确认成功后也进缓存（引用自己的回复时不用等下一轮回显）。
def load_quote_cache():
    try:
        with open(QUOTE_CACHE_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {"by_id": d.get("by_id", {}) if isinstance(d.get("by_id"), dict) else {},
                    "by_svr": d.get("by_svr", {}) if isinstance(d.get("by_svr"), dict) else {}}
    except (OSError, ValueError):
        pass
    return {"by_id": {}, "by_svr": {}}


def save_quote_cache(d):
    try:
        by_id = d.get("by_id", {})
        cutoff = time.time() - QUOTE_CACHE_TTL
        kept = {}
        for k, v in by_id.items():
            if isinstance(v, dict) and float(v.get("at", 0) or 0) >= cutoff:
                kept[k] = v
        while len(kept) > QUOTE_CACHE_CAP:  # 最老优先驱逐（按插入顺序）
            kept.pop(next(iter(kept)))
        d = {"by_id": kept, "by_svr": d.get("by_svr", {})}
        _atomic_write_json(QUOTE_CACHE_FILE, d)
    except Exception as e:
        print("QUOTE_CACHE_SAVE_FAIL", e, flush=True)


def record_quote(msg_id, text, kind="text", from_uid="", media_path=None,
                 svr_id=None):
    """记一条可被引用的消息（入站 + 本机器人已发出的都记）。绝不抛异常。"""
    if not msg_id or not text:
        return
    try:
        d = load_quote_cache()
        d["by_id"][str(msg_id)] = {
            "text": str(text)[:500], "kind": kind,
            "from": str(from_uid or ""), "at": time.time(),
            "media_path": media_path or "",
        }
        if svr_id:
            d["by_svr"][str(svr_id)] = str(msg_id)
        save_quote_cache(d)
    except Exception as e:
        print("QUOTE_CACHE_FAIL", e, flush=True)


def resolve_quote(qid, svr_id=None):
    """按 msg_id（或 svr_id）查被引原文；查不到返回 ""（调用方再回退 msg_cache）。"""
    if not qid and not svr_id:
        return ""
    try:
        d = load_quote_cache()
        mid = str(qid) if qid else d["by_svr"].get(str(svr_id), "")
        if mid:
            ent = d["by_id"].get(mid)
            if isinstance(ent, dict):
                return ent.get("text", "") or ""
    except Exception:
        pass
    return ""


def _load_token_stale():
    try:
        with open(TOKEN_STALE_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _note_token_stale():
    """记一次 -14：返回首次出现的时间戳（没有则现在建）。"""
    d = _load_token_stale()
    first = d.get("first_seen")
    if not first:
        first = time.time()
        try:
            _atomic_write_json(TOKEN_STALE_FILE, {"first_seen": first})
        except Exception:
            pass
    return float(first)


def _clear_token_stale():
    try:
        os.remove(TOKEN_STALE_FILE)
    except OSError:
        pass


def _resp_msg_id(r):
    """Extract the server-assigned message id from a send response.

    Confirmed 2026-09-29: the response carries top-level "message_id".
    Other shapes are kept as harmless fallbacks.
    """
    if isinstance(r, dict):
        for k in ("message_id", "msg_id", "msgid", "id"):
            if r.get(k):
                return r.get(k)
        data = r.get("data")
        if isinstance(data, dict):
            for k in ("message_id", "msg_id", "msgid", "id"):
                if data.get(k):
                    return data.get(k)
    return None


def extract_ref_text(msg, cache):
    """Resolve a quoted message to a '[引用: ...]' prefix.

    Real ilink behavior (captured 2026-09-29): ref_msg carries ONLY the quoted
    message's msg_id (message_item.type == 0), not its text -- CowAgent's
    parser assumes inline title/text and misses it. We resolve the id against
    recent messages cached by the receiver (both incoming and our own replies).

    内存缓存 miss 时回读一次磁盘：接收器是长驻进程，内存缓存只在启动时
    加载过一次，之后 cmd_send 进程写进磁盘的新条目它看不到；不回读，
    引用刚发出去的机器人回复就解析不到。
    """
    for item in msg.get("item_list", []) or []:
        ref = item.get("ref_msg") or {}
        if not ref:
            continue
        ref_mi = ref.get("message_item") or {}
        ref_title = ref.get("title", "") or ""
        ref_body = ""
        if ref_mi.get("type") == 1:  # inline text (CowAgent's assumed shape, kept as fallback)
            ref_body = (ref_mi.get("text_item") or {}).get("text", "") or ""
        qid = str(ref_mi.get("msg_id", "") or "")
        svr_id = str(ref.get("svr_id", "") or ref_mi.get("svr_id", "") or "")
        if not ref_body and (qid or svr_id):
            # 2026-11-19：先查 30 天 / 1 万条的持久引用缓存，再回退 300 条 msg_cache
            ref_body = resolve_quote(qid, svr_id)
        if not ref_body and qid:
            ref_body = cache.get(qid, "")
            if not ref_body:
                disk = load_msg_cache()
                if isinstance(disk, dict) and disk:
                    for k, v in disk.items():
                        if k not in cache:
                            cache[k] = v
                    ref_body = cache.get(qid, "")
        bits = [p for p in (ref_title, ref_body) if p]
        if bits:
            return "[引用: %s]\n" % " | ".join(bits)
        return "[引用: 早些的消息]\n"
    return ""


def find_ref_image_path(msg):
    """Resolve local path for a quoted inbound image.

    用户引用一张历史图片提问时，ref_msg 只带被引用消息的 msg_id；
    接收器下载入站图片时按 media/in_<qid>.jpg 落盘，命中即把路径给 worker，
    worker 才能看到被引用的图。非图片引用（无对应文件）返回 ""。
    """
    for item in msg.get("item_list", []) or []:
        ref = item.get("ref_msg") or {}
        ref_mi = ref.get("message_item") or {}
        qid = str(ref_mi.get("msg_id", "") or "")
        if qid:
            cand = os.path.join(MEDIA, "in_%s.jpg" % qid)
            if os.path.isfile(cand):
                return cand
    return ""


def split_text(text, limit=CHUNK):
    """Split text into chunks, preferring paragraph/line boundaries (from CowAgent)."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


SEND_EXPIRED_MARKER = os.path.join(BASE, ".send_session_expired")


def check_send_response(resp, fn=""):
    """Detect ilink -14 (session expired) on the send side (from CowAgent).

    The receiver already handles -14 on get_updates; this covers the blind
    spot where sending fails because the login went stale. Drops a marker
    file so the hook/sweeper can tell the user to rescan.
    """
    if not isinstance(resp, dict):
        return
    if resp.get("ret") == -14 or resp.get("errcode") == -14:
        # 2026-11-19：-14 分级。1 小时内连续出现算 stale：静默，不落
        # 过期标记（hook 不会吵着重扫）；超过 1 小时才算 expired，落标记。
        first_seen = _note_token_stale()
        if time.time() - first_seen < TOKEN_STALE_WINDOW:
            print("TOKEN_STALE_SEND", fn, flush=True)
            return
        print("SESSION_EXPIRED_SEND", fn, flush=True)
        with open(SEND_EXPIRED_MARKER, "w") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fn))


# --- 发送安全（2026-11-19，借鉴上游 send ledger / audit / text guard）---
# 教训：2026-10-27 上游分身半夜把 "--key" 当消息发出去了——worker 拼
# 命令时漏了正文占位符，桥对正文没有任何校验就原样发出。本文件此前
# 也没有这道校验，现补上：空正文或以 "--" 开头的正文一律拒绝发送。
def validate_send_text(txt):
    """-> None 表示可发，否则返回原因（"empty" / "looks-like-flag"）。"""
    if txt is None or (isinstance(txt, str) and txt.strip() == ""):
        return "empty"
    if isinstance(txt, str) and txt.startswith("--"):
        return "looks-like-flag"
    return None


class SendLedgerCorrupt(Exception):
    """发送幂等账本损坏：fail-closed，调用方必须不发、留文件待查。"""


def _load_send_ledger():
    try:
        with open(SEND_LEDGER) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("ledger not a dict")
        return d
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SendLedgerCorrupt(str(e))


def _save_send_ledger(d):
    now = time.time()
    d = {k: v for k, v in d.items()
         if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < SEND_LEDGER_TTL}
    _atomic_write_json(SEND_LEDGER, d)


class _ledger_locked:
    """send_ledger.json 的跨进程锁（fcntl，随 with 块释放）。"""

    def __enter__(self):
        self.fh = open(SEND_LEDGER_LOCK, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def _new_client_id():
    return "muse-wechat-" + uuid.uuid4().hex[:8]


def resolve_send_intent(key):
    """发送前先记 intent：返回 (client_id, already_sent)。

    同一个 key（文件:分片）重复解析返回同一个 client_id：重试时服务端
    按 client_id 去重，即使第一次调用服务端收到了、回执丢了，也不会
    让用户收到两条。already_sent=True 表示本分片已确认送达，调用方
    应直接跳过 API 调用。账本损坏抛 SendLedgerCorrupt（fail-closed）。
    """
    with _ledger_locked():
        ledger = _load_send_ledger()
        ent = ledger.get(key)
        if isinstance(ent, dict) and ent.get("status") == "sent":
            return ent.get("client_id", ""), True
        if isinstance(ent, dict) and ent.get("client_id"):
            return ent["client_id"], False
        cid = _new_client_id()
        ledger[key] = {"client_id": cid, "at": time.time(), "status": "intent"}
        _save_send_ledger(ledger)
        return cid, False


def mark_send_intent_sent(key):
    """确认送达后把 intent 翻成 sent（失败只记日志，不翻转发送结果）。"""
    try:
        with _ledger_locked():
            ledger = _load_send_ledger()
            ent = ledger.get(key)
            if isinstance(ent, dict) and ent.get("client_id"):
                ent["status"] = "sent"
                ent["at"] = time.time()
            else:
                ledger[key] = {"client_id": _new_client_id(),
                               "at": time.time(), "status": "sent"}
            _save_send_ledger(ledger)
    except Exception as e:
        print("SEND_LEDGER_MARK_FAIL", key, e, flush=True)


def append_send_audit(direction, to, client_id, result, attempt,
                      error=None, elapsed_ms=None):
    """发送审计账本：一行一次尝试，绝不记正文、不记 token；失败永不抛异常。"""
    try:
        entry = {"ts": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
            "direction": direction, "to": str(to or ""),
            "client_id": client_id or "", "result": result,
            "attempt": int(attempt)}
        if error is not None:
            entry["error"] = str(error)[:200]
        if elapsed_ms is not None:
            entry["elapsed_ms"] = int(elapsed_ms)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(SEND_AUDIT, "a") as f:
            f.write(line)
        # 超过上限时截断到最近 1000 行（读-改-写在追加后做，失败只记日志）
        try:
            with open(SEND_AUDIT) as f:
                lines = f.readlines()
            if len(lines) > SEND_AUDIT_CAP:
                tmp = "%s.%d.tmp" % (SEND_AUDIT, os.getpid())
                with open(tmp, "w") as f:
                    f.writelines(lines[-SEND_AUDIT_CAP:])
                os.replace(tmp, SEND_AUDIT)
        except OSError:
            pass
    except Exception:
        pass


def _call_with_client_id(fn, *args, **kwargs):
    """调 CowAgent 的 send_* 方法并传入 ledger client_id。

    旧版 CowAgent / 测试 FakeApi 没有 client_id 参数时 TypeError，
    回退到不带 client_id 的调用（旧行为）。TypeError 只可能来自
    参数绑定（调用前），不会发生在已发出请求之后，故回退不会重发。
    """
    cid = kwargs.pop("client_id", None)
    if cid:
        try:
            return fn(*args, client_id=cid, **kwargs)
        except TypeError:
            print("SEND_CLIENT_ID_FALLBACK", fn.__name__, flush=True)
    return fn(*args, **kwargs)


def _send_text_omit_token(api, to, txt, client_id=None):
    """hermes-agent 的野路子（2026-10-08）：完全省略 context_token 字段重发。

    对 "token 过期" 类 ret=-2 可能救回；配额耗尽类大概率无效。
    只在 -2 时试一次，成功则返回响应，失败返回 None。
    2026-11-19：omit 重试必须复用同一个 client_id（否则服务端去重失效）。
    注意：本地 ret=-2 首先按配额耗尽 fail-fast（见 _cmd_send_locked），
    这里的 omit 只在 send_text_retry 内部、且上游语义要求时才走，
    绝不把 -2 当成 stale-token 无条件重试——那是上游的逻辑，本地不抄。
    """
    refusal = validate_send_text(txt)
    if refusal:
        print("SEND_TEXT_REFUSED", "omit-token", refusal, flush=True)
        return {"ret": -3, "errmsg": "refused: " + refusal}
    try:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id or uuid.uuid4().hex[:16],
                "message_type": 2,  # BOT
                "message_state": 2,  # FINISH
                "item_list": [{"type": 1, "text_item": {"text": txt}}],
                # context_token 字段完全省略，不传空字符串
            }
        }
        r = api._post("ilink/bot/sendmessage", body, retries=0)
        check_send_response(r, "omit-token")
        return r
    except Exception as e:
        print("SEND_EXC_OMIT", e, flush=True)
        return None


def send_text_retry(api, to, ctx, txt, fn="", client_id=None):
    """Send one text chunk.

    2026-10-08 简化（Peter：重试叠三层无意义）：只做一次 api.send_text，
    底层 _post 已有连接重试；-2 则试一次 omit-token fallback，不再 3s 睡眠重试。
    2026-10-08 新增：分段计时（SLOW_SEND 诊断慢发送）。
    2026-11-19：正文先过 validate_send_text（空 / "--" 开头直接拒绝，
    不调 API、不写账本）；发送走 ledger client_id（omit 重试复用同一 id）。
    """
    refusal = validate_send_text(txt)
    if refusal:
        print("SEND_TEXT_REFUSED", fn, refusal, flush=True)
        append_send_audit("proactive" if _is_proactive(fn) else "reply",
                          to, client_id or "", "refused", 1, error=refusal)
        return {"ret": -3, "errmsg": "refused: " + refusal}
    t0 = time.time()
    try:
        r = _call_with_client_id(api.send_text, to, txt, ctx,
                                 client_id=client_id)
    except Exception as e:
        print("SEND_EXC", fn, e, "dt=%.1fs" % (time.time() - t0), flush=True)
        append_send_audit("proactive" if _is_proactive(fn) else "reply",
                          to, client_id or "", "exception", 1, error=e,
                          elapsed_ms=int((time.time() - t0) * 1000))
        return None
    dt = time.time() - t0
    if dt > 10:
        print("SLOW_SEND_TEXT", fn, "dt=%.1fs ret=%s"
              % (dt, r.get("ret") if r else None), flush=True)
    if r is not None:
        check_send_response(r, fn)
        if r.get("ret", 0) == 0:
            append_send_audit("proactive" if _is_proactive(fn) else "reply",
                              to, client_id or "", "ok", 1,
                              elapsed_ms=int(dt * 1000))
            return r
    # -2：试一次去 token 重发（hermes-agent 招数）
    if r is not None and r.get("ret") == -2:
        print("TRY_OMIT_TOKEN", fn, flush=True)
        t0 = time.time()
        r2 = _send_text_omit_token(api, to, txt, client_id=client_id)
        dt = time.time() - t0
        if dt > 10:
            print("SLOW_OMIT_TOKEN", fn, "dt=%.1fs" % dt, flush=True)
        if r2 is not None and r2.get("ret", 0) == 0:
            print("OMIT_TOKEN_OK", fn, flush=True)
            append_send_audit("proactive" if _is_proactive(fn) else "reply",
                              to, client_id or "", "ok", 2,
                              elapsed_ms=int(dt * 1000))
            return r2
        print("OMIT_TOKEN_FAIL", fn, flush=True)
        append_send_audit("proactive" if _is_proactive(fn) else "reply",
                          to, client_id or "", "fail", 2, error=r2,
                          elapsed_ms=int(dt * 1000))
    elif r is not None:
        append_send_audit("proactive" if _is_proactive(fn) else "reply",
                          to, client_id or "", "fail", 1, error=r,
                          elapsed_ms=int(dt * 1000))
    return r



def find_image_item(msg):
    """Return the image item dict (type 2) from a raw message, or None."""
    for item in msg.get("item_list", []) or []:
        if item.get("type") == 2:
            return item
        ref_mi = (item.get("ref_msg") or {}).get("message_item", {})
        if ref_mi.get("type") == 2:
            return ref_mi
    return None


def extract_voice_text(msg):
    """Return server-side transcription of a voice message (type 3), or ''."""
    for item in msg.get("item_list", []) or []:
        if item.get("type") == 3:
            t = (item.get("voice_item") or {}).get("text", "")
            if t:
                return t.strip()
    return ""


def download_inbound_image(api, item, mid):
    """Download an inbound image message from Weixin CDN. Returns local path or ''.

    2026-11-19：断点续传（借鉴上游 download_media_item）：加密密文先以
    HTTP Range 分段拉到 .enc.part（中断后下次从已下载处续传，最多 4 次，
    退避 2*(attempt+1)s），完整后再 AES-128-ECB 解密落盘；100MB 上限。
    服务端不支持 Range（回 200 全量）时从头重下；续传路径任何异常都
    回退到旧的 download_media_from_cdn 单次下载。
    """
    info = item.get("image_item", {})
    media = info.get("media", {})
    encrypt_param = media.get("encrypt_query_param", "")
    aes_key = info.get("aeskey", "") or media.get("aes_key", "")
    if not encrypt_param or not aes_key:
        print("IMG_NOPARAM", mid, flush=True)
        return ""
    os.makedirs(MEDIA, exist_ok=True)
    save_path = os.path.join(MEDIA, "in_%s.jpg" % mid)
    if os.path.isfile(save_path):
        return save_path
    try:
        key_bytes = _parse_aes_key(aes_key)
    except Exception as e:
        print("IMG_FAIL", mid, "bad aes key", e, flush=True)
        return ""
    cdn_base = getattr(api, "cdn_base_url", "") or CDN_BASE_URL
    from urllib.parse import quote
    url = "%s/download?encrypted_query_param=%s" % (
        cdn_base, quote(encrypt_param))
    part_path = save_path + ".enc.part"
    resumed = os.path.isfile(part_path)
    if _cow_cdn_session is not None and _cow_decrypt is not None:
        try:
            session = _cow_cdn_session()
            total = None
            for attempt in range(4):
                downloaded = (os.path.getsize(part_path)
                              if os.path.isfile(part_path) else 0)
                headers = ({"Range": "bytes=%d-" % downloaded}
                           if downloaded else {})
                try:
                    resp = session.get(url, headers=headers, timeout=60,
                                       stream=True)
                except Exception as e:
                    print("IMG_RETRY", mid, "attempt", attempt + 1, e, flush=True)
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code == 416:  # 已下载的比服务端多：从头来
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code == 200 and downloaded:
                    downloaded = 0  # 服务端不支持 Range：全量重下
                if resp.status_code not in (200, 206):
                    print("IMG_RETRY", mid, "http", resp.status_code, flush=True)
                    time.sleep(2 * (attempt + 1))
                    continue
                cr = resp.headers.get("Content-Range", "")
                if cr and "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                elif resp.headers.get("Content-Length"):
                    try:
                        total = downloaded + int(resp.headers["Content-Length"])
                    except ValueError:
                        total = None
                if total and total > MEDIA_MAX_BYTES:
                    print("IMG_TOO_LARGE", mid, total, flush=True)
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                    return ""
                mode = "ab" if resp.status_code == 206 and downloaded else "wb"
                try:
                    with open(part_path, mode) as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                except Exception as e:  # IncompleteRead 等：当次未完成，留 part 续传
                    print("IMG_RETRY", mid, "incomplete", e, flush=True)
                    time.sleep(2 * (attempt + 1))
                    continue
                downloaded = (os.path.getsize(part_path)
                              if os.path.isfile(part_path) else 0)
                if total is None or downloaded >= total:
                    break
                time.sleep(2 * (attempt + 1))
            else:
                print("IMG_FAIL", mid, "resume exhausted", flush=True)
                return ""
            with open(part_path, "rb") as f:
                encrypted = f.read()
            if total and len(encrypted) < total:
                print("IMG_FAIL", mid, "short read", flush=True)
                return ""
            decrypted = _cow_decrypt(encrypted, key_bytes)
            with open(save_path, "wb") as f:
                f.write(decrypted)
            try:
                os.remove(part_path)
            except OSError:
                pass
            print("IMG_SAVED", mid, save_path,
                  "resumed" if resumed else "fresh", flush=True)
            return save_path
        except Exception as e:
            print("IMG_RESUME_FALLBACK", mid, e, flush=True)
    try:
        download_media_from_cdn(cdn_base, encrypt_param, aes_key, save_path)
        print("IMG_SAVED", mid, save_path, flush=True)
        return save_path
    except Exception as e:
        print("IMG_FAIL", mid, e, flush=True)
        return ""


def _parse_aes_key(aes_key):
    """解析 CowAgent 两种 aes_key 格式 -> 16 字节原始 key（失败抛 ValueError）。"""
    try:
        kb = bytes.fromhex(aes_key)
        if len(kb) == 16:
            return kb
    except (ValueError, TypeError):
        pass
    decoded = base64.b64decode(aes_key)
    if len(decoded) == 32:
        kb = bytes.fromhex(decoded.decode("ascii"))
        if len(kb) == 16:
            return kb
    elif len(decoded) == 16:
        return decoded
    raise ValueError("Invalid AES key length")


def _is_msg_already_processed(mid):
    """磁盘去重兜底：msg_id 是否已在 pipeline 目录里（防重启后重复处理）。

    内存 seen 只在进程内有效；接收器重启后 seen 为空，buf 游标若回退
    （或服务端重推），同一条消息会再次进 inbox。磁盘上有记录 = 已处理过。
    """
    fn = "%s.json" % mid
    for d in (INBOX, PROCESSING, DONE, SENT):
        if os.path.exists(os.path.join(d, fn)):
            return True
    return False


def cmd_receive(drain=False):
    # 单实例锁：flock 随进程结束自动释放。pgrep + kill 去重曾多次误杀/漏杀
    # （2026-09-30 双实例、2026-10-10 误杀真进程）；锁在内核层面保证同一时刻
    # 只有一个接收器，第二个 receive 直接退出，不再靠外部脚本数进程。
    recv_lock_fh = open(RECEIVE_LOCK, "w")
    try:
        fcntl.flock(recv_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("RECV_ALREADY_RUNNING", flush=True)
        recv_lock_fh.close()
        return

    creds = load_creds()
    api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                    token=creds.get("token", ""))
    buf = open(BUF).read().strip() if os.path.exists(BUF) else ""
    # 有序去重集：超 SEEN_MAX 按 LRU 丢最老（24/7 长驻，裸 set 会无限增长）；
    # 丢掉的旧 id 由 _is_msg_already_processed 用磁盘兜底。
    import collections
    seen = collections.OrderedDict()
    failures = 0
    msg_cache = load_msg_cache()
    while True:
        try:
            resp = api.get_updates(buf)
        except Exception as e:
            failures += 1
            print("RECV_ERROR", e, flush=True)
            if failures > 5:
                print("RECV_GIVEUP")
                sys.exit(1)
            time.sleep(5)
            continue
        failures = 0
        ret = resp.get("ret", 0)
        errcode = resp.get("errcode", 0)
        if ret == SESSION_EXPIRED or errcode == SESSION_EXPIRED:
            # 2026-11-19：-14 分级（借鉴上游 token stale 窗口）。1 小时内
            # 连续出现算 stale：静默退避 60s，不报过期、不退出；超过 1 小时
            # 才算 expired，走原来的 SESSION_EXPIRED 退出流程。
            first_seen = _note_token_stale()
            if time.time() - first_seen < TOKEN_STALE_WINDOW:
                print("TOKEN_STALE", "age=%ds" % int(time.time() - first_seen),
                      flush=True)
                time.sleep(60)
                continue
            print("SESSION_EXPIRED", flush=True)
            sys.exit(2)
        if ret != 0 or errcode != 0:
            print("RECV_API_ERROR", ret, errcode, flush=True)
            time.sleep(5)
            continue
        _clear_token_stale()  # 任何一次成功轮询都清掉 stale 标记
        # 游标先不写盘：本轮消息处理完再写。崩溃在循环中途时游标未推进，
        # 重启后会重拉本轮消息（磁盘去重兜底，不会重复回复）；先写游标则丢消息。
        new_buf = resp.get("get_updates_buf", buf)
        for m in resp.get("msgs", []) or []:
            mtype = m.get("message_type", 0)
            if mtype == 2:
                # 自己发出去的消息：记入引用解析缓存，不回复
                t2 = extract_text(m)
                if t2:
                    mid2 = m.get("message_id", m.get("seq", ""))
                    cache_msg_text(msg_cache, mid2, t2)
                    record_quote(mid2, t2, kind="text",
                                 svr_id=m.get("svr_id", ""))
                continue
            if mtype != 1:
                continue
            mid = str(m.get("message_id", m.get("seq", uuid.uuid4().hex[:8])))
            if mid in seen or _is_msg_already_processed(mid):
                continue
            seen[mid] = True
            while len(seen) > SEEN_MAX:
                seen.popitem(last=False)
            if drain:
                continue
            # 2026-11-19 主人白名单（接收阶段强制）：名单外不入队、不唤醒、
            # 不下载媒体、不重置配额；id 只进待批名单。没配 owner.json 时
            # 默认拒绝所有人；绑定窗口开着时，第一个发信人成为主人。
            from_uid = m.get("from_user_id", "")
            owner = load_owner()
            if not owner["owner_ids"]:
                bind = load_bind_state()
                if bind:
                    save_owner(from_uid, [from_uid])
                    clear_bind_state()
                    _send_bind_welcome(api, from_uid, m.get("context_token", ""))
                    print("BIND_BOUND", from_uid, flush=True)
                    owner = load_owner()
                else:
                    note_unknown_sender(from_uid)
                    print("UNKNOWN_SENDER", mid, from_uid, flush=True)
                    continue
            elif from_uid not in owner["owner_ids"]:
                note_unknown_sender(from_uid)
                print("UNKNOWN_SENDER", mid, from_uid, flush=True)
                continue
            text = extract_text(m)
            if not text:
                vt = extract_voice_text(m)
                if vt:
                    text = "[语音] " + vt
            image_path = ""
            img_item = find_image_item(m)
            if img_item:
                image_path = download_inbound_image(api, img_item, mid)
            if not image_path:
                # 无直发图片时，试引用图片（用户引用历史图片提问）
                image_path = find_ref_image_path(m)
            if not text and not image_path:
                # 用户发了视频/文件/语音：以前静默丢弃，现在转成标记让 worker 看到、
                # 由 Muse 如实回复"收到了但暂时看不了"，而不是装没看见。
                # 语音无转写也进 [语音]：此前无转写语音被跳过，用户得不到任何回应；
                # RESPONDER 会让 worker 如实说没收到语音文字、请对方发文字。
                media_note = ""
                for item in m.get("item_list", []) or []:
                    itype = item.get("type", 0)
                    if itype == 5:
                        media_note = "[视频]"
                        break
                    if itype == 4:
                        media_note = "[文件]"
                        break
                    if itype == 3:
                        media_note = "[语音]"
                        break
                if not media_note:
                    continue
                text = media_note
            if not text and image_path:
                text = "[图片]"
            # 引用解析：把被引用消息的原文拼到前面，worker 才知道用户在问哪一句
            ref_prefix = extract_ref_text(m, msg_cache)
            if ref_prefix:
                text = ref_prefix + text
            # 缓存收到的文字（含 [图片]/[语音]/[视频]/[文件] 标记），供后续引用解析
            cache_msg_text(msg_cache, mid, text)
            record_quote(mid, text, kind="text", from_uid=from_uid,
                         media_path=image_path or None,
                         svr_id=m.get("svr_id", ""))
            rec = {
                "msg_id": mid,
                "from": m.get("from_user_id", ""),
                "text": text,
                "context_token": m.get("context_token", ""),
                "session_id": m.get("session_id", ""),
                "ts": time.time(),
            }
            if image_path:
                rec["image"] = image_path
            _atomic_write_json(os.path.join(INBOX, mid + ".json"), rec)
            append_history(rec["from"], "user", text)  # 会话历史：用户侧
            _quota_reset()  # 用户入站：ilink 回复配额清零
            # 立即触发微信原生“对方正在输入…”（0.2s 反馈，不占 10 条配额；
            # 失败静默忽略，ack_timer 仍是兜底）
            _typing_ok = send_typing_indicator(api, rec["from"], command=1)
            print("TYPING", mid, _typing_ok, flush=True)
            desc = text[:30].replace("\n", " ") if text else "[图片]"
            print("INBOX", mid, desc, flush=True)

        buf = new_buf
        buf_tmp = "%s.%d.tmp" % (BUF, os.getpid())
        with open(buf_tmp, "w") as f:
            f.write(buf)
        os.replace(buf_tmp, BUF)


def _is_stale_ack(fn):
    """ack_<id>.json 是否已过期：同 id 的真正回复已经发出。

    sent/<id>.json 或 done/<id>.json 存在 = 回复已送达，
    此时再发"收到，正在想，稍等…"只会让用户困惑（2026-09-29 实测：
    ack 的 API 请求比真正回复晚到服务器，导致先看到答案、后看到"正在想"）。
    outbox/<id>.json 已存在 = 回复已写好、马上就发，此时 ack 也是多余的
    （用户下一秒就看到正文，不需要先看一条"正在想"）。
    过期则直接归档，不再发送。
    """
    if not fn.startswith("ack_"):
        return False
    base = fn[len("ack_"):]
    return (os.path.exists(os.path.join(SENT, base))
            or os.path.exists(os.path.join(DONE, base))
            or os.path.exists(os.path.join(OUTBOX, base)))


def cmd_send(only=None):
    """发送 outbox 任务。

    only=None（默认）：发送全部 outbox 文件（显式 `wxbot.py send`、
    ack_timer、proactive worker 的全量发送）。
    only="<fn>"：只发这一个文件（`wxbot.py finish <id>` 的收尾路径）。

    2026-10-16 隔离修复：finish 曾调全量 send，导致每次普通回复都
    顺带发走 outbox 里的快讯/早报/卡片（10-15 实锤 3 次）。finish 必须
    只发本单文件；proactive 只由显式 send 发。
    """
    creds = load_creds()
    api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                    token=creds.get("token", ""))
    msg_cache = load_msg_cache()  # 记下发出去的消息，供引用解析
    # 互斥：ack_timer 和 worker 可能同时起 send 进程。无锁时两个进程会各自
    # 看到 outbox 里对方的文件、按文件名排序发出，导致顺序错乱甚至重复发送
    # （2026-09-29 实测）。flock 随进程结束自动释放；单次发送有 15s 超时上限，
    # 不会无限持有锁。
    lock_fh = open(SEND_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        _cmd_send_locked(api, msg_cache, only=only)
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


def _parts_ledger_path(fn):
    """已发送分片账本：outbox/<fn>.parts.json，记录本任务已落地的分片。

    2026-09-29 实测：图文回复里文字先发成功、图片上传抛异常时，整单会留在
    outbox，下一次 wxbot.py send 从头重发，导致已落地的文字被重复发送
    （一晚发了 3 遍）。账本让重试跳过已落地的分片，只补没发完的部分。
    """
    return os.path.join(OUTBOX, fn + ".parts.json")


def _load_parts_ledger(fn):
    try:
        with open(_parts_ledger_path(fn)) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _mark_part_done(fn, parts, pid):
    parts.add(pid)
    _atomic_write_json(_parts_ledger_path(fn), sorted(parts))


def _clear_parts_ledger(fn):
    try:
        os.remove(_parts_ledger_path(fn))
    except OSError:
        pass


SEND_RETRY_ATTEMPTS = 3           # 任务级重试次数（含首次）
SEND_RETRY_BACKOFF = (5, 15, 30)  # 每次瞬时失败后的等待秒数，指数退避


def _send_priority_key(fn):
    """outbox 发送排序（2026-11-19）：普通回复（纯数字文件名）最先；
    proactive 按 job 的 priority 升序（1 紧急 → 2 普通 → 3 低优），同级
    按文件名。只做排序，不做配额门控（2026-10-08 Peter 决定：proactive
    不做本地预判，服务端是最终裁决）。"""
    stem = fn[:-5] if fn.endswith(".json") else fn
    if stem.isdigit():
        return (0, 0, fn)
    try:
        with open(os.path.join(OUTBOX, fn)) as f:
            prio = int(json.load(f).get("priority", 2) or 2)
    except Exception:
        prio = 2
    return (1, prio, fn)


def _cmd_send_locked(api, msg_cache, only=None):
    """加锁发送。only=None 发全部；only="<fn>" 只发这一个文件。

    finish 路径传 only=<id>.json：只发本单回复，绝不碰 outbox 里
    其他文件（快讯/早报/卡片等 proactive 只由显式 send 发）。
    滞留的同单 ack（ack_<id>.json）不用 finish 管：下次全量 send 时
    _is_stale_ack 会判它过期（sent/<id>.json 已存在）直接归档不发，
    绝不会迟发一条"正在想"。
    """
    # 账本文件 *.parts.json 也以 .json 结尾，必须排除，否则会被当成待发任务
    files = sorted((f for f in os.listdir(OUTBOX)
                    if f.endswith(".json") and not f.endswith(".parts.json")),
                   key=_send_priority_key)
    if only is not None:
        files = [f for f in files if f == only]
    for fn in files:
        path = os.path.join(OUTBOX, fn)
        if _is_stale_ack(fn):
            try:
                os.replace(path, os.path.join(SENT, fn))
            except OSError:
                pass
            print("SENT_SKIP_STALE_ACK", fn, flush=True)
            continue
        # ilink 配额门控（2026-10-08）：用户回复永远发送，不受限。
        # ack 延迟提示在配额>=5 时跳过（省配额给正文）。
        # proactive（快讯/卡片）：Peter 要求"有快讯就推"，不做本地预判，
        # 直接发，撞 -2 再 fail fast 排队（服务端是最终裁决）。
        if _is_proactive(fn):
            q = _load_quota()
            if fn.startswith("ack_") and q >= QUOTA_ACK_LIMIT:
                print("QUOTA_SKIP_ACK", fn, "quota=%d" % q, flush=True)
                try:
                    os.replace(path, os.path.join(SENT, fn))
                except OSError:
                    pass
                continue
        # 任务级重试：只对网络异常（请求断连/超时）退避重试；
        # ret=-2 是配额耗尽的确定性拒绝，重试永不成功，直接 fail fast 留 outbox
        # 等下次入站（2026-10-08 全网调研结论，hermes-agent #115359 同理）。
        # （2026-10-08 Peter 要求：失败时自动重新拉起发送，不再只靠 3 分钟兜底轮询
        #  ——该要求针对网络抖动；配额类 -2 不适用，已收回）
        for attempt in range(SEND_RETRY_ATTEMPTS):
            ok, transient = _send_one_file(api, msg_cache, fn, path)
            if ok:
                _quota_bump()
                break
            if not transient:
                break
            if attempt + 1 < SEND_RETRY_ATTEMPTS:
                wait = SEND_RETRY_BACKOFF[
                    min(attempt, len(SEND_RETRY_BACKOFF) - 1)]
                print("SEND_RETRY", fn,
                      "attempt %d/%d in %ds"
                      % (attempt + 2, SEND_RETRY_ATTEMPTS, wait), flush=True)
                time.sleep(wait)


def _is_transient_send_fail(resp):
    """判断发送失败是否值得重试。

    2026-10-08 修正：ret=-2 prepare failed 是 ilink 回复配额耗尽的确定性拒绝
    （约 10 条/用户入站，腾讯维护者确认），重试永不成功，直接 fail fast，
    文件留 outbox 等下次入站。只有 resp 为 None（请求断连/超时）才值得重试。
    -14 登录过期、-3 参数错误、文件缺失等是永久性失败，重试无意义。"""
    if resp is None:
        return True
    return False


def _send_one_file(api, msg_cache, fn, path):
    """发送单个 outbox 任务。返回 (ok, transient)：
    ok=True 整单成功；ok=False 且 transient=True 是瞬时故障、可重试；
    transient=False 是永久性失败，重试也发不出去。

    图文混排：文本分段发送的同时，后台线程提前跑图片 CDN 上传
    （上传本身不发给用户、不占配额，只是把加密包备好），文字发完直接
    send_image_item，省一次串行上传等待。上传失败不影响文字分片：
    文本已落地记账本，重试只补图片。
    0.5s 间隔只用在"后面还有分片/附件"时：最后一片发完直接收尾，
    不再空等半秒。
    """
    try:
        with open(path) as f:
            job = json.load(f)
        to, ctx, text = job["to"], job.get("context_token", ""), job.get("text", "")
        reply_parts = []  # 本次实际发出的内容，整单成功后一次性记入历史
        parts_done = _load_parts_ledger(fn)  # 已落地的分片，重试时跳过
        chunks = split_text(text) if text else [""]
        img = job.get("image")
        vid = job.get("video")
        fpath = job.get("file")
        has_media = bool(img or vid or fpath)

        # 图文并行：文本发送的同时，后台线程跑 CDN 上传流水线
        img_future = None
        if img and os.path.isfile(img) and "image" not in parts_done:
            from concurrent.futures import ThreadPoolExecutor
            executor = ThreadPoolExecutor(max_workers=1)
            img_future = executor.submit(upload_media_to_cdn, api, img, to, 1)

        for i, ch in enumerate(chunks):
            pid = "text:%d" % i
            if pid in parts_done:
                reply_parts.append(ch)  # 历史仍记全量
                continue
            if not ch and has_media and len(chunks) == 1:
                continue  # 纯图片单：跳过空文本分片，不发一条空消息
            refusal = validate_send_text(ch)
            if refusal:
                print("SEND_TEXT_REFUSED", fn, refusal, flush=True)
                return False, False
            key = fn + ":" + pid
            try:
                cid, already_sent = resolve_send_intent(key)
            except SendLedgerCorrupt:
                # 账本损坏 = fail-closed：不发，文件留 outbox 待人工查
                print("SEND_LEDGER_CORRUPT", fn, flush=True)
                return False, False
            if already_sent:
                # 服务端已收、回执丢了：跳过 API 调用，不让用户收到两条
                reply_parts.append(ch)
                _mark_part_done(fn, parts_done, pid)
                continue
            r = send_text_retry(api, to, ctx, ch, fn, client_id=cid)
            if not r or r.get("ret", 0) != 0:
                print("SEND_FAIL", fn, r, flush=True)
                return False, _is_transient_send_fail(r)
            mark_send_intent_sent(key)
            cache_msg_text(msg_cache, _resp_msg_id(r), ch)
            record_quote(_resp_msg_id(r), ch, kind="text")
            reply_parts.append(ch)
            _mark_part_done(fn, parts_done, pid)
            if (i + 1 < len(chunks)) or has_media:
                time.sleep(0.5)
        if img:
            if not os.path.isfile(img):
                print("SEND_FAIL", fn, "image not found:", img, flush=True)
                return False, False
            if "image" not in parts_done:
                try:
                    up = (img_future.result() if img_future
                          else upload_media_to_cdn(api, img, to, media_type=1))
                except Exception as e:
                    print("SEND_FAIL", fn, "image upload exc:", e, flush=True)
                    return False, True
                key = fn + ":image"
                try:
                    cid, already_sent = resolve_send_intent(key)
                except SendLedgerCorrupt:
                    print("SEND_LEDGER_CORRUPT", fn, flush=True)
                    return False, False
                if already_sent:
                    _mark_part_done(fn, parts_done, "image")
                else:
                    r = _call_with_client_id(
                        api.send_image_item, to, ctx,
                        up["encrypt_query_param"], up["aes_key_b64"],
                        up["ciphertext_size"], client_id=cid)
                    check_send_response(r, fn)
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "image:", r, flush=True)
                        return False, _is_transient_send_fail(r)
                    mark_send_intent_sent(key)
                    # 图片缓存带正文前 60 字：用户引用这条图片消息时，worker 能认出是哪张图
                    img_label = "[图片]" if not text else "[图片] %s" % text[:60]
                    cache_msg_text(msg_cache, _resp_msg_id(r), img_label)
                    record_quote(_resp_msg_id(r), img_label, kind="image",
                                 media_path=img)
                    _mark_part_done(fn, parts_done, "image")
            reply_parts.append("[图片]")
            if vid or fpath:
                time.sleep(0.5)
        if vid:
            if not os.path.isfile(vid):
                print("SEND_FAIL", fn, "video not found:", vid, flush=True)
                return False, False
            if "video" not in parts_done:
                up = upload_media_to_cdn(api, vid, to, media_type=2)
                key = fn + ":video"
                try:
                    cid, already_sent = resolve_send_intent(key)
                except SendLedgerCorrupt:
                    print("SEND_LEDGER_CORRUPT", fn, flush=True)
                    return False, False
                if already_sent:
                    _mark_part_done(fn, parts_done, "video")
                else:
                    r = _call_with_client_id(
                        api.send_video_item, to, ctx,
                        up["encrypt_query_param"], up["aes_key_b64"],
                        up["ciphertext_size"], client_id=cid)
                    check_send_response(r, fn)
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "video:", r, flush=True)
                        return False, _is_transient_send_fail(r)
                    mark_send_intent_sent(key)
                    cache_msg_text(msg_cache, _resp_msg_id(r), "[视频]")
                    record_quote(_resp_msg_id(r), "[视频]", kind="video",
                                 media_path=vid)
                    _mark_part_done(fn, parts_done, "video")
            reply_parts.append("[视频]")
            if fpath:
                time.sleep(0.5)
        if fpath:
            if not os.path.isfile(fpath):
                print("SEND_FAIL", fn, "file not found:", fpath, flush=True)
                return False, False
            if "file" not in parts_done:
                up = upload_media_to_cdn(api, fpath, to, media_type=3)
                key = fn + ":file"
                try:
                    cid, already_sent = resolve_send_intent(key)
                except SendLedgerCorrupt:
                    print("SEND_LEDGER_CORRUPT", fn, flush=True)
                    return False, False
                if already_sent:
                    _mark_part_done(fn, parts_done, "file")
                else:
                    # raw_size 缺失时回退本地文件大小（上传结果异常也不应炸掉整单）
                    raw_size = up.get("raw_size")
                    if not raw_size:
                        try:
                            raw_size = os.path.getsize(fpath)
                        except OSError:
                            raw_size = 0
                    r = _call_with_client_id(
                        api.send_file_item, to, ctx,
                        up["encrypt_query_param"], up["aes_key_b64"],
                        os.path.basename(fpath), raw_size, client_id=cid)
                    check_send_response(r, fn)
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "file:", r, flush=True)
                        return False, _is_transient_send_fail(r)
                    mark_send_intent_sent(key)
                    cache_msg_text(msg_cache, _resp_msg_id(r),
                                   "[文件] " + os.path.basename(fpath))
                    record_quote(_resp_msg_id(r),
                                 "[文件] " + os.path.basename(fpath),
                                 kind="file", media_path=fpath)
                    _mark_part_done(fn, parts_done, "file")
            reply_parts.append("[文件] " + os.path.basename(fpath))
        if os.path.exists(SEND_EXPIRED_MARKER):
            os.remove(SEND_EXPIRED_MARKER)
        # ack 延迟提示不记会话历史：它是"正在想"的占位提示，不是真正的回复；
        # 记进去 worker 再读上文时会看到一句自己没说过的"正在想"，是噪音。
        if not fn.startswith("ack_"):
            append_history(to, "assistant",  # 会话历史：整单成功才记
                           "\n".join(p for p in reply_parts if p) or "[空回复]")
            # 正式回复发出后取消“对方正在输入…”；仅普通回复文件：
            # proactive（快讯/早报/卡片/汇总）的取消会误关正在思考的回复的指示灯。
            if not fn.startswith(("flash-", "briefing-", "market-", "digest-")) \
                    and not fn.endswith("-digest.json"):
                send_typing_indicator(api, to, command=2)
        os.replace(path, os.path.join(SENT, fn))
        _clear_parts_ledger(fn)
        # 2026-11-04：整单成功才记回复送达账本（纯数字文件名 = 普通回复）。
        # 部分送达不记：文字已出、图片待发时 finish 必须继续续发，不能抑制。
        # 包在 try 里：记账失败绝不能把已成功的发送翻成失败。
        try:
            stem = fn[:-5] if fn.endswith(".json") else fn
            if stem.isdigit():
                record_reply_delivered(stem, text)
        except Exception as e:
            print("REPLY_MAP_FAIL", fn, e, flush=True)
        print("SENT", fn, flush=True)
        # 2026-10-11 19:10：任何路径发出的 flash/digest，成功后自动把
        # 各条标题记入共享去重池（防 responder finish 等旁路漏记导致重复推送）
        if fn.startswith("flash-"):
            try:
                _record_digest_items_to_shared_pool(
                    _extract_pool_titles(fn, text))
            except Exception as e:
                print("POOL_RECORD_FAIL", fn, e, flush=True)
        return True, False
    except Exception as e:
        print("SEND_ERROR", fn, e, flush=True)
        return False, True


MERGE_BACKLOG_THRESHOLD = 4  # outbox 里 flash 积压达到这个数就合并成一条汇总


def _record_digest_items_to_shared_pool(items):
    """merge-backlog 去重补记（2026-10-11 15:38 重复推送事故的根因修复）。

    digest 合并后原文件被删除，若各条标题不记入共享去重池
    （pushed-shared.json），后续快讯轮的去重检查会漏掉它们，
    同一新闻会被重复推送（15:38"特朗普称乌克兰该换总统"与
    14:01 digest 第 2 条重复就是这么发生的）。

    在合并时（而非发送时）登记：原文件已删除、内容已收进 digest，
    digest 要么在 outbox 待发、要么已发出，24 小时内再出现同题
    直接判重是正确的。写入失败不影响合并本身，只打日志。
    """
    try:
        try:
            with open(SHARED_POOL) as f:
                pool = json.load(f)
        except (OSError, ValueError):
            pool = {}
        pushed = pool.get("pushed")
        if not isinstance(pushed, list):
            pushed = []
            pool["pushed"] = pushed
        cst = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime.now(cst)
        existing = {e.get("title") for e in pushed if isinstance(e, dict)}
        n_new = 0
        for t in items:
            if t and t not in existing:
                pushed.append({"title": t, "ts": now.isoformat(),
                               "by": "flash-digest"})
                existing.add(t)
                n_new += 1
        # 顺手清理 48 小时前的旧记录（与快讯 cron 的清理口径一致）
        cutoff = now - datetime.timedelta(hours=48)
        kept = []
        for e in pushed:
            try:
                ts = datetime.datetime.fromisoformat(e.get("ts", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=cst)
            except (ValueError, AttributeError):
                continue
            if ts >= cutoff:
                kept.append(e)
        pool["pushed"] = kept
        _atomic_write_json(SHARED_POOL, pool)
        print("SHARED_POOL_RECORDED", n_new, flush=True)
    except Exception as e:
        print("SHARED_POOL_FAIL", e, flush=True)


def _extract_pool_titles(fn, text):
    """从已发送的 flash 文件提取应记入共享去重池的标题列表。

    2026-10-11 19:10 修复：14:01 经 responder finish 发出的 5 条 digest，
    其各条标题从未记入共享池（只记了整条 digest 文本），导致 18:41 的
    利雅得快讯去重漏检、18:56 的 digest 差点重复推送已送达内容。
    此后任何路径发出的 flash/digest，成功后都自动补记。
    """
    titles = []
    if "digest" in fn:
        # digest 文本格式："📰 快讯汇总（N条…）\n· 标题1\n· 标题2…"
        for line in (text or "").split("\n"):
            line = line.strip()
            if line.startswith("·"):
                t = line[1:].strip()
                if t:
                    titles.append(t)
    else:
        title = (text or "").lstrip().split("\n")[0].strip()
        title = re.sub(r"^[⚡\s]*快讯：?", "", title).strip()
        if title:
            titles.append(title)
    return titles


def _is_text_delivered(text, msg_cache):
    """检查文本是否已在 msg_cache 中（已送达微信）。

    2026-10-11 18:59 修复：merge-backlog 曾把已送达的 Isaias 文本
    （msg_cache 有真实 message_id 7514987327573018376，18:21–18:27 送达）
    重新收进 digest，造成重复推送。合并前必须排除已送达的条目，
    否则违背"不重复推送"承诺。
    msg_cache 格式 {message_id: text}，按 value 精确匹配。
    注意：cmd_finish 不再用它（2026-11-04 改按 reply_map 查单号）；
    仅 cmd_merge_backlog 还用它排除已送达文本。
    """
    if not text:
        return False
    for cached_text in msg_cache.values():
        if cached_text == text:
            return True
    return False


def cmd_merge_backlog():
    """合并积压的 flash：outbox 里未发送的 flash-* 文件达到阈值时，
    合并成一条"快讯汇总"后删除原文件，避免通道恢复后一条条轰炸。

    2026-10-08 Peter 要求（优化项 2）。幂等：汇总文件本身
    （*-digest.json）不参与再次合并；不足阈值时直接跳过。
    加锁执行，与 send 互斥，避免和正在发送的文件打架。
    2026-10-11 18:59：合并前查 msg_cache，已送达的文本不再收进
    汇总（防重复推送）；该文件直接删除（文本已出，图片为辅）。
    2026-10-16：剔除已送达后若只剩 1 条，不包成"1 条汇总"（单条
    本来就是完整快讯，包一层汇总是多此一举），留它原样待发。
    同分钟已有 digest 时吸收合并（不覆盖）：两轮合并撞到同一分钟
    文件名时，旧 digest 的标题必须收进新 digest，否则旧标题随覆盖丢失
    （原文件已在上一轮删除，标题只剩 digest 里有）。
    只删真正合并进来的文件：解析失败的文件不删，留着排查。
    2026-10-21 Peter 要求：只合并 2 小时内创建的文件，超 2 小时的
    旧文件不参与合并、单独保留待补发（新旧不混）。
    """
    lock_fh = open(SEND_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        # 2026-10-21 Peter 要求：digest 只合并 2 小时内创建的文件，
        # 超 2 小时的旧文件不参与合并、单独保留待补发。
        now = time.time()
        files = sorted(f for f in os.listdir(OUTBOX)
                       if f.startswith("flash-") and f.endswith(".json")
                       and not f.endswith(".parts.json")
                       and not f.endswith("-digest.json")
                       and (now - os.path.getmtime(os.path.join(OUTBOX, f)) < 7200))
        if len(files) < MERGE_BACKLOG_THRESHOLD:
            print("MERGE_SKIP", len(files), flush=True)
            return
        msg_cache = load_msg_cache()
        items, merged_fns, to = [], [], None
        for fn in files:
            try:
                with open(os.path.join(OUTBOX, fn)) as f:
                    job = json.load(f)
            except (OSError, ValueError):
                continue
            text = job.get("text", "") or ""
            if _is_text_delivered(text, msg_cache):
                # 文本已送达微信，不再收进汇总，直接删除原文件
                # （图片分片无需单独补发，新闻本身已出）
                try:
                    os.remove(os.path.join(OUTBOX, fn))
                except OSError:
                    pass
                _clear_parts_ledger(fn)
                print("MERGE_SKIP_DELIVERED", fn, flush=True)
                continue
            if to is None:
                to = job.get("to")
            title = text.lstrip().split("\n")[0].strip()
            title = re.sub(r"^[⚡\s]*快讯：?", "", title).strip()
            if title:
                items.append(title)
                merged_fns.append(fn)
        if not to or not items:
            print("MERGE_FAIL", "no usable content", flush=True)
            return
        if len(items) < 2:
            print("MERGE_SKIP_SINGLE", len(items), flush=True)
            return
        # 文件名时间戳用 Asia/Shanghai（VM 本地是 UTC，直接 strftime 会差 8 小时）
        ts = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
              .strftime("%Y%m%d%H%M"))
        digest_fn = "flash-%s-digest.json" % ts
        digest_path = os.path.join(OUTBOX, digest_fn)
        if os.path.exists(digest_path):
            # 同分钟已有 digest：把它的标题吸收进来，不覆盖丢标题
            try:
                with open(digest_path) as f:
                    old_digest = json.load(f)
                old_titles = _extract_pool_titles(digest_fn, old_digest.get("text", ""))
                for ot in reversed(old_titles):
                    if ot not in items:
                        items.insert(0, ot)
            except (OSError, ValueError):
                pass
        digest_text = ("📰 快讯汇总（%d条，通道恢复补发）\n" % len(items)
                       + "\n".join("· " + t for t in items))
        _atomic_write_json(digest_path, {"to": to, "text": digest_text})
        for fn in merged_fns:
            try:
                os.remove(os.path.join(OUTBOX, fn))
            except OSError:
                pass
            _clear_parts_ledger(fn)
        # 2026-10-11 15:38 重复推送事故：合并后各条标题必须记入共享去重池，
        # 否则后续快讯轮的去重检查漏掉它们，同一新闻会被重复推送。
        _record_digest_items_to_shared_pool(items)
        print("MERGED", len(merged_fns), "->", digest_fn, flush=True)
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


def _finish_one(msg_id, expected_claim_ts=None, do_recheck=True):
    """收尾核心（cmd_finish 与 watch-outbox 共用）：发 outbox/<id>.json → processing→done → 写耗时打点。返回 0 成功 / 1 失败（留给兜底）/ 2 FINISH_RECHECK（仅 do_recheck 时）。

    前置：processing/<id>.json 已存在（hook 脚本认领时 mv 过来，并打了 claim_ts），
    outbox/<id>.json 已由 worker 写好（含 to/context_token/text，可选 image/video/file）。
    发送复用 cmd_send 的加锁逻辑，但只发本单文件（only=<id>.json），
    绝不碰 outbox 里其他文件（快讯/早报/卡片等 proactive 只由显式 send 发；
    2026-10-16 隔离修复：此前 finish 调全量 send，普通回复会顺带发走 proactive）。
    失败时 processing 留在原地，由兜底 hook 接手，绝不静默丢单。

    expected_claim_ts（可选）：worker 从唤醒 payload 里拿到的 claim_ts。
    看门狗超时回收后 hook 会重新认领并改写 claim_ts；此时旧 worker 已是过时认领，
    必须拒发（FINISH_STALE，安静退出），否则会和新 worker 重复回复。
    （2026-10-04：watchdog 75s→30s 后加此校验，防止慢启动 worker 误判导致重发）

    do_recheck（2026-11-20，watch-outbox）：True 时发送前复查 inbox，同发信人
    有新消息就合并进本单并返回 2，让 worker 重新生成回复再 finish。
    False 时直接发——watcher 不能复查：复查要求 worker 原地重新生成回复，
    watcher 唤不回 worker；且 watcher 0.2 秒就发，写好到发出的窗口极小，
    追问直接走下一条单独回，不会丢。cmd_finish（worker/hook 手动调）永远 True。

    2026-10-21 幂等防护（2026-11-04 改为按单号查 reply_map，见下）：
    Worker 发了回复但死在 finish 之前时，消息留在 processing 里被重认领，
    会导致重复回复。此时直接归档、不重发。旧逻辑按文本查 msg_cache，
    同一句话发给不同消息会误拦（11-04 实测），已废弃。
    """
    import datetime
    t0 = time.time()
    proc_path = os.path.join(PROCESSING, msg_id + ".json")
    out_path = os.path.join(OUTBOX, msg_id + ".json")
    try:
        with open(proc_path) as f:
            proc = json.load(f)
    except (OSError, ValueError):
        if expected_claim_ts is not None:
            # 归属校验模式：processing 已不在（被看门狗回收/被新 worker 接手/已归档），
            # 本轮是过时或滞后唤醒，安静退出，绝不重发。
            print("FINISH_STALE", msg_id, "no processing file, standing down",
                  flush=True)
            return 0
        # 幂等收尾：processing 已不在。残留的 outbox 只有在已发送（sent/ 有记录）
        # 时才是真孤儿、可以删；否则这单从未发出（例如早报 outbox 由 daily cron
        # 直接写、从不经过 processing），删掉会静默丢单，必须保留待重试。
        # （2026-10-04 实测：旧逻辑删掉了未发出的早报 outbox）
        try:
            if os.path.exists(out_path):
                if os.path.exists(os.path.join(SENT, msg_id + ".json")):
                    os.remove(out_path)
                    print("FINISH_FAIL", msg_id,
                          "no processing file, orphan outbox removed (already sent)",
                          flush=True)
                else:
                    print("FINISH_FAIL", msg_id,
                          "no processing file, outbox kept for retry (never sent)",
                          flush=True)
                return 1
        except OSError:
            pass
        print("FINISH_FAIL", msg_id, "no processing file", flush=True)
        return 1
    claim_ts = proc.get("claim_ts") or t0
    if expected_claim_ts is not None:
        actual_claim = proc.get("claim_ts")
        if actual_claim is not None and str(actual_claim) != str(expected_claim_ts):
            # 过时认领：看门狗已回收并改写 claim_ts，新 worker 拥有这单。
            # 拒发并安静退出；残留 outbox 留给新 worker 覆盖或兜底处理，绝不擅自删除。
            print("FINISH_STALE", msg_id,
                  "claim_ts mismatch (expected %s, have %s), owned by newer claim; standing down"
                  % (expected_claim_ts, actual_claim), flush=True)
            return 0
    try:
        with open(out_path) as f:
            job = json.load(f)
    except (OSError, ValueError):
        print("FINISH_FAIL", msg_id, "no usable outbox file", flush=True)
        return 1
    # 回填兜底：worker 漏写 to / context_token 时从 processing 补，
    # 否则 send 会因缺字段失败；processing 是认领时写入的可信来源。
    need_rewrite = False
    if not job.get("to") and proc.get("from"):
        job["to"] = proc["from"]
        need_rewrite = True
    if "context_token" not in job and proc.get("context_token"):
        job["context_token"] = proc["context_token"]
        need_rewrite = True
    if need_rewrite:
        _atomic_write_json(out_path, job)
    out_mtime = os.path.getmtime(out_path)

    # 2026-11-19 发送前复查：同发信人的新消息还在 inbox/ 里没人管时，
    # 不发这份可能已过时的回复；并进本单、返回 2，让 worker 按合并后
    # 的上下文重新生成回复再 finish（FINISH_RECHECK）。watch-outbox
    # 不做复查（do_recheck=False，见 _finish_one docstring）。
    if do_recheck:
        merged = _recheck_new_messages(proc, msg_id)
        if merged:
            print("FINISH_RECHECK", msg_id, "merged", merged, flush=True)
            return 2

    # 2026-11-04 去重粒度修复（Peter 要求）：旧逻辑按回复文本查 msg_cache，
    # 同一句话发给不同消息会误判 FINISH_DUP（11-04 "1" 的"链路是通的。"
    # 与 11-03 同文回复撞车，确认被拦）；同单文字已出、图片待发时也会
    # 误拦、丢掉待发图片。新逻辑按本单 inbound msg_id 查 reply_map：
    # 只有本单整单成功送达过才抑制；不同消息、部分送达都不抑制
    # （部分送达靠 parts 账本续发剩余分片）。Worker 曾发了回复但死在
    # finish 之前、消息留在 processing 里被重认领时，账本里已有本单，
    # 直接归档、不重发——10-21 的防重复保护原样保留，只是认单号不认文本。
    try:
        _delivered = load_reply_map()
        if str(msg_id) in _delivered:
            print("FINISH_DUP", msg_id, "reply already delivered, archiving without resend",
                  flush=True)
            # 归档 outbox 文件到 sent/（标记为已处理）
            try:
                os.replace(out_path, os.path.join(SENT, msg_id + ".json"))
            except OSError:
                pass
            try:
                os.replace(proc_path, os.path.join(DONE, msg_id + ".json"))
            except OSError:
                pass
            try:
                os.remove(os.path.join(PROCESSING, msg_id + ".alive"))
            except OSError:
                pass
            return 0
    except Exception as e:
        # 检查失败不阻断正常流程，继续发送（宁可重发，不丢单）
        print("FINISH_WARN", msg_id, "dup check failed:", e, flush=True)

    cmd_send(only=msg_id + ".json")  # 只发本单，绝不碰 outbox 里其他文件

    # 成功判定必须两条都满足：outbox 文件已消费（不在了）且 sent/ 有归档。
    # 只看 sent/ 会误判：历史上已发过同 id（sent/ 有旧记录），但本轮 outbox
    # 任务发送失败、文件还在，再报 FINISH_OK 就把未发出的回复当发出了。
    if os.path.exists(out_path) or not os.path.exists(os.path.join(SENT, msg_id + ".json")):
        print("FINISH_FAIL", msg_id, "send incomplete, left for fallback",
              flush=True)
        return 1
    try:
        os.replace(proc_path, os.path.join(DONE, msg_id + ".json"))
    except OSError as e:
        print("FINISH_WARN", msg_id, "done move failed:", e, flush=True)
    # Clean up the watchdog alive marker (worker started OK).
    try:
        os.remove(os.path.join(PROCESSING, msg_id + ".alive"))
    except OSError:
        pass
    t_sent = time.time()
    note = ("image" if job.get("image") else
            "video" if job.get("video") else
            "file" if job.get("file") else "纯文本")
    entry = {
        "ts": datetime.datetime.fromtimestamp(claim_ts).isoformat(),
        "msg_id": msg_id,
        "wake_to_claim_s": 0,
        "claim_to_reply_s": round(out_mtime - claim_ts, 1),
        "reply_to_sent_s": round(t_sent - out_mtime, 1),
        "total_s": round(t_sent - claim_ts, 1),
        "note": note,
    }
    try:
        os.makedirs(os.path.dirname(LATENCY_LOG), exist_ok=True)
        with open(LATENCY_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print("FINISH_WARN", msg_id, "latency log failed:", e, flush=True)
    print("FINISH_OK", msg_id, "total=%.1fs" % entry["total_s"], flush=True)
    return 0


def cmd_finish(msg_id, expected_claim_ts=None):
    """worker 单条命令收尾：`wxbot.py finish <id> [claim_ts]`，复查开着
    （do_recheck=True）。watch-outbox 出现后 worker 平时不再调它
    （watcher 代发），它只留给手动收尾与 hook §1c auto-finish 兜底。"""
    return _finish_one(msg_id, expected_claim_ts, do_recheck=True)


def _recheck_new_messages(proc, msg_id):
    """发送前复查（2026-11-19）：看 inbox 里有没有同发信人的新消息。

    Worker 思考期间用户又发了一条，hook 可能已认领（processing/，
    归另一个 worker 管，这里不管）或还在 inbox/（没人管）。还在
    inbox/ 的并进本单：文本追加到 processing 的 merged_text，文件
    归档到 done/（不是丢弃，是已合并进本单）。返回合并的 id 列表；
    调用方（cmd_finish）此时不发、返回 2（FINISH_RECHECK），让
    worker 按合并后的上下文重新生成回复再 finish。
    """
    sender = proc.get("from")
    if not sender:
        return []
    try:
        files = sorted(f for f in os.listdir(INBOX) if f.endswith(".json"))
    except OSError:
        return []
    merged = []
    for fn in files:
        mid = fn[:-5]
        if mid == str(msg_id):
            continue
        path = os.path.join(INBOX, fn)
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("from") != sender:
            continue
        merged.append(mid)
        try:
            proc.setdefault("merged_text", []).append(d.get("text", ""))
            proc.setdefault("merged_ids", []).append(mid)
        except Exception:
            pass
        try:
            os.replace(path, os.path.join(DONE, fn))
        except OSError:
            pass
    if merged:
        try:
            _atomic_write_json(
                os.path.join(PROCESSING, str(msg_id) + ".json"), proc)
        except Exception as e:
            print("FINISH_RECHECK_SAVE_FAIL", msg_id, e, flush=True)
    return merged


# --- 运维命令（2026-11-19，借鉴上游 health/watchdog/stats/preflight/smoke/prune）---

def _load_json_file(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _dir_json_count(d):
    try:
        return len([f for f in os.listdir(d) if f.endswith(".json")])
    except OSError:
        return 0


def cmd_health(offline=False):
    """健康检查：exit 0 正常 / 1 警告 / 2 严重。只读，不调发送接口。

    警告：队列积压、处理超 10 分钟的单（按 mtime 判）、buf.txt 超 180 秒
    未更新。严重：无登录凭证、发送账本损坏、token 探活报 -14。
    token 探活异常（连不上）只算"探活无结论"，不算警告——接收器自己的
    长轮询才是登录态的真实信号（buf.txt 新鲜度已覆盖）。
    """
    warnings, criticals = [], []
    try:
        creds = load_creds()
    except Exception as e:
        print("HEALTH_CRIT", "no credentials:", e, flush=True)
        return 2
    try:
        age_h = (time.time() - os.path.getmtime(CREDS)) / 3600
    except OSError:
        age_h = -1
    if not offline:
        try:
            api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                            token=creds.get("token", ""))
            uid = creds.get("user_id", "")
            r = api.get_config(uid) if uid else {}
            if isinstance(r, dict) and r.get("ret") not in (0, None):
                if r.get("ret") == -14:
                    criticals.append("token expired (-14)")
                else:
                    warnings.append("token probe ret=%s" % r.get("ret"))
        except Exception as e:
            print("HEALTH_INFO", "token probe inconclusive:", e, flush=True)
    n_inbox = _dir_json_count(INBOX)
    n_proc = _dir_json_count(PROCESSING)
    n_out = _dir_json_count(OUTBOX)
    if n_inbox > 0:
        warnings.append("inbox backlog %d" % n_inbox)
    if n_out > 0:
        warnings.append("outbox backlog %d" % n_out)
    now = time.time()
    try:
        for fn in os.listdir(PROCESSING):
            if not fn.endswith(".json"):
                continue
            if now - os.path.getmtime(os.path.join(PROCESSING, fn)) > 600:
                warnings.append("stale processing %s" % fn)
    except OSError:
        pass
    try:
        _load_send_ledger()
    except SendLedgerCorrupt:
        criticals.append("send ledger corrupt")
    try:
        buf_age = time.time() - os.path.getmtime(BUF)
        if buf_age > 180:
            warnings.append("buf.txt stale %ds" % int(buf_age))
    except OSError:
        warnings.append("buf.txt missing")
    owner = load_owner()
    print("HEALTH", "inbox=%d processing=%d outbox=%d owners=%d unknown=%d "
          "quota=%d login_age_h=%.1f"
          % (n_inbox, n_proc, n_out, len(owner["owner_ids"]),
             len(load_unknown_senders()), _load_quota(), age_h), flush=True)
    if criticals:
        for c in criticals:
            print("HEALTH_CRIT", c, flush=True)
        return 2
    if warnings:
        for w in warnings:
            print("HEALTH_WARN", w, flush=True)
        return 1
    print("HEALTH_OK", flush=True)
    return 0


def cmd_watchdog(max_age_mins=30):
    """看门狗：buf.txt 超 max_age_mins 未更新即判接收器失活。

    状态机（watchdog_state.json）：首次失活报 WATCHDOG_ALERT，重复失活
    报 WATCHDOG_STALE（静默，不重复吵），恢复报一次 WATCHDOG_RECOVERED。
    桥有意停用时 touch watchdog.off 即可静音。
    """
    if os.path.exists(WATCHDOG_OFF_FILE):
        print("WATCHDOG_OFF", flush=True)
        return 0
    try:
        age = time.time() - os.path.getmtime(BUF)
    except OSError:
        age = float("inf")
    state = _load_json_file(WATCHDOG_STATE_FILE, {})
    if age <= max_age_mins * 60:
        if state.get("status") == "stale":
            _atomic_write_json(WATCHDOG_STATE_FILE, {"status": "ok"})
            print("WATCHDOG_RECOVERED", flush=True)
        else:
            print("WATCHDOG_OK", flush=True)
        return 0
    if state.get("status") == "stale":
        print("WATCHDOG_STALE", "buf age %ds" % int(age), flush=True)
        return 1
    _atomic_write_json(WATCHDOG_STATE_FILE,
                       {"status": "stale", "since": time.time()})
    print("WATCHDOG_ALERT", "buf age %ds" % int(age), flush=True)
    return 1


def alert_check(kind, event):
    """告警闭环状态机（借鉴上游 ilink-watch.sh alert-state.json）。

    kind: token（过期立即报，之后每小时复报）/ poll / worker
    （连续失败满 10 分钟才报，之后每小时复报）。
    event: failing / recovered。返回 ALERT_SEND（该通知了）/
    ALERT_SUPPRESS（去重窗口内，不通知）/ ALERT_RECOVERED（恢复，
    报一次平安）/ ALERT_NONE。
    """
    rules = ALERT_RULES.get(kind, (0, 3600))
    debounce, repeat = rules
    state = _load_json_file(ALERT_STATE_FILE, {})
    ent = state.get(kind) or {}
    now = time.time()
    if event == "recovered":
        if ent.get("alerted"):
            state.pop(kind, None)
            try:
                _atomic_write_json(ALERT_STATE_FILE, state)
            except Exception:
                pass
            return "ALERT_RECOVERED"
        return "ALERT_NONE"
    if event != "failing":
        return "ALERT_NONE"
    first_seen = ent.get("first_seen") or now
    ent["first_seen"] = first_seen
    if now - first_seen < debounce:
        state[kind] = ent
        try:
            _atomic_write_json(ALERT_STATE_FILE, state)
        except Exception:
            pass
        return "ALERT_SUPPRESS"
    last_alert = ent.get("last_alert", 0)
    if ent.get("alerted") and now - last_alert < repeat:
        state[kind] = ent
        try:
            _atomic_write_json(ALERT_STATE_FILE, state)
        except Exception:
            pass
        return "ALERT_SUPPRESS"
    ent["alerted"] = True
    ent["last_alert"] = now
    state[kind] = ent
    try:
        _atomic_write_json(ALERT_STATE_FILE, state)
    except Exception:
        pass
    return "ALERT_SEND"


def cmd_alert(kind, event):
    print(alert_check(kind, event), flush=True)
    return 0


def cmd_stats():
    """离线快照：不调服务端，只读本地文件。"""
    try:
        creds_age = (time.time() - os.path.getmtime(CREDS)) / 3600
    except OSError:
        creds_age = -1
    try:
        media_files = [f for f in os.listdir(MEDIA)
                       if os.path.isfile(os.path.join(MEDIA, f))]
        media_mb = sum(os.path.getsize(os.path.join(MEDIA, f))
                       for f in media_files) / (1024 * 1024)
    except OSError:
        media_files, media_mb = [], 0.0
    try:
        ledger_n = len(_load_send_ledger())
    except SendLedgerCorrupt:
        ledger_n = -1
    qc = load_quote_cache()
    stats = {
        "inbox": _dir_json_count(INBOX),
        "processing": _dir_json_count(PROCESSING),
        "outbox": _dir_json_count(OUTBOX),
        "sent": _dir_json_count(SENT),
        "done": _dir_json_count(DONE),
        "quota_sent_since_inbound": _load_quota(),
        "owners": len(load_owner()["owner_ids"]),
        "unknown_senders": len(load_unknown_senders()),
        "msg_cache": len(load_msg_cache()),
        "reply_map": len(load_reply_map()),
        "quote_cache": len(qc.get("by_id", {})),
        "send_ledger": ledger_n,
        "media_files": len(media_files),
        "media_mb": round(media_mb, 1),
        "login_age_h": round(creds_age, 1),
    }
    print("STATS", json.dumps(stats, ensure_ascii=False), flush=True)
    return 0


def cmd_preflight():
    """登录前环境预检：不登录、不联网、不碰状态文件。"""
    problems = []
    if sys.version_info < (3, 10):
        problems.append("python >=3.10 required")
    try:
        import Crypto  # noqa: F401  (CowAgent 的 AES 加解密依赖)
    except Exception:
        problems.append("pycryptodome (Crypto) not importable")
    for d in (INBOX, OUTBOX, SENT, DONE, MEDIA, PROCESSING):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".preflight_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            problems.append("dir not writable %s: %s" % (d, e))
    if problems:
        for p in problems:
            print("PREFLIGHT_FAIL", p, flush=True)
        return 1
    print("PREFLIGHT_OK", flush=True)
    return 0


def cmd_smoke_test(no_send=False):
    """端到端冒烟自检。故意不用 getupdates 做登录探活（35 秒长轮询，
    收到消息会被重投）。无凭证时 SKIP（不算失败）；--no-send 只做
    离线检查，不发任何真实消息。"""
    try:
        creds = load_creds()
    except Exception:
        print("SMOKE_SKIP", "no credentials", flush=True)
        return 0
    for d in (INBOX, OUTBOX, SENT, DONE, MEDIA, PROCESSING):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".smoke_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            print("SMOKE_FAILED", "dirs not writable:", e, flush=True)
            return 1
    try:
        _load_send_ledger()
    except SendLedgerCorrupt:
        print("SMOKE_FAILED", "send ledger corrupt", flush=True)
        return 1
    if no_send:
        print("SMOKE_OK", "offline checks passed, live send skipped",
              flush=True)
        return 0
    owner = load_owner()
    if not owner["owner_ids"]:
        print("SMOKE_FAILED", "no owner configured", flush=True)
        return 1
    # 真实发送路径（只在显式要求时跑）：一条主动文本 + 一张 1x1 图。
    try:
        api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                        token=creds.get("token", ""))
        to = owner["owner_ids"][0]
        cid, _ = resolve_send_intent("smoke-test:text")
        r = send_text_retry(api, to, "", "冒烟自检（请忽略）",
                            "smoke-test", client_id=cid)
        if not r or r.get("ret", 0) != 0:
            print("SMOKE_FAILED", "text send:", r, flush=True)
            return 1
        mark_send_intent_sent("smoke-test:text")
        import struct, zlib

        def _png_chunk(typ, data):
            return (struct.pack(">I", len(data)) + typ + data
                    + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\xff\x00\x00")  # 1 个红像素
        png = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
               + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b""))
        tmp_png = os.path.join(MEDIA, "smoke-1x1.png")
        with open(tmp_png, "wb") as f:
            f.write(png)
        up = upload_media_to_cdn(api, tmp_png, to, media_type=1)
        cid2, _ = resolve_send_intent("smoke-test:image")
        r2 = _call_with_client_id(api.send_image_item, to, "",
                                  up["encrypt_query_param"],
                                  up["aes_key_b64"], up["ciphertext_size"],
                                  client_id=cid2)
        if not r2 or r2.get("ret", 0) != 0:
            print("SMOKE_FAILED", "image send:", r2, flush=True)
            return 1
        mark_send_intent_sent("smoke-test:image")
    except Exception as e:
        print("SMOKE_FAILED", e, flush=True)
        return 1
    print("SMOKE_OK", flush=True)
    return 0


def cmd_prune_media(max_age_days=30, max_total_mb=500, dry_run=False):
    """媒体保留清理：超 30 天的删；总量超 500MB 时从最老的删起。
    引用缓存里登记过的文件（media_path）两轮都豁免。单个文件失败
    不致命，跳过继续。"""
    now = time.time()
    referenced = set()
    try:
        for ent in load_quote_cache().get("by_id", {}).values():
            if isinstance(ent, dict) and ent.get("media_path"):
                referenced.add(os.path.abspath(ent["media_path"]))
    except Exception:
        pass
    try:
        files = [os.path.join(MEDIA, f) for f in os.listdir(MEDIA)
                 if os.path.isfile(os.path.join(MEDIA, f))
                 and not f.endswith(".enc.part")]
    except OSError:
        files = []
    to_delete = []
    for p in files:
        if os.path.abspath(p) in referenced:
            continue
        try:
            if now - os.path.getmtime(p) > max_age_days * 86400:
                to_delete.append(p)
        except OSError:
            continue
    total = sum(os.path.getsize(p) for p in files if os.path.exists(p))
    cap_bytes = max_total_mb * 1024 * 1024
    if total > cap_bytes:
        cands = sorted(
            (p for p in files
             if os.path.abspath(p) not in referenced and p not in to_delete),
            key=lambda p: os.path.getmtime(p))
        for p in cands:
            if total <= cap_bytes:
                break
            to_delete.append(p)
            try:
                total -= os.path.getsize(p)
            except OSError:
                pass
    freed = sum(os.path.getsize(p) for p in to_delete if os.path.exists(p))
    if dry_run:
        print("PRUNE_DRY_RUN", "would delete %d files, %.1f KB"
              % (len(to_delete), freed / 1024), flush=True)
        for p in to_delete:
            print("PRUNE_WOULD_DELETE", p, flush=True)
        return 0
    deleted = 0
    for p in to_delete:
        try:
            os.remove(p)
            deleted += 1
        except OSError as e:
            print("PRUNE_FAIL", p, e, flush=True)
    print("PRUNE_DONE", "deleted %d files, %.1f KB"
          % (deleted, freed / 1024), flush=True)
    return 0


def _write_warm_marker(since, until):
    try:
        _atomic_write_json(WARM_FILE, {
            "since": since, "activity": time.time(),
            "heartbeat": time.time(), "until": until,
            # 2026-11-20：记 PID，供排查常驻死亡时确认进程是否还在
            "pid": os.getpid()})
    except Exception:
        pass


def _clear_warm_marker():
    try:
        os.remove(WARM_FILE)
    except OSError:
        pass


# --- worker-side claim（2026-11-19，warm-wait 接线）---
# hook §2 的认领逻辑长期只活在 wechat-inbox.sh 的 heredoc 里；warm-wait
# 回来后 worker 需要同一套认领（原子 mv + 归并 + ack/watchdog + payload），
# 但不能再跑 hook 脚本（会重复唤醒一个新 worker）。所以把认领收进 wxbot.py：
# `wxbot.py claim` 只认领、不唤醒；两个入口共享同一套规则（归并阈值、
# skill 意图、simple 路由、上文截取），改规则时两边一起改。
ACK_NOISE = "收到你的消息啦，我需要大约半分钟思考一下~"
EVOLVE_FILE = os.path.join(BASE, "EVOLVE.md")
ACK_TIMER_SCRIPT = os.path.join(BASE, "ack_timer.sh")
WATCHDOG_SCRIPT = os.path.join(BASE, "watchdog.sh")

_SIMPLE_PATTERNS = (
    "你好", "您好", "hi", "hello", "早上好", "晚上好", "在吗", "在么",
    "好的", "好", "谢谢", "ok", "OK", "收到", "嗯", "哦",
    "1", "是", "对", "行", "可以", "没问题", "好吧", "嗯嗯",
)


def _claim_skill_hint(text_content):
    """hook §2 的确定性 skill 意图预取（best-effort），原样照搬。"""
    m_weather = re.search(r"([\u4e00-\u9fa5]{2,10})[的]?天气", text_content)
    if m_weather:
        city = re.sub(r"(今天|明天|后天|实时|现在|请问|查一下|查|看)",
                      "", m_weather.group(1)).strip()
        return {"type": "weather", "city": city,
                "cmd": f'curl -s "https://60s.vaneus.ccwu.cc/v2/weather?query={city}"'}
    if any(k in text_content for k in ("微博热搜", "知乎热搜", "B站热搜", "抖音热搜")):
        platform = ("weibo" if "微博" in text_content else
                    "zhihu" if "知乎" in text_content else
                    "bili" if "B站" in text_content else "douyin")
        return {"type": "hot-topics", "platform": platform,
                "cmd": f'curl -s "https://60s.vaneus.ccwu.cc/v2/{platform}"'}
    if any(k in text_content for k in ("今日新闻", "60秒", "早报", "新闻简报", "每日新闻")):
        return {"type": "daily-news",
                "cmd": 'curl -s "https://60s.vaneus.ccwu.cc/v2/60s"'}
    if any(k in text_content for k in ("我的基金", "大盘", "行情", "大盘分析")):
        fund_arg = "--my-funds" if "我的基金" in text_content else "--market-report"
        return {"type": "fund",
                "cmd": f"python skills/fund-market-analysis/scripts/analyze_fund.py {fund_arg}"}
    m_url = re.search(r"https?://[^\s]+", text_content)
    if m_url:
        url = m_url.group(0)
        return {"type": "url", "url": url,
                "cmd": f"skills/grok-search/scripts/fetch.js '{url}' --provider direct"}
    return None


def _claim_histories(sender):
    """hook §2 的上文预取：最近 15 行，过滤 ack 噪音；最近 4 条留 1000 字，更早的 400 字。"""
    lines = []
    if not sender:
        return lines
    hist_file = os.path.join(HISTORY_DIR, safe_user_id(sender) + ".jsonl")
    try:
        with open(hist_file) as f:
            raw = f.readlines()[-15:]
    except OSError:
        return lines
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
    return lines


def _touch_alive(msg_id):
    """建/刷新 processing/<msg_id>.alive（watchdog 判活的唯一信号）。
    认领时由 _do_claim / hook §2 立刻建一个；worker 起 keepalive 后每
    10 秒续 touch；worker 死则 .alive 自然变 stale，watchdog 按新鲜度
    回收（2026-11-20：此前只有 worker 在提示第 0/5 步顺手 touch，长
    思考中途无刷新，watchdog 20 秒一到就误判死、反复重认领）。"""
    p = os.path.join(PROCESSING, msg_id + ".alive")
    try:
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError:
        pass


def _do_claim():
    """认领 inbox 里的新消息（warm-wait 专用入口），只认领、不唤醒。
    返回 (rc, payload_or_None)，不打印；cmd_claim() 是打印版外壳，
    常驻 warm-wait 直接调这里以便在 payload 里加 rotate 标记。

    与 hook §2（wechat-inbox.sh）同一套规则：按发送方分组、同用户连发
    归并（带图超 1 条或跨度超 60 秒不归并）、打 claim_ts、起 ack_timer
    与 watchdog、输出与 hook wake payload 同形的 JSON（msg_ids /
    messages / histories / evolve_tail）。2026-11-19 起 warm-wait 检测
    到新消息直接调它（自认领，省掉 worker 另起 claim 进程的约 1 秒交接）。
    原子 mv 是去重本身：hook 恰好同时认领了同一批时，这里会拿到空，
    返回 (1, None)，worker 安静退出（hook 唤醒的 worker 会负责那批消息），
    绝不重复回。读坏的 inbox 文件留在原地不动（hook 下一轮会再处理），
    不往 processing 里留孤儿。
    """
    try:
        names = sorted(
            (f for f in os.listdir(INBOX) if f.endswith(".json")),
            key=lambda f: (os.path.getmtime(os.path.join(INBOX, f)), f))
    except OSError:
        names = []
    if not names:
        return 1, None
    now = int(time.time())
    sender_groups = {}
    for name in names:
        mid = name[:-5]
        path = os.path.join(INBOX, name)
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        sender = d.get("from") or d.get("to") or ""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        sender_groups.setdefault(sender, []).append((mid, d, mtime))

    final_ids, msgs, histories = [], {}, {}
    for sender, items in sender_groups.items():
        items.sort(key=lambda x: x[2])
        if len(items) > 1:
            n_img = sum(1 for _, d, _ in items if d.get("image"))
            span = items[-1][2] - items[0][2] if items[0][2] else 0
            mergeable = n_img <= 1 and span <= 60
        else:
            mergeable = False
        if mergeable:
            lead_id, lead_d, _ = items[-1]
            all_texts = [d.get("text", "").strip() for _, d, _ in items
                         if d.get("text", "").strip()]
            lead_d["text"] = "\n".join(all_texts)
            for _, d, _ in items:
                if d.get("image"):
                    lead_d["image"] = d.get("image")
            for prev_id, _, _ in items[:-1]:
                try:
                    os.makedirs(DONE, exist_ok=True)
                    os.replace(os.path.join(INBOX, prev_id + ".json"),
                               os.path.join(DONE, prev_id + ".json"))
                except OSError:
                    pass
                try:
                    os.remove(os.path.join(PROCESSING, prev_id + ".alive"))
                except OSError:
                    pass
            group = [(lead_id, lead_d)]
        else:
            group = [(i, d) for i, d, _ in items]

        for lead_id, d in group:
            d["claim_ts"] = now
            proc_path = os.path.join(PROCESSING, lead_id + ".json")
            try:
                os.replace(os.path.join(INBOX, lead_id + ".json"), proc_path)
            except OSError:
                continue  # hook 同时认领走了：跳过，本轮不归我
            try:
                _atomic_write_json(proc_path, d)
            except OSError:
                pass
            # 2026-11-20：认领时立刻建 .alive（watchdog 判活的基准）。
            _touch_alive(lead_id)
            ctx = d.get("context_token") or ""
            noack = bool(d.get("watchdog_noack"))
            if sender and not noack:
                try:
                    subprocess.Popen(
                        ["bash", ACK_TIMER_SCRIPT, lead_id, sender, ctx, "30"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                except Exception:
                    pass
            try:
                subprocess.Popen(
                    ["bash", WATCHDOG_SCRIPT, BASE, lead_id],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
            text_content = (d.get("text", "") or "").strip()
            skill_hint = _claim_skill_hint(text_content)
            msg_info = {"from": sender, "text": d.get("text", ""),
                        "image": d.get("image", ""),
                        "context_token": d.get("context_token"),
                        "claim_ts": now}
            if skill_hint:
                msg_info["skill_hint"] = skill_hint
            if not d.get("image") and not skill_hint:
                _txt = text_content
                if len(_txt) < 15 and any(
                        p == _txt or _txt.startswith(p + "，") or _txt.startswith(p + ",")
                        for p in _SIMPLE_PATTERNS):
                    msg_info["simple"] = True
            msgs[lead_id] = msg_info
            final_ids.append(lead_id)
            if sender and sender not in histories:
                histories[sender] = _claim_histories(sender)

    if not final_ids:
        return 1, None
    evolve_tail = []
    try:
        with open(EVOLVE_FILE) as f:
            evolve_tail = [ln.rstrip("\n") for ln in f.readlines()[-20:] if ln.strip()]
    except OSError:
        pass
    out = {"msg_ids": final_ids, "messages": msgs, "histories": histories}
    if evolve_tail:
        out["evolve_tail"] = evolve_tail
    return 0, out


def cmd_claim():
    """`wxbot.py claim` 命令行入口：只认领、不唤醒，打印 _do_claim() 的
    结果（payload JSON，或 CLAIM_EMPTY）。输出的 payload worker 按
    hook-worker.md 0-5 步回。常驻 warm-wait 不走这里，直接调 _do_claim()。"""
    rc, payload = _do_claim()
    if payload is None:
        print("CLAIM_EMPTY", flush=True)
        return 1
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def _parse_keepalive_args(argv):
    """解析 keepalive 的 --interval-secs/--max-secs（`=` 与空格两种写法
    都接受）；非法值回落默认值，绝不抛错。返回 (interval_secs, max_secs)。"""
    interval_secs, max_secs = 10, 1200
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--interval-secs" or a.startswith("--interval-secs="):
            if a.startswith("--interval-secs="):
                val = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                val = argv[i + 1]
                i += 1
            else:
                val = ""
            try:
                v = int(val)
                if v > 0:
                    interval_secs = v
            except ValueError:
                pass
        elif a == "--max-secs" or a.startswith("--max-secs="):
            if a.startswith("--max-secs="):
                val = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                val = argv[i + 1]
                i += 1
            else:
                val = ""
            try:
                v = int(val)
                if v > 0:
                    max_secs = v
            except ValueError:
                pass
        i += 1
    return interval_secs, max_secs


def cmd_keepalive(msg_id, interval_secs=10, max_secs=1200):
    """worker 存活心跳：认领后 worker 起一个 keepalive，每 interval 秒
    touch processing/<msg_id>.alive；processing/<msg_id>.json 消失
    （回完，移到 done/）或超 max_secs 即退出。watchdog 按 .alive 新鲜度
    判活（60 秒没刷新 = 死），所以长思考中途必须有人续 touch——此前
    .alive 只靠 worker 在提示第 0 步和第 5 步顺手 touch，思考中途无刷新，
    watchdog 20 秒宽限一到就误判死、反复重认领（2026-11-20 09:34 连发
    5 次唤醒、第一份丢回复）。
    必须由 worker 以前台 exec 起（10 秒后自动后台化，但仍留在 worker
    进程组——worker 会话结束时随之被杀，.alive 自然变 stale，watchdog
    才能判死）。绝不要用 background=true 起它：那会给它独立进程组、
    脱离 worker 会话，worker 死后它还继续 touch、把死 worker 伪装成活
    的（只剩 max_secs 兜底，太慢）。它结束的通知不用理会。"""
    proc = os.path.join(PROCESSING, msg_id + ".json")
    start = time.time()
    while True:
        if not os.path.exists(proc):
            return 0
        if time.time() - start >= max_secs:
            return 0
        _touch_alive(msg_id)
        time.sleep(interval_secs)
    return 0


def _parse_warm_wait_args(argv):
    """解析 warm-wait 的 --idle-secs/--cap-secs/--max-claims/--resident，
    `=` 与空格两种写法都接受（`--cap-secs=900` 和 `--cap-secs 900`）。
    只认 `=` 写法时，worker 按自然写法传空格形式会被静默忽略、跑满默认
    1800 秒（2026-11-19 实测），所以这里两种都收；非法值一律回落默认值，
    绝不抛错。返回 (idle_secs, cap_secs, resident, max_claims)。
    常驻模式（--resident）默认 cap 24 小时、max-claims 100；显式传了
    --cap-secs 时以显式值为准（worker 每轮传剩余秒数）。"""
    idle_secs, cap_secs = WARM_IDLE_SECS, WARM_CAP_SECS
    resident, max_claims = False, WARM_RESIDENT_MAX_CLAIMS
    cap_given = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--resident" or a.startswith("--resident="):
            v = a.split("=", 1)[1].lower() if "=" in a else "true"
            resident = v in ("1", "true", "yes", "on")
        elif a == "--max-claims" or a.startswith("--max-claims="):
            if a.startswith("--max-claims="):
                val = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                val = argv[i + 1]
                i += 1
            else:
                val = None
            if val is not None:
                try:
                    v = int(val)
                except ValueError:
                    v = None
                if v is not None and v > 0:
                    max_claims = v
        for key, attr in (("--idle-secs", "idle"), ("--cap-secs", "cap")):
            val = None
            if a.startswith(key + "="):
                val = a.split("=", 1)[1]
            elif a == key and i + 1 < len(argv):
                val = argv[i + 1]
                i += 1
            if val is not None:
                try:
                    v = int(val)
                except ValueError:
                    v = None
                if v is not None and v > 0:
                    if attr == "idle":
                        idle_secs = v
                    else:
                        cap_secs = v
                        cap_given = True
        i += 1
    if resident and not cap_given:
        cap_secs = WARM_RESIDENT_CAP_SECS
    return idle_secs, cap_secs, resident, max_claims


def _recent_done(window_secs=None):
    """刚有 worker 回完吗（done 里有 window 秒内落盘的文件）？自适应让步用：
    刚回完说明它的 warm-wait 正在启动，多等一会让它先抢。"""
    if window_secs is None:
        window_secs = WARM_RECENT_DONE_SECS
    try:
        now = time.time()
        for f in os.listdir(DONE):
            if not f.endswith(".json"):
                continue
            try:
                if now - os.path.getmtime(os.path.join(DONE, f)) <= window_secs:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def _resident_load():
    """读常驻会话账本 {"session_start", "claims", "until"?}；缺失/损坏返回 None。
    "until" 是会话终点（epoch 秒），2026-11-20 起写入；老账本没有也正常。"""
    try:
        with open(WARM_RESIDENT_FILE) as f:
            d = json.load(f)
        if (isinstance(d, dict) and isinstance(d.get("claims"), int)
                and isinstance(d.get("session_start"), (int, float))):
            return d
    except (OSError, ValueError):
        pass
    return None


def _resident_save(session_start, claims, until=None):
    # until 不传时沿用账本里已有的：重拉/认领都不应改动会话终点。
    # 2026-11-20：会话终点只认账本 until——warm.json 会被 worker 自跑的
    # 非 resident warm-wait 改写成 start+1800，supervisor 再按它钳制常驻
    # cap，会把会话提前掐断（实测：cap 被钳到 1648 秒，会话 16:25 提前结束）。
    if until is None:
        try:
            st = _resident_load()
            if st is not None:
                until = st.get("until")
        except Exception:
            pass
    data = {"session_start": session_start, "claims": claims}
    if isinstance(until, (int, float)):
        data["until"] = until
    try:
        _atomic_write_json(WARM_RESIDENT_FILE, data)
    except Exception:
        pass


def _resident_reset():
    try:
        os.remove(WARM_RESIDENT_FILE)
    except OSError:
        pass


def _write_warm_claim_file(payload):
    """常驻 warm-wait 认领后把 payload 写一份 warm-claim-<首条 id>.json，
    hook supervisor 下一轮读它唤醒 worker（见 wechat-inbox.sh §1f）。
    只写文件；打印仍由调用方做。失败静默——认领本身已生效，丢文件只会
    让这批消息多等一轮 watchdog 重排，不丢不重。"""
    try:
        ids = (payload or {}).get("msg_ids") or []
        if not ids:
            return
        safe_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(ids[0]))
        # 前缀是模块常量，测试可 override 到 tmp（测试卫生铁律）；
        # live 时即 BASE/warm-claim-<id>.json，与 hook §1f 的 glob 一致。
        _atomic_write_json(WARM_CLAIM_PREFIX + safe_id + ".json", payload)
    except Exception:
        pass


def _parse_request_warm_args(argv):
    """解析 request-warm 的 --cap-secs（`=` 与空格两种写法），非法回落 86400。"""
    cap_secs = WARM_RESIDENT_CAP_SECS
    i = 0
    while i < len(argv):
        a = argv[i]
        val = None
        if a.startswith("--cap-secs="):
            val = a.split("=", 1)[1]
        elif a == "--cap-secs" and i + 1 < len(argv):
            val = argv[i + 1]
            i += 1
        if val is not None:
            try:
                v = int(val)
            except ValueError:
                v = None
            if v is not None and v > 0:
                cap_secs = v
        i += 1
    return cap_secs


def cmd_request_warm(cap_secs):
    """worker 回完一轮后请求常驻：写 warm-request.json，hook supervisor
    5 秒内拉起 hook 所有的 warm-wait。worker 自己绝不跑 warm-wait——
    worker 会话结束时 runtime 会清掉它名下的后台进程，background=true +
    setsid 三轮实测仍在 5 秒～4.5 分钟内死光（2026-11-20）。
    cap_secs 钳制到 1..86400。返回 0 并打印 WARM_REQUEST_OK。"""
    try:
        cap = int(cap_secs)
    except (TypeError, ValueError):
        cap = WARM_RESIDENT_CAP_SECS
    cap = max(1, min(WARM_RESIDENT_CAP_SECS, cap))
    try:
        _atomic_write_json(WARM_REQUEST_FILE,
                           {"cap_secs": cap, "ts": int(time.time())})
    except OSError:
        return 1
    print("WARM_REQUEST_OK %d" % cap, flush=True)
    return 0


def cmd_warm_wait(idle_secs=180, cap_secs=1800, resident=False, max_claims=100):
    """回完一轮后保持热等待：inbox 一有新消息立刻自己认领，hook 下一轮直接
    唤醒 worker 处理，省一次冷启动；空闲超 idle_secs 报
    IDLE_TIMEOUT，总时长超 cap_secs 报 TOTAL_CAP。心跳每 5 秒写
    warm.json，hook 在心跳新鲜时不另唤 worker（见 wechat-inbox.sh §2
    前的 warm guard）。收件箱每 1 秒扫一次（不是 5 秒，5 秒只是心跳）。
    2026-11-19 自认领：检测到新消息直接调 cmd_claim() 原子认领并打印
    payload，不再只报 NEW_MESSAGES 等 worker 另起 claim 进程——那约 1
    秒交接就是 17:54 第二条（32.3 秒）输给 hook 5 秒轮询的原因。
    2026-11-20 常驻（--resident）：没有空闲超时，等到 cap（默认 24 小时）
    或认领满 max_claims（默认 100 条）才退出；认领前先让步
    WARM_RESIDENT_DEFER_SECS（默认 2 秒）：worker 自跑的 warm-wait 1 秒轮询，
    让它先抢；2 秒后消息还在才认领，没 worker 在等时只慢 2 秒。自适应：
    刚有 worker 回完（done 文件 30 秒内）时多等 WARM_RESIDENT_DEFER_LONG_SECS
    （默认 20 秒），因为 worker 发完到跑起 warm-wait 实测慢到 17 秒。认领后 warm.json 不清除，
    心跳在思考期间继续给 hook 守卫留 10 秒宽限（思考超 10 秒后来消息仍
    可能被 hook 认领——老行为，不丢不重，只是多一次冷启动）。认领满
    max_claims 时 payload 里带 "rotate": true，worker 回完本轮就退出不
    再跑 warm-wait，下一条消息走 hook 另唤新 worker（轮换防上下文膨胀）。
    所有权（2026-11-20 第二轮）：warm-wait 归 hook supervisor 所有——worker
    回完只写 warm-request.json 就退出，绝不自己跑 warm-wait；hook 用起接收器
    同款办法（setsid nohup，见 wechat-inbox.sh §1f）拉起它。worker 会话结束时
    runtime 会清掉它名下的后台进程，background=true + setsid 三轮实测仍在
    5 秒～4.5 分钟内死光（2026-11-20 10:27–10:44）。认领后 payload 除了打印，
    还写一份 warm-claim-<id>.json 供 hook 下一轮唤醒 worker。__main__ 入口仍
    保留 setsid 兜底（前台误起/测试时多一层保护）。
    opt-in：只在 hook supervisor 拉起它时才生效，不跑不影响现有流程。"""
    start = time.time()
    session_start, claims = start, 0
    ledger_until = None
    clear_on_exit = True
    if resident:
        st = _resident_load()
        # 会话终点以账本 until 为准（2026-11-20）：warm.json 会被 worker 自跑
        # 的非 resident warm-wait 改写，不能当会话终点——既不能拿它判过期，
        # 也不能拿它钳制重拉的 cap。
        if st is not None:
            u = st.get("until")
            if isinstance(u, (int, float)) and u > start:
                ledger_until = u
        # 会话是否已结束：账本 until 在先；没有时才看 warm.json 的 until，
        # 再没有用账本 elapsed >= cap 判断（cap 此时是完整时长）。旧写法只比
        # elapsed >= cap：cap 是剩余时长时，流逝过半就误判会话已结束。
        expired = False
        if ledger_until is not None:
            expired = start >= ledger_until
        elif st is not None:
            try:
                expired = start >= json.load(open(WARM_FILE))["until"]
            except (OSError, ValueError, KeyError):
                expired = start - st["session_start"] >= cap_secs
        if st is None or expired:
            # 新会话：首次、账本损坏、或上个会话已超 until
            ledger_until = start + cap_secs
            _resident_save(start, 0, ledger_until)
        elif st["claims"] >= max_claims:
            if os.path.exists(WARM_FILE):
                # 上轮 worker 没听 rotate 劝又跑了一次：干净轮换，不认领
                print("ROTATION_LIMIT", flush=True)
                _clear_warm_marker()
                _resident_reset()
                return 0
            # 上轮正常轮换已清 marker：新会话
            ledger_until = start + cap_secs
            _resident_save(start, 0, ledger_until)
        else:
            session_start, claims = st["session_start"], st["claims"]
    # until = 启动时刻 + cap：cap 永远是“剩余时长”（worker/supervisor 传的
    # 都是剩余），不是从 session_start 起算。旧写法 session_start + cap 每
    # 轮重拉丢掉已流逝的时间，会话越缩越短——2026-11-20 实测：09:31 的会话
    # 三轮 worker 重拉 + 一轮 hook 重拉后 until 从次日 09:31 缩到 05:14。
    # 常驻时再钳到账本 until：重拉传的 cap 再大也不能把会话终点往后推。
    until = start + cap_secs
    if ledger_until is not None:
        until = min(until, ledger_until)
    _write_warm_marker(session_start, until)
    last_hb = start
    try:
        while True:
            try:
                new_files = sorted(f for f in os.listdir(INBOX)
                                   if f.endswith(".json"))
            except OSError:
                new_files = []
            if new_files:
                if resident:
                    # 混合版让步（2026-11-20）：worker 自跑的 warm-wait 也是
                    # 1 秒轮询，让它先抢——先等一会再复查，消息还在才认领。
                    # 自适应：刚有 worker 回完（done 文件 30 秒内）时，它的
                    # warm-wait 正在启动（实测慢到 17 秒），多等 20 秒；否则
                    # 只等 2 秒。复查后仍在才认领；抢赢抢输都由 _do_claim 的
                    # 原子 mv 兜底（输了报 CLAIM_EMPTY，不重不漏）。
                    _defer = (WARM_RESIDENT_DEFER_LONG_SECS
                              if _recent_done() else WARM_RESIDENT_DEFER_SECS)
                    time.sleep(_defer)
                    _write_warm_marker(session_start, until)
                    last_hb = time.time()
                    try:
                        new_files = sorted(
                            f for f in os.listdir(INBOX)
                            if f.endswith(".json"))
                    except OSError:
                        new_files = []
                    if not new_files:
                        continue
                    rc, payload = _do_claim()
                    if payload is None:
                        print("CLAIM_EMPTY", flush=True)
                        return 1
                    claims += 1
                    _resident_save(session_start, claims)
                    if claims >= max_claims:
                        payload["rotate"] = True
                    # 2026-11-20 第二轮：warm-wait 归 hook supervisor 所有，认领后
                    # 把 payload 写一份 warm-claim-<id>.json，hook 下一轮读它唤醒
                    # worker（worker 不再等 warm-wait 的 stdout）。
                    _write_warm_claim_file(payload)
                    print(json.dumps(payload, ensure_ascii=False), flush=True)
                    if claims >= max_claims:
                        _clear_warm_marker()
                        _resident_reset()
                        return 0
                    # 常驻：认领成功不清除 marker（见 docstring 的 10 秒宽限）
                    clear_on_exit = False
                    return 0
                # 直接认领：不等 worker 再起 claim 进程，省掉约 1 秒交接，
                # hook 5 秒轮询抢不赢（2026-11-19 第二条 32.3 秒就是输在这 1 秒）。
                return cmd_claim()
            now = time.time()
            if not resident and now - start >= idle_secs:
                print("IDLE_TIMEOUT", flush=True)
                return 0
            if now - start >= cap_secs:
                print("TOTAL_CAP", flush=True)
                if resident:
                    _resident_reset()
                return 0
            if now - last_hb >= 5:
                _write_warm_marker(session_start, until)
                last_hb = now
            time.sleep(1)
    except Exception:
        # 2026-11-20 第二轮：异常退出不清除 marker——hook supervisor 靠
        # warm.json 的 until/claims 判断会话未完、重新拉起；清掉反而丢了
        # 会话信息。干净退出（IDLE_TIMEOUT/TOTAL_CAP/轮换/CLAIM_EMPTY）
        # 仍走 finally 清除。
        clear_on_exit = False
        raise
    finally:
        if clear_on_exit:
            _clear_warm_marker()


# --- outbox watcher（2026-11-20，发送提速）：常驻进程盯 outbox/，普通回复
# 文件一出现就代发，worker 不用再调 finish（省一次模型动作 ~3.5s）。
# 设计要点：
# - 只发纯数字文件名的普通回复，且 processing/<id>.json 必须在（worker
#   回复进行中）；proactive（flash-/briefing-/market-/digest-/ack_）绝不
#   代发，只由显式 send 发。
# - worker 按协议原子发布（先写 .tmp 再 mv），文件一出现就是完整的，
#   不用等 settled；解析失败当没写完，下一轮再试。
# - 不做 FINISH_RECHECK（见 _finish_one 的 do_recheck 说明）。
# - 失败（配额 -2 等）留文件给兜底（hook §1c / sweeper），本进程 60 秒内
#   不重复尝试同一文件（防配额墙下空转）；文件被 worker 重写（mtime 变）
#   则立即再试。
# - 心跳写 watch-outbox.json（pid + heartbeat + 累计 sent/failed），
#   hook §1h 按它判活、死了重拉；死了期间 §1c auto-finish 兜底，只是慢一点。
# - 预热说明（Peter 问"不可以也是预热吗"）：只预热 Python 进程只能省
#   ~0.3 秒启动，省不掉 ~3.5 秒的模型动作；真正的省法是去掉 worker 单独
#   调 finish 这一步——watcher 让"写文件"本身就是发送触发，worker 不用
#   再发一次动作。常驻 watcher 进程本身就是预热（Python 已起好），
#   检测到文件 0.2 秒内就发。
def _watch_outbox_scan(attempts, retry_secs, stats):
    """一次 outbox 扫描：对每个待发普通回复跑 _finish_one（不做 recheck）。
    attempts: {msg_id: (last_attempt_ts, last_mtime)}，调用方持有；
    stats: {"sent": int, "failed": int} 累计计数，写心跳用。
    返回 (sent_this_scan, failed_this_scan)。"""
    now = time.time()
    sent = failed = 0
    try:
        names = os.listdir(OUTBOX)
    except OSError:
        names = []
    # 清掉已不在 outbox 的尝试记录（已发出或已被清理），防字典无限增长
    for mid in list(attempts):
        if (mid + ".json") not in names:
            del attempts[mid]
    for fn in sorted(names):
        if not fn.endswith(".json") or fn.endswith(".parts.json"):
            continue
        stem = fn[:-5]
        if not stem.isdigit():
            continue  # proactive 只由显式 send 发，watcher 绝不代发
        if not os.path.exists(os.path.join(PROCESSING, stem + ".json")):
            continue  # 不在处理中（孤儿/proactive），留给正常 send/兜底
        path = os.path.join(OUTBOX, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        prev = attempts.get(stem)
        if prev is not None:
            last_ts, last_mtime = prev
            if mtime <= last_mtime and now - last_ts < retry_secs:
                continue  # 刚试过且文件没变：退避中，不重复撞配额墙
        rc = _finish_one(stem, do_recheck=False)
        attempts[stem] = (now, mtime)
        if rc == 0:
            sent += 1
            stats["sent"] += 1
        else:
            failed += 1
            stats["failed"] += 1
    try:
        _atomic_write_json(WATCH_OUTBOX_FILE, {
            "pid": os.getpid(), "heartbeat": time.time(),
            "sent": stats["sent"], "failed": stats["failed"]})
    except Exception:
        pass
    return sent, failed


def _parse_watch_outbox_args(argv):
    """解析 watch-outbox 的 --poll-secs/--retry-secs/--max-secs，
    `=` 与空格两种写法都接受，非法值回落默认值。返回
    (poll_secs, retry_secs, max_secs)。max_secs 为 None = 一直跑。"""
    poll_secs, retry_secs = WATCH_OUTBOX_POLL_SECS, WATCH_OUTBOX_RETRY_SECS
    max_secs = None
    i = 0
    while i < len(argv):
        a = argv[i]
        for key, attr in (("--poll-secs", "poll"), ("--retry-secs", "retry"),
                          ("--max-secs", "max")):
            val = None
            if a.startswith(key + "="):
                val = a.split("=", 1)[1]
            elif a == key and i + 1 < len(argv):
                val = argv[i + 1]
                i += 1
            if val is not None:
                try:
                    v = float(val)
                except ValueError:
                    v = None
                if v is not None and v > 0:
                    if attr == "poll":
                        poll_secs = v
                    elif attr == "retry":
                        retry_secs = v
                    else:
                        max_secs = v
        i += 1
    return poll_secs, retry_secs, max_secs


def cmd_watch_outbox(poll_secs=WATCH_OUTBOX_POLL_SECS,
                     retry_secs=WATCH_OUTBOX_RETRY_SECS, max_secs=None):
    """常驻 outbox 盯梢：普通回复文件一出现就代发。
    worker 写完 outbox/<id>.json（原子 mv）后不用再调 finish，
    watcher 0.2 秒内检测到并发送，reply_to_sent 从 ~5 秒降到 ~1.5-2 秒
    （1.2 秒接口 + 0.2 秒检测 + 0.35 秒收尾）。
    max_secs 只供测试（跑满 N 秒后打印计数退出）；生产不传，一直跑。
    退出码恒为 0（daemon 靠 hook §1h 判活重拉，不靠退出码）。"""
    attempts = {}
    stats = {"sent": 0, "failed": 0}
    start = time.time()
    while True:
        _watch_outbox_scan(attempts, retry_secs, stats)
        if max_secs is not None and time.time() - start >= max_secs:
            print("WATCH_OUTBOX_DONE sent=%d failed=%d"
                  % (stats["sent"], stats["failed"]), flush=True)
            return 0
        time.sleep(poll_secs)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "qr":
        cmd_qr()
    elif cmd == "wait_login":
        cmd_wait_login()
    elif cmd == "receive":
        cmd_receive(drain="--drain" in sys.argv)
    elif cmd == "send":
        # send [only_fn]：不带参数 = 全量（显式 send、proactive）；
        # 带一个文件名 = 只发它（ack_timer 的本单 ack、worker 的预告都走这里，
        # 绝不触发全量带走 outbox 里其他文件）。
        only_arg = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_send(only=only_arg)
    elif cmd == "merge-backlog":
        cmd_merge_backlog()
    elif cmd == "finish":
        if len(sys.argv) < 3:
            print("usage: wxbot.py finish <msg_id> [expected_claim_ts]", flush=True)
            sys.exit(1)
        sys.exit(cmd_finish(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    elif cmd == "typing":
        # wxbot.py typing <user_id> <command: 1=显示, 2=取消>
        # 供 ack_timer.sh 每 3 秒 keepalive 调用（单次短命进程，不占配额）。
        if len(sys.argv) < 4:
            print("usage: wxbot.py typing <user_id> <command>", flush=True)
            sys.exit(1)
        try:
            command = int(sys.argv[3])
        except ValueError:
            command = 1
        sys.exit(0 if cmd_typing(sys.argv[2], command=command) else 1)
    elif cmd == "bind":
        # 开 600 秒绑定窗口，第一个发信人成为主人（owner.json 为空时才有效）。
        sys.exit(cmd_bind())
    elif cmd == "approve-owner":
        if len(sys.argv) < 3:
            print("usage: wxbot.py approve-owner <user_id>", flush=True)
            sys.exit(1)
        sys.exit(cmd_approve_owner(sys.argv[2]))
    elif cmd == "list-unknown":
        sys.exit(cmd_list_unknown())
    elif cmd == "remove-owner":
        if len(sys.argv) < 3:
            print("usage: wxbot.py remove-owner <user_id>", flush=True)
            sys.exit(1)
        sys.exit(cmd_remove_owner(sys.argv[2]))
    elif cmd == "health":
        sys.exit(cmd_health(offline="--offline" in sys.argv))
    elif cmd == "watchdog":
        max_age = 30
        for a in sys.argv[2:]:
            if a.startswith("--max-age-mins"):
                try:
                    max_age = int(a.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
        sys.exit(cmd_watchdog(max_age_mins=max_age))
    elif cmd == "alert":
        if len(sys.argv) < 4:
            print("usage: wxbot.py alert <token|poll|worker> <failing|recovered>",
                  flush=True)
            sys.exit(1)
        sys.exit(cmd_alert(sys.argv[2], sys.argv[3]))
    elif cmd == "stats":
        sys.exit(cmd_stats())
    elif cmd == "preflight":
        sys.exit(cmd_preflight())
    elif cmd == "smoke-test":
        sys.exit(cmd_smoke_test(no_send="--no-send" in sys.argv))
    elif cmd == "prune-media":
        max_age_days, max_total_mb = 30, 500
        for a in sys.argv[2:]:
            if a.startswith("--max-age-days="):
                try:
                    max_age_days = int(a.split("=", 1)[1])
                except ValueError:
                    pass
            if a.startswith("--max-total-mb="):
                try:
                    max_total_mb = int(a.split("=", 1)[1])
                except ValueError:
                    pass
        sys.exit(cmd_prune_media(max_age_days=max_age_days,
                                 max_total_mb=max_total_mb,
                                 dry_run="--dry-run" in sys.argv))
    elif cmd == "warm-wait":
        # 2026-11-20 第二轮：warm-wait 归 hook supervisor 所有（setsid nohup
        # 起，见 wechat-inbox.sh §1f），worker 不再自己跑它——worker 会话结束
        # 时 runtime 会清掉它名下的后台进程，background=true + setsid 三轮
        # 实测仍在 5 秒～4.5 分钟内死光。这里的 setsid 只是兜底（前台误起/
        # 测试时多一层保护）。已是组长时 setsid 报 EPERM——那正是已脱离的
        # 状态，直接继续。
        # 注意：只在 __main__ 入口做，绝不在 cmd_warm_wait() 里做——测试
        # 是进程内调 cmd_warm_wait，setsid 会把 pytest 进程也脱离出去。
        try:
            os.setsid()
        except OSError:
            pass
        idle_secs, cap_secs, resident, max_claims = _parse_warm_wait_args(sys.argv[2:])
        sys.exit(cmd_warm_wait(idle_secs=idle_secs, cap_secs=cap_secs,
                               resident=resident, max_claims=max_claims))
    elif cmd == "request-warm":
        # worker 回完一轮后请求常驻：只写 warm-request.json，hook supervisor
        # 5 秒内拉起 hook 所有的 warm-wait。worker 绝不自己跑 warm-wait。
        sys.exit(cmd_request_warm(_parse_request_warm_args(sys.argv[2:])))
    elif cmd == "claim":
        # warm-wait 专用：认领 inbox 新消息，只认领、不唤醒（见 cmd_claim）。
        sys.exit(cmd_claim())
    elif cmd == "watch-outbox":
        # 常驻 outbox 盯梢：回复文件一出现就代发（见 cmd_watch_outbox）。
        # hook §1h 负责拉起/重拉（setsid nohup）；这里 setsid 只是兜底
        # （前台误起时多一层保护）。只在 __main__ 入口做，绝不在
        # cmd_watch_outbox() 里做——测试是进程内调它，setsid 会把
        # pytest 进程也脱离出去（同 warm-wait 的教训）。
        try:
            os.setsid()
        except OSError:
            pass
        poll_secs, retry_secs, max_secs = _parse_watch_outbox_args(sys.argv[2:])
        sys.exit(cmd_watch_outbox(poll_secs=poll_secs, retry_secs=retry_secs,
                                  max_secs=max_secs))
    elif cmd == "keepalive":
        # worker 存活心跳：认领后 worker 以前台 exec 起，每 10 秒 touch
        # 一次 .alive；单子回完自己停。绝不要用 background=true 起它
        # （见 cmd_keepalive docstring：会脱离 worker 会话、伪装活 worker）。
        if len(sys.argv) < 3:
            print("usage: wxbot.py keepalive <msg_id> [--interval-secs N] [--max-secs N]", flush=True)
            sys.exit(1)
        interval_secs, max_secs = _parse_keepalive_args(sys.argv[3:])
        sys.exit(cmd_keepalive(sys.argv[2], interval_secs=interval_secs,
                               max_secs=max_secs))
    else:
        print("usage: wxbot.py {qr|wait_login|receive [--drain]|send [only_fn]|merge-backlog|finish <msg_id> [expected_claim_ts]|typing <user_id> <command>|bind|approve-owner <id>|list-unknown|remove-owner <id>|health [--offline]|watchdog [--max-age-mins N]|alert <kind> <failing|recovered>|stats|preflight|smoke-test [--no-send]|prune-media [--dry-run]|warm-wait|request-warm [--cap-secs N]|claim|watch-outbox [--poll-secs N] [--retry-secs N] [--max-secs N]|keepalive <msg_id>}")
        sys.exit(1)
