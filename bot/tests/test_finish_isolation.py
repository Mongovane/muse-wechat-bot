"""普通回复与 proactive 隔离回归测试（2026-10-16）。

根因：cmd_finish 曾调全量 cmd_send()，每次普通回复都顺带发走
outbox 里的快讯/早报/卡片（2026-10-15 实锤 3 次：08:23 早报带出、
13:50 入站带出、15:37 入站带出）。
修复：finish 只发 outbox/<id>.json（cmd_send(only=...)）；
显式 `wxbot.py send` 仍全量发送 proactive。
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_isolation_under_test", os.path.join(BASE, "..", "wxbot.py"))
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
    tmp = tempfile.mkdtemp(prefix="isolationtest-")
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
    # flash 发送成功会记共享去重池：必须指向 tmp，绝不写真实生产池
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    # 发送会写 msg_cache（cache_msg_text 合并落盘）：指向 tmp，绝不碰生产缓存
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def write_job(outbox, name, text):
    with open(os.path.join(outbox, name), "w") as f:
        json.dump({"to": "u1", "context_token": "ctx", "text": text}, f)


def test_finish_passes_only_to_send():
    """finish 必须调 cmd_send(only=<id>.json)，绝不调全量。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "m-iso1"
    with open(os.path.join(dirs["processing"], mid + ".json"), "w") as f:
        json.dump({"from": "u", "text": "hi", "claim_ts": 1000}, f)
    write_job(dirs["outbox"], mid + ".json", "reply")

    calls = []
    real = wxbot.cmd_send

    def fake_send(only=None):
        calls.append(only)
        os.replace(os.path.join(dirs["outbox"], mid + ".json"),
                   os.path.join(dirs["sent"], mid + ".json"))
    wxbot.cmd_send = fake_send
    try:
        rc = wxbot.cmd_finish(mid)
    finally:
        wxbot.cmd_send = real
    assert rc == 0
    assert calls == [mid + ".json"], calls


def test_scoped_send_only_sends_that_file():
    """finish 的收尾路径：outbox 里同时有回复 + flash + 早报 + 卡片 + 他单 ack
    → 只发对应 ID 的回复，其余原样留在 outbox，绝不带出。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "12345"
    write_job(dirs["outbox"], mid + ".json", "normal reply")
    write_job(dirs["outbox"], "flash-202610151415-1.json", "⚡ 快讯：KOSPI")
    write_job(dirs["outbox"], "briefing-20261016.json", "早报正文")
    write_job(dirs["outbox"], "market-pre-20261016.json", "📈 盘前卡片")
    write_job(dirs["outbox"], "ack_99999.json", "收到你的消息啦")

    api = FakeApi()
    wxbot._cmd_send_locked(api, {}, only=mid + ".json")

    assert os.path.exists(os.path.join(dirs["sent"], mid + ".json"))
    assert api.text_calls == ["normal reply"], api.text_calls
    for fn in ("flash-202610151415-1.json", "briefing-20261016.json",
               "market-pre-20261016.json", "ack_99999.json"):
        assert os.path.exists(os.path.join(dirs["outbox"], fn)), fn
        assert not os.path.exists(os.path.join(dirs["sent"], fn)), fn


def test_scoped_send_missing_file_sends_nothing():
    """空 outbox（only 指向不存在的文件）→ 什么都不发，不崩。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    api = FakeApi()
    wxbot._cmd_send_locked(api, {}, only="ghost.json")
    assert api.text_calls == []
    assert os.listdir(dirs["sent"]) == []


def test_explicit_full_send_still_sends_all():
    """显式全量 send（only=None）→ 全部发出，行为不变。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    write_job(dirs["outbox"], "111.json", "reply-one")
    write_job(dirs["outbox"], "flash-20261016.json", "⚡ 快讯：X")
    write_job(dirs["outbox"], "briefing-20261016.json", "早报")

    api = FakeApi()
    wxbot._cmd_send_locked(api, {}, only=None)

    for fn in ("111.json", "flash-20261016.json", "briefing-20261016.json"):
        assert os.path.exists(os.path.join(dirs["sent"], fn)), fn
    assert sorted(api.text_calls) == sorted(["reply-one", "早报", "⚡ 快讯：X"])


def test_stale_ack_of_finished_id_never_sent_late():
    """同单 ack 滞留时：本单回复已发出（sent/ 有记录）→ 下次全量 send
    判 ack 过期直接归档，绝不迟发一条"正在想"。
    （finish 不用管 ack，_is_stale_ack 在全量 send 时兜底。）"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    mid = "777"
    write_job(dirs["outbox"], mid + ".json", "reply")
    api = FakeApi()
    wxbot._cmd_send_locked(api, {}, only=mid + ".json")
    assert os.path.exists(os.path.join(dirs["sent"], mid + ".json"))

    # ack_timer 在 worker 慢时写下的同单 ack，现在才轮到全量 send
    write_job(dirs["outbox"], "ack_%s.json" % mid, "收到你的消息啦")
    api2 = FakeApi()
    wxbot._cmd_send_locked(api2, {}, only=None)

    assert os.path.exists(os.path.join(dirs["sent"], "ack_%s.json" % mid))
    assert api2.text_calls == [], "过期 ack 必须归档不发，绝不迟发"
