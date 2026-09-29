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
"""
import sys, os, json, time, uuid

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
MEDIA = os.path.join(BASE, "media")
QRFILE = os.path.join(BASE, "qr.json")
QRPNG = os.path.join(BASE, "qr.png")
SESSION_EXPIRED = -14
CHUNK = 3500

for d in (INBOX, OUTBOX, SENT, DONE, MEDIA):
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
            if m.get("message_type", 0) != 1:
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
                continue
            rec = {
                "msg_id": mid,
                "from": m.get("from_user_id", ""),
                "text": text if text else ("[图片]" if image_path else ""),
                "context_token": m.get("context_token", ""),
                "session_id": m.get("session_id", ""),
                "ts": time.time(),
            }
            if image_path:
                rec["image"] = image_path
            with open(os.path.join(INBOX, mid + ".json"), "w") as f:
                json.dump(rec, f, ensure_ascii=False)
            desc = text[:30].replace("\n", " ") if text else "[图片]"
            print("INBOX", mid, desc, flush=True)


def cmd_send():
    creds = load_creds()
    api = WeixinApi(base_url=creds.get("base_url", DEFAULT_BASE_URL),
                    token=creds.get("token", ""))
    files = sorted(f for f in os.listdir(OUTBOX) if f.endswith(".json"))
    for fn in files:
        path = os.path.join(OUTBOX, fn)
        try:
            with open(path) as f:
                job = json.load(f)
            to, ctx, text = job["to"], job.get("context_token", ""), job.get("text", "")
            chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
            for ch in chunks:
                r = api.send_text(to, ch, ctx)
                if r.get("ret", 0) != 0:
                    print("SEND_FAIL", fn, r, flush=True)
                    break
                time.sleep(0.5)
            else:
                img = job.get("image")
                if img:
                    if not os.path.isfile(img):
                        print("SEND_FAIL", fn, "image not found:", img, flush=True)
                        continue
                    up = upload_media_to_cdn(api, img, to, media_type=1)
                    r = api.send_image_item(to, ctx, up["encrypt_query_param"],
                                            up["aes_key_b64"], up["ciphertext_size"])
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "image:", r, flush=True)
                        continue
                    time.sleep(0.5)
                vid = job.get("video")
                if vid:
                    if not os.path.isfile(vid):
                        print("SEND_FAIL", fn, "video not found:", vid, flush=True)
                        continue
                    up = upload_media_to_cdn(api, vid, to, media_type=2)
                    r = api.send_video_item(to, ctx, up["encrypt_query_param"],
                                            up["aes_key_b64"], up["ciphertext_size"])
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "video:", r, flush=True)
                        continue
                    time.sleep(0.5)
                fpath = job.get("file")
                if fpath:
                    if not os.path.isfile(fpath):
                        print("SEND_FAIL", fn, "file not found:", fpath, flush=True)
                        continue
                    up = upload_media_to_cdn(api, fpath, to, media_type=3)
                    r = api.send_file_item(to, ctx, up["encrypt_query_param"],
                                           up["aes_key_b64"],
                                           os.path.basename(fpath), up["raw_size"])
                    if r.get("ret", 0) != 0:
                        print("SEND_FAIL", fn, "file:", r, flush=True)
                        continue
                    time.sleep(0.5)
                os.replace(path, os.path.join(SENT, fn))
                print("SENT", fn, flush=True)
                continue
        except Exception as e:
            print("SEND_ERROR", fn, e, flush=True)


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
    else:
        print("usage: wxbot.py {qr|wait_login|receive [--drain]|send}")
        sys.exit(1)
