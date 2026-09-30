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
  send        Send all outbox/*.json -> sent/
              (text chunked, optional "image"/"video"/"file" local paths)
  finish      Worker one-shot: send outbox/<id>.json, move processing/<id>.json
              -> done/, append latency entry. FINISH_FAIL leaves processing/
              for the 5-min fallback sweeper.
"""
import sys, os, json, time, uuid, re, fcntl

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
LATENCY_LOG = os.path.join(os.path.expanduser("~"),
                           "workspace/goals/muse/hidden_files/wechat-latency.jsonl")
MEDIA = os.path.join(BASE, "media")
QRFILE = os.path.join(BASE, "qr.json")
QRPNG = os.path.join(BASE, "qr.png")
SESSION_EXPIRED = -14
CHUNK = 3500

for d in (INBOX, OUTBOX, SENT, DONE, MEDIA, PROCESSING):
    os.makedirs(d, exist_ok=True)


def load_creds():
    with open(CREDS) as f:
        return json.load(f)


def save_creds(creds):
    tmp = CREDS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS)


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
    with open(QRFILE, "w") as f:
        json.dump({"qrcode": qrcode, "content": content, "ts": time.time()}, f)
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
        safe = re.sub(r"[^A-Za-z0-9_@.\-]", "_", str(user_id))[:64]
        path = os.path.join(HISTORY_DIR, safe + ".jsonl")
        with open(path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "role": role, "text": text},
                               ensure_ascii=False) + "\n")
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > HISTORY_MAX_LINES:
            with open(path, "w") as f:
                f.writelines(lines[-HISTORY_MAX_LINES:])
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
    cache[str(mid)] = text
    while len(cache) > MSGCACHE_MAX:
        cache.pop(next(iter(cache)))
    try:
        with open(MSGCACHE, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
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
        bits = [p for p in (ref_title, ref_body) if p]
        if bits:
            return "[引用: %s]\n" % " | ".join(bits)
        return "[引用: 早些的消息]\n"
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


def send_text_retry(api, to, ctx, txt, fn=""):
    """Send one text chunk with one retry (from CowAgent's _send retry)."""
    r = None
    for _ in range(2):
        try:
            r = api.send_text(to, txt, ctx)
        except Exception as e:
            print("SEND_EXC", fn, e, flush=True)
            r = None
        if r is not None:
            check_send_response(r, fn)
            if r.get("ret", 0) == 0:
                return r
        time.sleep(3)
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


def cmd_receive(drain=False):
    creds = load_creds()
    api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                    token=creds.get("token", ""))
    buf = open(BUF).read().strip() if os.path.exists(BUF) else ""
    seen = set()
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
        buf = resp.get("get_updates_buf", buf)
        with open(BUF, "w") as f:
            f.write(buf)
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
            if mid in seen:
                continue
            seen.add(mid)
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
            if not text and not image_path:
                # 用户发了视频/文件：以前静默丢弃，现在转成标记让 worker 看到、
                # 由 Muse 如实回复"收到了但暂时看不了"，而不是装没看见
                media_note = ""
                for item in m.get("item_list", []) or []:
                    itype = item.get("type", 0)
                    if itype == 5:
                        media_note = "[视频]"
                        break
                    if itype == 4:
                        media_note = "[文件]"
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
            with open(os.path.join(INBOX, mid + ".json"), "w") as f:
                json.dump(rec, f, ensure_ascii=False)
            append_history(rec["from"], "user", text)  # 会话历史：用户侧
            desc = text[:30].replace("\n", " ") if text else "[图片]"
            print("INBOX", mid, desc, flush=True)


def _is_stale_ack(fn):
    """ack_<id>.json 是否已过期：同 id 的真正回复已经发出。

    sent/<id>.json 或 done/<id>.json 存在 = 回复已送达，
    此时再发"收到，正在想，稍等…"只会让用户困惑（2026-09-29 实测：
    ack 的 API 请求比真正回复晚到服务器，导致先看到答案、后看到"正在想"）。
    过期则直接归档，不再发送。
    """
    if not fn.startswith("ack_"):
        return False
    base = fn[len("ack_"):]
    return (os.path.exists(os.path.join(SENT, base))
            or os.path.exists(os.path.join(DONE, base)))


def cmd_send():
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
        _cmd_send_locked(api, msg_cache)
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
    tmp = _parts_ledger_path(fn) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(parts), f)
    os.replace(tmp, _parts_ledger_path(fn))


def _clear_parts_ledger(fn):
    try:
        os.remove(_parts_ledger_path(fn))
    except OSError:
        pass


def _cmd_send_locked(api, msg_cache):
    # 账本文件 *.parts.json 也以 .json 结尾，必须排除，否则会被当成待发任务
    files = sorted(f for f in os.listdir(OUTBOX)
                   if f.endswith(".json") and not f.endswith(".parts.json"))
    for fn in files:
        path = os.path.join(OUTBOX, fn)
        if _is_stale_ack(fn):
            try:
                os.replace(path, os.path.join(SENT, fn))
            except OSError:
                pass
            print("SENT_SKIP_STALE_ACK", fn, flush=True)
            continue
        try:
            with open(path) as f:
                job = json.load(f)
            to, ctx, text = job["to"], job.get("context_token", ""), job.get("text", "")
            reply_parts = []  # 本次实际发出的内容，整单成功后一次性记入历史
            parts_done = _load_parts_ledger(fn)  # 已落地的分片，重试时跳过
            chunks = split_text(text) or [""]
            for i, ch in enumerate(chunks):
                pid = "text:%d" % i
                if pid in parts_done:
                    reply_parts.append(ch)  # 历史仍记全量
                    continue
                r = send_text_retry(api, to, ctx, ch, fn)
                if not r or r.get("ret", 0) != 0:
                    print("SEND_FAIL", fn, r, flush=True)
                    break
                cache_msg_text(msg_cache, _resp_msg_id(r), ch)
                reply_parts.append(ch)
                _mark_part_done(fn, parts_done, pid)
                time.sleep(0.5)
            else:
                img = job.get("image")
                if img:
                    if not os.path.isfile(img):
                        print("SEND_FAIL", fn, "image not found:", img, flush=True)
                        continue
                    if "image" not in parts_done:
                        up = upload_media_to_cdn(api, img, to, media_type=1)
                        r = api.send_image_item(to, ctx, up["encrypt_query_param"],
                                                up["aes_key_b64"], up["ciphertext_size"])
                        check_send_response(r, fn)
                        if r.get("ret", 0) != 0:
                            print("SEND_FAIL", fn, "image:", r, flush=True)
                            continue
                        cache_msg_text(msg_cache, _resp_msg_id(r), "[图片]")
                        _mark_part_done(fn, parts_done, "image")
                    reply_parts.append("[图片]")
                    time.sleep(0.5)
                vid = job.get("video")
                if vid:
                    if not os.path.isfile(vid):
                        print("SEND_FAIL", fn, "video not found:", vid, flush=True)
                        continue
                    if "video" not in parts_done:
                        up = upload_media_to_cdn(api, vid, to, media_type=2)
                        r = api.send_video_item(to, ctx, up["encrypt_query_param"],
                                                up["aes_key_b64"], up["ciphertext_size"])
                        check_send_response(r, fn)
                        if r.get("ret", 0) != 0:
                            print("SEND_FAIL", fn, "video:", r, flush=True)
                            continue
                        cache_msg_text(msg_cache, _resp_msg_id(r), "[视频]")
                        _mark_part_done(fn, parts_done, "video")
                    reply_parts.append("[视频]")
                    time.sleep(0.5)
                fpath = job.get("file")
                if fpath:
                    if not os.path.isfile(fpath):
                        print("SEND_FAIL", fn, "file not found:", fpath, flush=True)
                        continue
                    if "file" not in parts_done:
                        up = upload_media_to_cdn(api, fpath, to, media_type=3)
                        r = api.send_file_item(to, ctx, up["encrypt_query_param"],
                                               up["aes_key_b64"],
                                               os.path.basename(fpath), up["raw_size"])
                        check_send_response(r, fn)
                        if r.get("ret", 0) != 0:
                            print("SEND_FAIL", fn, "file:", r, flush=True)
                            continue
                        cache_msg_text(msg_cache, _resp_msg_id(r),
                                       "[文件] " + os.path.basename(fpath))
                        _mark_part_done(fn, parts_done, "file")
                    reply_parts.append("[文件] " + os.path.basename(fpath))
                    time.sleep(0.5)
                if os.path.exists(SEND_EXPIRED_MARKER):
                    os.remove(SEND_EXPIRED_MARKER)
                append_history(to, "assistant",  # 会话历史：整单成功才记
                               "\n".join(p for p in reply_parts if p) or "[空回复]")
                os.replace(path, os.path.join(SENT, fn))
                _clear_parts_ledger(fn)
                print("SENT", fn, flush=True)
                continue
        except Exception as e:
            print("SEND_ERROR", fn, e, flush=True)


def cmd_finish(msg_id):
    """worker 单条命令收尾：发 outbox/<id>.json → processing→done → 写耗时打点。

    前置：processing/<id>.json 已存在（hook 脚本认领时 mv 过来，并打了 claim_ts），
    outbox/<id>.json 已由 worker 写好（含 to/context_token/text，可选 image/video/file）。
    发送复用 cmd_send 的加锁整单逻辑（含分片账本/重试/历史记录/引用缓存）。
    失败时 processing 留在原地，由 5 分钟兜底 cron 接手，绝不静默丢单。
    """
    import datetime
    t0 = time.time()
    proc_path = os.path.join(PROCESSING, msg_id + ".json")
    out_path = os.path.join(OUTBOX, msg_id + ".json")
    try:
        with open(proc_path) as f:
            proc = json.load(f)
    except (OSError, ValueError):
        # 幂等收尾：processing 已不在说明这单已处理完，残留的 outbox 是孤儿文件，
        # 直接删掉防止被下次 sweep-send 误发造成重复
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        print("FINISH_FAIL", msg_id, "no processing file", flush=True)
        return 1
    claim_ts = proc.get("claim_ts") or t0
    try:
        with open(out_path) as f:
            job = json.load(f)
    except (OSError, ValueError):
        print("FINISH_FAIL", msg_id, "no usable outbox file", flush=True)
        return 1
    out_mtime = os.path.getmtime(out_path)

    cmd_send()  # 加锁发送 outbox（整单逻辑：分段/图片/账本/历史都在里面）

    if not os.path.exists(os.path.join(SENT, msg_id + ".json")):
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
        cmd_send()
    elif cmd == "finish":
        if len(sys.argv) < 3:
            print("usage: wxbot.py finish <msg_id>", flush=True)
            sys.exit(1)
        sys.exit(cmd_finish(sys.argv[2]))
    else:
        print("usage: wxbot.py {qr|wait_login|receive [--drain]|send|finish <msg_id>}")
        sys.exit(1)
