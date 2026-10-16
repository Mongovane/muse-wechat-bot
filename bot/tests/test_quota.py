"""ilink 回复配额账本回归测试（2026-10-08）。

背景：全网调研确认腾讯 ilink 有"约 10 条/用户入站"的 bot 回复配额，
超限服务端确定性返回 ret=-2 prepare failed（Thysrael/Horizon PR#159、
clawock、腾讯维护者亲口确认）。wxbot.py 实现本地账本：
- 用户入站 → 配额清零
- 每次 sendmessage 成功 → +1
- proactive（flash/briefing/market/ack）在配额>=8 时排队，ack 在 >=5 时跳过
- ret=-2 → fail fast 不重试
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.send_calls = []

    def send_text(self, to, text, ctx):
        self.send_calls.append(text)
        if self.script:
            r = self.script.pop(0)
            r = dict(r)
            r.setdefault("message_id", "mid-%d" % len(self.send_calls))
            return r
        return {"ret": 0, "message_id": "mid-%d" % len(self.send_calls)}


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="quotatest-")
    outbox = os.path.join(tmp, "outbox")
    sent = os.path.join(tmp, "sent")
    os.makedirs(outbox)
    os.makedirs(sent)
    wxbot.OUTBOX = outbox
    wxbot.SENT = sent
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.SEND_LOCK = os.path.join(tmp, "send.lock")
    # flash 发送成功会记共享去重池：指向 tmp，绝不写真实生产池
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    # 全部可写路径指向 tmp：测试绝不碰生产文件（2026-10-16 教训见
    # test_send_idempotent.py）。
    wxbot.HISTORY_DIR = os.path.join(tmp, "history")
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    os.makedirs(wxbot.HISTORY_DIR, exist_ok=True)
    return tmp, outbox, sent


def write_job(outbox, name, text="hi"):
    with open(os.path.join(outbox, name), "w") as f:
        json.dump({"to": "u1", "context_token": "", "text": text}, f)


def test_quota_bump_on_success():
    """成功发送一次，配额 +1。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "12345.json")  # 数字文件名 = 用户回复
    wxbot._cmd_send_locked(FakeApi(), {})
    assert wxbot._load_quota() == 1
    assert os.path.exists(os.path.join(sent, "12345.json"))


def test_quota_reset():
    """_quota_reset 清零。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot._save_quota(7)
    wxbot._quota_reset()
    assert wxbot._load_quota() == 0


def test_proactive_always_tries():
    """2026-10-08 修正：Peter 要求"有快讯就推"，proactive 不做本地配额预判，
    直接发，撞 -2 再 fail fast。本地账本只用于监控和 ack 节流。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "flash-20261008-test.json", "news")
    wxbot._save_quota(14)  # 配额很高也照发
    api = FakeApi()
    wxbot._cmd_send_locked(api, {})
    assert api.send_calls == ["news"], "proactive 应直接尝试发送"
    assert os.path.exists(os.path.join(sent, "flash-20261008-test.json"))
    assert wxbot._load_quota() == 15


def test_ack_skipped_when_quota_mid():
    """配额>=5 时 ack 延迟提示跳过（省配额），文件归档不重发。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "ack_123.json", "ack text")
    wxbot._save_quota(5)
    api = FakeApi()
    wxbot._cmd_send_locked(api, {})
    assert api.send_calls == [], "ack 应被跳过"
    assert os.path.exists(os.path.join(sent, "ack_123.json")), "ack 归档"
    assert wxbot._load_quota() == 5, "未发送则配额不变"


def test_reply_always_sent():
    """用户回复（数字文件名）不受配额门控，始终发送。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "99999.json", "reply")
    wxbot._save_quota(9)  # 配额几乎用完
    api = FakeApi()
    wxbot._cmd_send_locked(api, {})
    assert api.send_calls == ["reply"], "回复必须发出"
    assert wxbot._load_quota() == 10


def test_is_proactive_classification():
    """文件名分类：proactive vs 回复。"""
    wxbot = load_wxbot()
    for fn in ("ack_1.json", "flash-20261008-1.json", "briefing-20261008.json",
               "market-pre-20261008.json", "digest-x.json"):
        assert wxbot._is_proactive(fn), fn
    for fn in ("12345.json", "7513786869511980168.json"):
        assert not wxbot._is_proactive(fn), fn
