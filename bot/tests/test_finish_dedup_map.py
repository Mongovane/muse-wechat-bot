"""去重粒度回归测试（2026-11-04）。

旧逻辑按回复文本查 msg_cache：同一句话发给不同消息会误判 FINISH_DUP
（11-04 "1" 的"链路是通的。"撞上 11-03 同文回复，确认被拦）；
且同单文字已出、图片待发时也会误拦、丢掉待发图片。
新逻辑按本单 inbound msg_id 查 reply_map：只有本单整单成功送达过
才抑制；不同消息、部分送达都不抑制（部分送达靠 parts 账本续发）。
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_dedup_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    def __init__(self):
        self.text_calls = []

    def send_text(self, to, txt, ctx):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "mid-%d" % len(self.text_calls)}

    def _post(self, endpoint, body, retries=0):
        return {"ret": 0, "message_id": "mid-omit"}


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="deduptest-")
    dirs = {}
    for name in ("outbox", "sent", "history", "done", "processing"):
        d = os.path.join(tmp, name)
        os.makedirs(d)
        dirs[name] = d
    wxbot.OUTBOX = dirs["outbox"]
    wxbot.SENT = dirs["sent"]
    wxbot.DONE = dirs["done"]
    wxbot.PROCESSING = dirs["processing"]
    wxbot.HISTORY_DIR = dirs["history"]
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    wxbot.SEND_LOCK = os.path.join(tmp, "send.lock")
    wxbot.LATENCY_LOG = os.path.join(tmp, "wechat-latency.jsonl")
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    # 回复送达账本 + typing 票据：必须指向 tmp，绝不写真实生产文件
    wxbot.REPLY_MAP = os.path.join(tmp, "reply_map.json")
    wxbot.TYPING_TICKETS_FILE = os.path.join(tmp, "typing_tickets.json")
    # 2026-11-19 新增：发送账本/审计/引用缓存/token stale 也指向 tmp
    wxbot.SEND_LEDGER = os.path.join(tmp, "send_ledger.json")
    wxbot.SEND_LEDGER_LOCK = os.path.join(tmp, "send_ledger.lock")
    wxbot.SEND_AUDIT = os.path.join(tmp, "send_audit.jsonl")
    wxbot.QUOTE_CACHE_FILE = os.path.join(tmp, "quote_cache.json")
    wxbot.TOKEN_STALE_FILE = os.path.join(tmp, "token_stale.json")
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def write_proc(dirs, mid, text="hi"):
    with open(os.path.join(dirs["processing"], mid + ".json"), "w") as f:
        json.dump({"from": "u", "text": text, "claim_ts": 1000}, f)


def write_job(dirs, name, text):
    with open(os.path.join(dirs["outbox"], name), "w") as f:
        json.dump({"to": "u1", "context_token": "ctx", "text": text}, f)


def fake_send_factory(wxbot, dirs, calls):
    def fake_send(only=None):
        calls.append(only)
        # 模拟整单成功：outbox 文件归档到 sent/
        src = os.path.join(dirs["outbox"], only)
        if os.path.exists(src):
            os.replace(src, os.path.join(dirs["sent"], only))
    return fake_send


def test_dup_suppressed_for_same_id():
    """本单已在账本里 → finish 抑制，不调 send，直接归档。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "7523549262655797768"
    write_proc(dirs, mid)
    write_job(dirs, mid + ".json", "链路是通的。")
    with open(wxbot.REPLY_MAP, "w") as f:
        json.dump({mid: {"ts": "2026-11-04T09:00:00", "text": "链路是通的。"}},
                  f)
    calls = []
    wxbot.cmd_send = fake_send_factory(wxbot, dirs, calls)
    assert wxbot.cmd_finish(mid) == 0
    assert calls == [], "same-id dup must not send again"
    assert not os.path.exists(os.path.join(dirs["outbox"], mid + ".json"))
    assert os.path.exists(os.path.join(dirs["sent"], mid + ".json"))
    assert os.path.exists(os.path.join(dirs["done"], mid + ".json"))


def test_same_text_different_id_sends():
    """11-04 回归：不同消息、同文本 → 必须发，文本撞车不再抑制。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    old_id = "7510000000000000001"
    mid = "7523549262655797768"
    # 旧单的同文回复已在账本 + msg_cache（模拟 11-03 的"链路是通的。"）
    with open(wxbot.REPLY_MAP, "w") as f:
        json.dump({old_id: {"ts": "2026-11-03T08:00:00",
                            "text": "链路是通的。"}}, f)
    with open(wxbot.MSGCACHE, "w") as f:
        json.dump({"mid-old": "链路是通的。"}, f)
    write_proc(dirs, mid)
    write_job(dirs, mid + ".json", "链路是通的。")
    calls = []
    wxbot.cmd_send = fake_send_factory(wxbot, dirs, calls)
    assert wxbot.cmd_finish(mid) == 0
    assert calls == [mid + ".json"], "different id must send despite same text"
    assert os.path.exists(os.path.join(dirs["sent"], mid + ".json"))
    assert os.path.exists(os.path.join(dirs["done"], mid + ".json"))


def test_partial_delivery_not_suppressed():
    """文字已在 cache（分片已出）但账本无本单 → 必须发，不丢待发图片。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "7523549262655797769"
    with open(wxbot.MSGCACHE, "w") as f:
        json.dump({"mid-part": "hello"}, f)
    # 账本为空：整单从未成功过（只出了文字分片）
    write_proc(dirs, mid)
    write_job(dirs, mid + ".json", "hello")
    calls = []
    wxbot.cmd_send = fake_send_factory(wxbot, dirs, calls)
    assert wxbot.cmd_finish(mid) == 0
    assert calls == [mid + ".json"], "partial delivery must resume, not dup"


def test_record_on_full_send():
    """整单成功后，纯数字回复文件必须记入账本。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "7523549262655797770"
    write_job(dirs, mid + ".json", "hello")
    ok, transient = wxbot._send_one_file(
        FakeApi(), {}, mid + ".json",
        os.path.join(dirs["outbox"], mid + ".json"))
    assert (ok, transient) == (True, False)
    assert os.path.exists(os.path.join(dirs["sent"], mid + ".json"))
    with open(wxbot.REPLY_MAP) as f:
        m = json.load(f)
    assert mid in m and m[mid]["text"] == "hello"


def test_proactive_not_recorded():
    """proactive 文件（flash- 前缀）整单成功也不记入回复账本。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    fn = "flash-202611040101-1.json"
    write_job(dirs, fn, "flash text")
    ok, _ = wxbot._send_one_file(
        FakeApi(), {}, fn, os.path.join(dirs["outbox"], fn))
    assert ok is True
    assert os.path.exists(os.path.join(dirs["sent"], fn))
    if os.path.exists(wxbot.REPLY_MAP):
        with open(wxbot.REPLY_MAP) as f:
            m = json.load(f)
        assert fn not in m


def test_corrupt_map_fail_open():
    """账本损坏 → 检查失败不阻断，继续发送（宁可重发，不丢单）。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "7523549262655797771"
    with open(wxbot.REPLY_MAP, "w") as f:
        f.write("{not json")
    write_proc(dirs, mid)
    write_job(dirs, mid + ".json", "hello")
    calls = []
    wxbot.cmd_send = fake_send_factory(wxbot, dirs, calls)
    assert wxbot.cmd_finish(mid) == 0
    assert calls == [mid + ".json"]
