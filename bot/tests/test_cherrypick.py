"""Cherry-pick 回归测试（2026-10-16）：从 optimized 版挑入的收益项。

覆盖：引用磁盘回读、引用图片关联、ack 过期判 outbox、ack 不进历史、
finish 误判 FINISH_OK、finish 回填 to/context_token、merge 单条不打包、
merge 同分钟吸收、merge 只删已合并、单文本无尾随睡眠、图文并行上传。
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_cherrypick_under_test", os.path.join(BASE, "..", "wxbot.py"))
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
    tmp = tempfile.mkdtemp(prefix="cherrypicktest-")
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
    wxbot.LATENCY_LOG = os.path.join(tmp, "wechat-latency.jsonl")
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False)


def test_extract_ref_text_reloads_disk_cache_on_miss():
    """引用解析：内存缓存 miss 时回读磁盘，否则引用刚发出的机器人回复解析不到。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    qid = "bot-reply-99"
    write_json(os.path.join(dirs["outbox"], "..", "msg_cache.json"),
               {qid: "1. 星际穿越 2. 盗梦空间"})
    cache = {}
    msg = {"item_list": [{"type": 1, "text_item": {"text": "第二部是谁拍的？"},
                          "ref_msg": {"message_item": {"type": 0, "msg_id": qid}}}]}
    got = wxbot.extract_ref_text(msg, cache)
    assert "星际穿越" in got, got
    assert cache.get(qid) == "1. 星际穿越 2. 盗梦空间"


def test_find_ref_image_path_resolves_quoted_image():
    """引用历史图片：media/in_<qid>.jpg 存在 → 返回路径；不存在 → 空。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    qid = "img-123"
    p = os.path.join(dirs["media"], "in_%s.jpg" % qid)
    with open(p, "wb") as f:
        f.write(b"fake-jpg")
    msg = {"item_list": [{"ref_msg": {"message_item": {"msg_id": qid}}}]}
    assert wxbot.find_ref_image_path(msg) == p
    msg2 = {"item_list": [{"ref_msg": {"message_item": {"msg_id": "nope"}}}]}
    assert wxbot.find_ref_image_path(msg2) == ""


def test_is_msg_already_processed_disk_backed():
    """磁盘去重兜底：INBOX/PROCESSING/DONE/SENT 里有同名文件 = 已处理过。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    assert wxbot._is_msg_already_processed("m-1") is False
    write_json(os.path.join(dirs["inbox"], "m-1.json"), {"msg_id": "m-1"})
    assert wxbot._is_msg_already_processed("m-1") is True
    assert wxbot._is_msg_already_processed("m-2") is False


def test_stale_ack_skipped_when_outbox_reply_ready():
    """ack 过期：outbox/<id>.json 已写好（回复马上就发）→ ack 多余，判过期。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "555"
    write_json(os.path.join(dirs["outbox"], mid + ".json"),
               {"to": "u", "text": "reply"})
    assert wxbot._is_stale_ack("ack_%s.json" % mid) is True
    os.remove(os.path.join(dirs["outbox"], mid + ".json"))
    assert wxbot._is_stale_ack("ack_%s.json" % mid) is False


def test_ack_does_not_pollute_conversation_history():
    """ack 延迟提示不记会话历史：它不是真正的回复，记进去是噪音。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    api = FakeApi()
    fn = "ack_42.json"
    write_json(os.path.join(dirs["outbox"], fn),
               {"to": "u1", "context_token": "c", "text": "收到你的消息啦"})
    ok, _ = wxbot._send_one_file(api, {}, fn, os.path.join(dirs["outbox"], fn))
    assert ok is True
    assert api.text_calls == ["收到你的消息啦"]
    assert os.listdir(dirs["history"]) == [], "ack 绝不能写 history"


def test_finish_does_not_falsely_succeed_when_prior_sent_exists():
    """finish 成功判定：sent/ 有旧记录、但本轮 outbox 未发出 → 必须 FINISH_FAIL，
    绝不能因为 sent/ 存在就误判 FINISH_OK。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "m-prior"
    write_json(os.path.join(dirs["processing"], mid + ".json"),
               {"from": "u", "text": "hi", "claim_ts": 1000})
    write_json(os.path.join(dirs["outbox"], mid + ".json"),
               {"to": "u", "context_token": "c", "text": "new reply"})
    write_json(os.path.join(dirs["sent"], mid + ".json"),
               {"to": "u", "text": "old reply already sent"})

    real = wxbot.cmd_send
    wxbot.cmd_send = lambda only=None: None  # 本轮发送失败，outbox 还在
    try:
        rc = wxbot.cmd_finish(mid, "1000")
    finally:
        wxbot.cmd_send = real
    assert rc == 1
    assert os.path.exists(os.path.join(dirs["outbox"], mid + ".json"))


def test_finish_backfills_missing_to_and_context_token():
    """finish 回填：worker 漏写 to/context_token 时从 processing 补，不因此失败。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "m-backfill"
    write_json(os.path.join(dirs["processing"], mid + ".json"),
               {"from": "u9", "text": "hi", "claim_ts": 1234,
                "context_token": "ctx-9"})
    write_json(os.path.join(dirs["outbox"], mid + ".json"), {"text": "reply"})

    seen = {}
    real = wxbot.cmd_send

    def fake_send(only=None):
        seen["job"] = json.load(open(os.path.join(dirs["outbox"], mid + ".json")))
        os.replace(os.path.join(dirs["outbox"], mid + ".json"),
                   os.path.join(dirs["sent"], mid + ".json"))
    wxbot.cmd_send = fake_send
    try:
        rc = wxbot.cmd_finish(mid, "1234")
    finally:
        wxbot.cmd_send = real
    assert rc == 0
    assert seen["job"]["to"] == "u9"
    assert seen["job"]["context_token"] == "ctx-9"


def _flash(outbox, name, text):
    write_json(os.path.join(outbox, name),
               {"to": "u1", "context_token": "c", "text": text})


def test_merge_backlog_keeps_single_remaining_flash():
    """merge：剔除已送达后只剩 1 条 → 不包成"1 条汇总"，原文件原样待发。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    delivered = {"d1": "已送达1", "d2": "已送达2", "d3": "已送达3"}
    write_json(wxbot.MSGCACHE, delivered)
    _flash(dirs["outbox"], "flash-202610161000-1.json", "已送达1")
    _flash(dirs["outbox"], "flash-202610161000-2.json", "已送达2")
    _flash(dirs["outbox"], "flash-202610161000-3.json", "已送达3")
    _flash(dirs["outbox"], "flash-202610161000-4.json", "⚡ 快讯：唯一剩下的")
    wxbot.cmd_merge_backlog()
    assert os.path.exists(os.path.join(dirs["outbox"], "flash-202610161000-4.json"))
    digests = [f for f in os.listdir(dirs["outbox"]) if f.endswith("-digest.json")]
    assert digests == [], digests


def test_merge_backlog_absorbs_existing_digest():
    """merge：同分钟已有 digest → 吸收旧标题，不覆盖丢标题。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    import datetime
    ts = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
          .strftime("%Y%m%d%H%M"))
    digest_fn = "flash-%s-digest.json" % ts
    write_json(os.path.join(dirs["outbox"], digest_fn),
               {"to": "u1", "text": "📰 快讯汇总（2条，通道恢复补发）\n· 旧标题A\n· 旧标题B"})
    for i, t in enumerate(["新标题1", "新标题2", "新标题3", "新标题4"], start=1):
        _flash(dirs["outbox"], "flash-202610161000-%d.json" % i, "⚡ 快讯：%s" % t)
    wxbot.cmd_merge_backlog()
    digest = json.load(open(os.path.join(dirs["outbox"], digest_fn)))
    for t in ("旧标题A", "旧标题B", "新标题1", "新标题2", "新标题3", "新标题4"):
        assert t in digest["text"], digest["text"]
    assert len([f for f in os.listdir(dirs["outbox"])
                if f.startswith("flash-202610161000-")]) == 0


def test_merge_backlog_keeps_unparseable_file():
    """merge：解析失败的文件不删（留着排查），只删真正合并进来的。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    for i in range(4):
        _flash(dirs["outbox"], "flash-202610161000-%d.json" % i, "⚡ 快讯：标题%d" % i)
    with open(os.path.join(dirs["outbox"], "flash-202610161000-bad.json"), "w") as f:
        f.write("{not json")
    wxbot.cmd_merge_backlog()
    assert os.path.exists(os.path.join(dirs["outbox"], "flash-202610161000-bad.json"))
    assert len([f for f in os.listdir(dirs["outbox"])
                if f.startswith("flash-202610161000-")]) == 1


def test_no_trailing_sleep_on_single_chunk_send():
    """单文本消息：发完直接收尾，不空等 0.5 秒。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    api = FakeApi()
    fn = "111.json"
    write_json(os.path.join(dirs["outbox"], fn),
               {"to": "u1", "context_token": "c", "text": "hi"})
    sleeps = []
    wxbot.time.sleep = lambda s: sleeps.append(s)
    try:
        ok, _ = wxbot._send_one_file(api, {}, fn, os.path.join(dirs["outbox"], fn))
    finally:
        wxbot.time.sleep = lambda s: None
    assert ok is True
    assert sleeps == [], sleeps


def test_image_upload_overlaps_text_send():
    """图文混排：图片 CDN 上传与文字发送并行；文字与图片之间仍隔 0.5 秒。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    api = FakeApi()
    img_path = os.path.join(dirs["media"], "pic.jpg")
    with open(img_path, "wb") as f:
        f.write(b"fake-jpg")
    uploads = []
    real_upload = wxbot.upload_media_to_cdn

    def fake_upload(api_, path, to, media_type=1):
        uploads.append(path)
        return {"encrypt_query_param": "enc", "aes_key_b64": "aes",
                "ciphertext_size": 10}
    wxbot.upload_media_to_cdn = fake_upload
    sleeps = []
    wxbot.time.sleep = lambda s: sleeps.append(s)
    try:
        fn = "222.json"
        write_json(os.path.join(dirs["outbox"], fn),
                   {"to": "u1", "context_token": "c", "text": "看图", "image": img_path})
        ok, _ = wxbot._send_one_file(api, {}, fn, os.path.join(dirs["outbox"], fn))
    finally:
        wxbot.upload_media_to_cdn = real_upload
        wxbot.time.sleep = lambda s: None
    assert ok is True
    assert uploads == [img_path]
    assert api.text_calls == ["看图"]
    assert api.image_calls == [("u1", "enc")]
    assert sleeps == [0.5], sleeps
