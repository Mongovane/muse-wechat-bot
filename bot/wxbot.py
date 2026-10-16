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
"""
import sys, os, json, time, uuid, re, fcntl
import datetime

sys.path.insert(0, os.environ.get("COWAGENT_HOME", "/home/hatch/workspace/CowAgent"))
from channel.weixin.weixin_api import (  # noqa: E402
    WeixinApi, DEFAULT_BASE_URL, upload_media_to_cdn,
    download_media_from_cdn, CDN_BASE_URL,
)

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
# sendtyping 票据缓存：getconfig 拿到的 typing_ticket，有效期约 20 小时
# （nanoclaw #1553 实测；agentserver 独立落地同一 API，确认客户端真渲染）。
TYPING_TICKETS_FILE = os.path.join(BASE, "typing_tickets.json")
TYPING_TICKET_TTL = 20 * 3600
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
QUOTA_FILE = os.path.join(BASE, "quota.json")
QUOTA_MAX = 10
QUOTA_ACK_LIMIT = 5
# proactive 优先级 → 允许发送的配额上限（quota < 上限才发）
# 1=紧急突发：quota<9 几乎总发；2=普通快讯/卡片：quota<5；3=低优汇总：quota<3
QUOTA_PRIORITY_LIMIT = {1: 9, 2: 5, 3: 3}

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
    """获取或复用缓存的 typing_ticket（有效期约 20 小时）。

    2026-10-16 借鉴 nanoclaw #1553 实测：getconfig 请求体必须显式带 ilink_user_id，
    否则返回 ret=-2 errmsg='ilink_user_id required'。票据损坏/过期时重新获取。
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
        except (TypeError, ValueError):
            tk, exp = "", 0
        if tk and now < exp:
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
        print("SESSION_EXPIRED_SEND", fn, flush=True)
        with open(SEND_EXPIRED_MARKER, "w") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fn))


def _send_text_omit_token(api, to, txt):
    """hermes-agent 的野路子（2026-10-08）：完全省略 context_token 字段重发。

    对 "token 过期" 类 ret=-2 可能救回；配额耗尽类大概率无效。
    只在 -2 时试一次，成功则返回响应，失败返回 None。"""
    try:
        r = api._post("ilink/bot/sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": uuid.uuid4().hex[:16],
                "message_type": 2,  # BOT
                "message_state": 2,  # FINISH
                "item_list": [{"type": 1, "text_item": {"text": txt}}],
                # context_token 字段完全省略，不传空字符串
            }
        }, retries=0)
        check_send_response(r, "omit-token")
        return r
    except Exception as e:
        print("SEND_EXC_OMIT", e, flush=True)
        return None


def send_text_retry(api, to, ctx, txt, fn=""):
    """Send one text chunk.

    2026-10-08 简化（Peter：重试叠三层无意义）：只做一次 api.send_text，
    底层 _post 已有连接重试；-2 则试一次 omit-token fallback，不再 3s 睡眠重试。
    2026-10-08 新增：分段计时（SLOW_SEND 诊断慢发送）。
    """
    t0 = time.time()
    try:
        r = api.send_text(to, txt, ctx)
    except Exception as e:
        print("SEND_EXC", fn, e, "dt=%.1fs" % (time.time() - t0), flush=True)
        return None
    dt = time.time() - t0
    if dt > 10:
        print("SLOW_SEND_TEXT", fn, "dt=%.1fs ret=%s"
              % (dt, r.get("ret") if r else None), flush=True)
    if r is not None:
        check_send_response(r, fn)
        if r.get("ret", 0) == 0:
            return r
    # -2：试一次去 token 重发（hermes-agent 招数）
    if r is not None and r.get("ret") == -2:
        print("TRY_OMIT_TOKEN", fn, flush=True)
        t0 = time.time()
        r2 = _send_text_omit_token(api, to, txt)
        dt = time.time() - t0
        if dt > 10:
            print("SLOW_OMIT_TOKEN", fn, "dt=%.1fs" % dt, flush=True)
        if r2 is not None and r2.get("ret", 0) == 0:
            print("OMIT_TOKEN_OK", fn, flush=True)
            return r2
        print("OMIT_TOKEN_FAIL", fn, flush=True)
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
    """Download an inbound image message from Weixin CDN. Returns local path or ''."""
    info = item.get("image_item", {})
    media = info.get("media", {})
    encrypt_param = media.get("encrypt_query_param", "")
    aes_key = info.get("aeskey", "") or media.get("aes_key", "")
    if not encrypt_param or not aes_key:
        print("IMG_NOPARAM", mid, flush=True)
        return ""
    os.makedirs(MEDIA, exist_ok=True)
    save_path = os.path.join(MEDIA, "in_%s.jpg" % mid)
    try:
        cdn_base = getattr(api, "cdn_base_url", "") or CDN_BASE_URL
        download_media_from_cdn(cdn_base, encrypt_param, aes_key, save_path)
        print("IMG_SAVED", mid, save_path, flush=True)
        return save_path
    except Exception as e:
        print("IMG_FAIL", mid, e, flush=True)
        return ""


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
            print("SESSION_EXPIRED", flush=True)
            sys.exit(2)
        if ret != 0 or errcode != 0:
            print("RECV_API_ERROR", ret, errcode, flush=True)
            time.sleep(5)
            continue
        # 游标先不写盘：本轮消息处理完再写。崩溃在循环中途时游标未推进，
        # 重启后会重拉本轮消息（磁盘去重兜底，不会重复回复）；先写游标则丢消息。
        new_buf = resp.get("get_updates_buf", buf)
        for m in resp.get("msgs", []) or []:
            mtype = m.get("message_type", 0)
            if mtype == 2:
                # 自己发出去的消息：记入引用解析缓存，不回复
                t2 = extract_text(m)
                if t2:
                    cache_msg_text(msg_cache,
                                   m.get("message_id", m.get("seq", "")), t2)
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


def _cmd_send_locked(api, msg_cache, only=None):
    """加锁发送。only=None 发全部；only="<fn>" 只发这一个文件。

    finish 路径传 only=<id>.json：只发本单回复，绝不碰 outbox 里
    其他文件（快讯/早报/卡片等 proactive 只由显式 send 发）。
    滞留的同单 ack（ack_<id>.json）不用 finish 管：下次全量 send 时
    _is_stale_ack 会判它过期（sent/<id>.json 已存在）直接归档不发，
    绝不会迟发一条"正在想"。
    """
    # 账本文件 *.parts.json 也以 .json 结尾，必须排除，否则会被当成待发任务
    files = sorted(f for f in os.listdir(OUTBOX)
                   if f.endswith(".json") and not f.endswith(".parts.json"))
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
            r = send_text_retry(api, to, ctx, ch, fn)
            if not r or r.get("ret", 0) != 0:
                print("SEND_FAIL", fn, r, flush=True)
                return False, _is_transient_send_fail(r)
            cache_msg_text(msg_cache, _resp_msg_id(r), ch)
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
                r = api.send_image_item(to, ctx, up["encrypt_query_param"],
                                        up["aes_key_b64"], up["ciphertext_size"])
                check_send_response(r, fn)
                if r.get("ret", 0) != 0:
                    print("SEND_FAIL", fn, "image:", r, flush=True)
                    return False, _is_transient_send_fail(r)
                # 图片缓存带正文前 60 字：用户引用这条图片消息时，worker 能认出是哪张图
                img_label = "[图片]" if not text else "[图片] %s" % text[:60]
                cache_msg_text(msg_cache, _resp_msg_id(r), img_label)
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
                r = api.send_video_item(to, ctx, up["encrypt_query_param"],
                                        up["aes_key_b64"], up["ciphertext_size"])
                check_send_response(r, fn)
                if r.get("ret", 0) != 0:
                    print("SEND_FAIL", fn, "video:", r, flush=True)
                    return False, _is_transient_send_fail(r)
                cache_msg_text(msg_cache, _resp_msg_id(r), "[视频]")
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
                r = api.send_file_item(to, ctx, up["encrypt_query_param"],
                                       up["aes_key_b64"],
                                       os.path.basename(fpath), up["raw_size"])
                check_send_response(r, fn)
                if r.get("ret", 0) != 0:
                    print("SEND_FAIL", fn, "file:", r, flush=True)
                    return False, _is_transient_send_fail(r)
                cache_msg_text(msg_cache, _resp_msg_id(r),
                               "[文件] " + os.path.basename(fpath))
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
    """
    lock_fh = open(SEND_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        files = sorted(f for f in os.listdir(OUTBOX)
                       if f.startswith("flash-") and f.endswith(".json")
                       and not f.endswith(".parts.json")
                       and not f.endswith("-digest.json"))
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


def cmd_finish(msg_id, expected_claim_ts=None):
    """worker 单条命令收尾：发 outbox/<id>.json → processing→done → 写耗时打点。

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
    else:
        print("usage: wxbot.py {qr|wait_login|receive [--drain]|send [only_fn]|merge-backlog|finish <msg_id> [expected_claim_ts]|typing <user_id> <command>}")
        sys.exit(1)
