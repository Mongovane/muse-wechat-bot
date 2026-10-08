"""任务级发送重试回归测试：瞬时故障（ret=-2 / 请求异常）退避重试，
永久性失败（ret=-3、文件缺失）不重试。

2026-10-08 Peter 要求：wxbot.py send 失败时自动重新拉起发送，
不再只靠 3 分钟兜底 cron 轮询。
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
    """按 script 逐次返回：元素为 dict=响应，None=抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.text_calls = []

    def send_text(self, to, txt, ctx):
        self.text_calls.append(txt)
        nxt = self.script.pop(0) if self.script else {"ret": 0}
        if nxt is None:
            raise ConnectionError("boom")
        r = dict(nxt)
        r.setdefault("message_id", "mid-%d" % len(self.text_calls))
        return r


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="retrytest-")
    outbox = os.path.join(tmp, "outbox")
    sent = os.path.join(tmp, "sent")
    hist = os.path.join(tmp, "history")
    for d in (outbox, sent, hist):
        os.makedirs(d)
    wxbot.OUTBOX = outbox
    wxbot.SENT = sent
    wxbot.HISTORY_DIR = hist
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    wxbot.time.sleep = lambda s: None  # 跳过退避等待
    return tmp, outbox, sent


def write_job(outbox, name, text="hello"):
    with open(os.path.join(outbox, name), "w") as f:
        json.dump({"to": "u1", "context_token": "ctx", "text": text}, f)


def run_send(wxbot, api):
    wxbot._cmd_send_locked(api, {})


def test_transient_then_success():
    """ret=-2 两次耗尽单分片内部重试 → 任务级重试后成功，只送达一次。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "a.json")
    api = FakeApi([{"ret": -2}, {"ret": -2}, {"ret": 0}])
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(sent, "a.json")), "应已发出并归档"
    assert not os.path.exists(os.path.join(outbox, "a.json"))
    # 分片账本保证 exactly-once：成功的那次只发了一遍
    assert api.text_calls == ["hello", "hello", "hello"]


def test_permanent_fail_no_retry():
    """ret=-3 参数错误是永久性失败：只走单分片内部重试，不触发任务级重试。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "b.json")
    api = FakeApi([{"ret": -3}] * 10)
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(outbox, "b.json")), "文件应保留待人工处理"
    assert not os.path.exists(os.path.join(sent, "b.json"))
    # 2 = send_text_retry 内部的两次尝试，无任务级重试
    assert len(api.text_calls) == 2, api.text_calls


def test_persistent_transient_gives_up():
    """一直 ret=-2：3 次任务级尝试后停手，文件保留给兜底 cron。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "c.json")
    api = FakeApi([{"ret": -2}] * 100)
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(outbox, "c.json"))
    assert not os.path.exists(os.path.join(sent, "c.json"))
    # 3 次任务尝试 × 每次单分片内部 2 次 = 6
    assert len(api.text_calls) == 6, api.text_calls


def test_exception_is_transient():
    """请求抛异常视为瞬时故障，可重试并恢复。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "d.json")
    api = FakeApi([None, None, {"ret": 0}])
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(sent, "d.json"))
