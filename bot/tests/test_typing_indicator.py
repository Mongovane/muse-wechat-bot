"""sendtyping 原生输入态 + 缓存小修回归测试（2026-10-16）。

采纳自新版优化包，带两处收紧：
- 取消指示灯只在普通回复路径：proactive（快讯/早报/卡片/汇总）的取消
  会误关正在思考的回复的指示灯；ack_ 文件不取消（worker 还在想）。
- 本轮只发一次，无 keepalive：长思考时指示灯可能中途过期，
  ack_timer 每 3 秒 keepalive 续上（ack 文字已于 2026-10-16 去掉）。
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_typing_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def send_text(self, to, txt, ctx):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "mid-%d" % len(self.text_calls)}

    def send_image_item(self, to, ctx, enc, aes, size):
        self.image_calls.append((to, enc))
        return {"ret": 0, "message_id": "mid-img"}

    def _post(self, endpoint, body, retries=0):
        return {"ret": 0, "message_id": "mid-omit"}


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="typingtest-")
    dirs = {}
    for name in ("inbox", "outbox", "sent", "history", "done", "processing", "media"):
        d = os.path.join(tmp, name)
        os.makedirs(d)
        dirs[name] = d
    wxbot.INBOX = dirs["inbox"]
    wxbot.OUTBOX = dirs["outbox"]
    wxbot.SENT = dirs["sent"]
    wxbot.DONE = dirs["done"]
    wxbot.PROCESSING = dirs["processing"]
    wxbot.MEDIA = dirs["media"]
    wxbot.HISTORY_DIR = dirs["history"]
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    wxbot.SEND_LOCK = os.path.join(tmp, "send.lock")
    wxbot.RECEIVE_LOCK = os.path.join(tmp, "receive.lock")
    wxbot.TYPING_TICKETS_FILE = os.path.join(tmp, "typing_tickets.json")
    wxbot.LATENCY_LOG = os.path.join(tmp, "wechat-latency.jsonl")
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False)


def test_send_typing_indicator_and_ticket_cache():
    """nanoclaw #1553：getconfig 必须带 ilink_user_id；票据缓存 20h，第二
    次调用不重复调 getconfig。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    posts = []

    class FakeTypingApi:
        def _post(self, endpoint, body, retries=0):
            posts.append((endpoint, body))
            if endpoint == "ilink/bot/getconfig":
                assert body.get("ilink_user_id") == "wxid_peter"
                return {"ret": 0, "typing_ticket": "tk_secret_123"}
            if endpoint == "ilink/bot/sendtyping":
                assert body.get("ilink_user_id") == "wxid_peter"
                assert body.get("to_user_id") == "wxid_peter"
                assert body.get("typing_ticket") == "tk_secret_123"
                assert body.get("command") in (1, 2)
                return {"ret": 0}
            return {"ret": -1}

    api = FakeTypingApi()
    assert wxbot.send_typing_indicator(api, "wxid_peter", command=1) is True
    assert len(posts) == 2  # 第 1 次：getconfig + sendtyping
    assert wxbot.send_typing_indicator(api, "wxid_peter", command=2) is True
    assert len(posts) == 3  # 第 2 次：直接命中 20h 缓存，只发 sendtyping
    assert posts[2][0] == "ilink/bot/sendtyping"
    assert posts[2][1]["command"] == 2


def test_send_typing_indicator_tolerates_corrupt_ticket_file():
    """票据文件损坏（expires_at 非数字）→ 当无缓存重新获取，不抛异常。"""
    wxbot = load_wxbot()
    make_env(wxbot)

    with open(wxbot.TYPING_TICKETS_FILE, "w") as f:
        json.dump({"u1": {"ticket": "stale", "expires_at": "not-a-number"}}, f)

    class FakeTypingApi:
        def _post(self, endpoint, body, retries=0):
            if endpoint == "ilink/bot/getconfig":
                return {"ret": 0, "typing_ticket": "fresh-ticket"}
            return {"ret": 0}

    assert wxbot.send_typing_indicator(FakeTypingApi(), "u1", command=1) is True
    assert wxbot._ensure_typing_ticket(FakeTypingApi(), "u1") == "fresh-ticket"


def _send_file(wxbot, dirs, fn, job):
    """用 _send_one_file 发一个 outbox 文件，返回 typing 取消调用记录。"""
    api = FakeApi()
    calls = []
    real = wxbot.send_typing_indicator
    wxbot.send_typing_indicator = lambda a, u, command=1: calls.append(command) or True
    try:
        path = os.path.join(dirs["outbox"], fn)
        write_json(path, job)
        ok, _ = wxbot._send_one_file(api, {}, fn, path)
    finally:
        wxbot.send_typing_indicator = real
    return ok, calls, api


def test_typing_cancel_only_for_plain_reply():
    """取消指示灯只在普通回复文件；flash/briefing/digest/ack_ 绝不取消
    （会误关正在思考的回复的指示灯）。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    job = {"to": "u1", "context_token": "c", "text": "hi"}

    ok, calls, _ = _send_file(wxbot, dirs, "12345.json", job)
    assert ok is True and calls == [2], calls

    ok, calls, _ = _send_file(wxbot, dirs, "flash-202610161000-1.json", job)
    assert ok is True and calls == [], calls

    ok, calls, _ = _send_file(wxbot, dirs, "briefing-20261016.json", job)
    assert ok is True and calls == [], calls

    ok, calls, _ = _send_file(wxbot, dirs, "flash-202610161000-digest.json", job)
    assert ok is True and calls == [], calls

    ok, calls, _ = _send_file(wxbot, dirs, "ack_12345.json", job)
    assert ok is True and calls == [], calls


def test_cache_msg_text_lru_move_to_end():
    """重复缓存的条目移到末尾：再进新条目时，驱逐的是真正的最老条目，
    而不是刚更新过的那条。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    n = wxbot.MSGCACHE_MAX
    cache = {("k%d" % i): ("v%d" % i) for i in range(n)}
    wxbot.cache_msg_text(cache, "k0", "updated")
    assert list(cache)[-1] == "k0"  # k0 移到末尾
    wxbot.cache_msg_text(cache, "new_mid", "new")
    assert "k0" in cache, "刚更新过的 k0 不该被驱逐"
    assert "k1" not in cache, "最老的 k1 才该被驱逐"
    assert "new_mid" in cache
    assert len(cache) == n


def test_cmd_typing_cli_entry():
    """cmd_typing（ack_timer keepalive 的入口）：读 creds、建 API、调 sendtyping。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    creds_path = os.path.join(tmp, "credentials.json")
    write_json(creds_path,
               {"base_url": "http://127.0.0.1:9/", "token": "t", "cdn_base_url": ""})
    wxbot.CREDS = creds_path
    posts = []

    class FakeTypingApi:
        def __init__(self, base_url="", token="", cdn_base_url=""):
            pass

        def _post(self, endpoint, body, retries=0):
            posts.append((endpoint, body))
            if endpoint == "ilink/bot/getconfig":
                return {"ret": 0, "typing_ticket": "tk-cli"}
            return {"ret": 0}

    wxbot.WeixinApi = FakeTypingApi
    assert wxbot.cmd_typing("u-cli", command=1) is True
    assert [p[0] for p in posts] == ["ilink/bot/getconfig", "ilink/bot/sendtyping"]
    assert posts[1][1]["command"] == 1
    assert wxbot.cmd_typing("u-cli", command=2) is True
    assert posts[2][0] == "ilink/bot/sendtyping"
    assert posts[2][1]["command"] == 2


def test_image_cache_label_includes_text_snippet():
    """图片缓存带正文前 60 字：用户引用这条图片消息时 worker 能认出是哪张图。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    api = FakeApi()
    img_path = os.path.join(dirs["media"], "pic.jpg")
    with open(img_path, "wb") as f:
        f.write(b"fake-jpg")
    real_upload = wxbot.upload_media_to_cdn
    wxbot.upload_media_to_cdn = lambda a, p, t, media_type=1: {
        "encrypt_query_param": "enc", "aes_key_b64": "aes", "ciphertext_size": 10}
    try:
        fn = "222.json"
        write_json(os.path.join(dirs["outbox"], fn),
                   {"to": "u1", "context_token": "c",
                    "text": "这张图是第三季度财报的截图", "image": img_path})
        ok, _ = wxbot._send_one_file(api, {}, fn, os.path.join(dirs["outbox"], fn))
    finally:
        wxbot.upload_media_to_cdn = real_upload
    assert ok is True
    disk = json.load(open(wxbot.MSGCACHE))
    assert any(v == "[图片] 这张图是第三季度财报的截图" for v in disk.values()), disk
