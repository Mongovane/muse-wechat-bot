"""任务级发送重试回归测试：2026-10-08 修正——ret=-2 是 ilink 配额耗尽的
确定性拒绝（约 10 条/用户入站），fail fast 不重试；只有请求异常（断连/超时）
才退避重试。永久性失败（ret=-3、文件缺失）不重试。
"""
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
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
        self.omit_calls = []

    def send_text(self, to, txt, ctx):
        self.text_calls.append(txt)
        nxt = self.script.pop(0) if self.script else {"ret": 0}
        if nxt is None:
            raise ConnectionError("boom")
        r = dict(nxt)
        r.setdefault("message_id", "mid-%d" % len(self.text_calls))
        return r

    def _post(self, endpoint, body, retries=0):
        # omit-token fallback 走这里：验证 context_token 字段确实被省略
        assert "context_token" not in body.get("msg", {}), \
            "omit-token 请求不应包含 context_token 字段"
        self.omit_calls.append(body["msg"]["item_list"][0]["text_item"]["text"])
        nxt = self.script.pop(0) if self.script else {"ret": 0}
        r = dict(nxt)
        r.setdefault("message_id", "mid-omit-%d" % len(self.omit_calls))
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
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    # 全部可写路径指向 tmp：测试绝不碰生产文件（2026-10-16 教训见
    # test_send_idempotent.py）。
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    wxbot.time.sleep = lambda s: None  # 跳过退避等待
    return tmp, outbox, sent


def write_job(outbox, name, text="hello"):
    with open(os.path.join(outbox, name), "w") as f:
        json.dump({"to": "u1", "context_token": "ctx", "text": text}, f)


def run_send(wxbot, api):
    wxbot._cmd_send_locked(api, {})


def test_transient_then_success():
    """ret=-2 后试 omit-token fallback：若 fallback 成功则发出。

    2026-10-08：hermes-agent 招数，对 token 过期类 -2 可能救回。
    2026-10-08 简化：单次尝试，不再 3s 睡眠重试。
    """
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "a.json")
    # 第一次 -2（走 send_text），第二次 omit-token 成功
    api = FakeApi([{"ret": -2}, {"ret": 0}])
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(sent, "a.json")), "omit-token 救回后应发出"
    assert api.omit_calls == ["hello"], "应尝试一次 omit-token"
    assert len(api.text_calls) == 1, "只试一次 send_text"
    assert wxbot._load_quota() == 1, "成功发送计入配额"


def test_omit_token_fail_still_gives_up():
    """omit-token 也 -2：fail fast，文件保留等下次入站。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "a2.json")
    api = FakeApi([{"ret": -2}] * 10)
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(outbox, "a2.json")), "应保留待下次入站"
    assert not os.path.exists(os.path.join(sent, "a2.json"))
    assert len(api.omit_calls) == 1, "只试一次 omit-token"
    assert len(api.text_calls) == 1, "只试一次 send_text"


def test_permanent_fail_no_retry():
    """ret=-3 参数错误是永久性失败：单次尝试，不重试，不试 omit-token。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "b.json")
    api = FakeApi([{"ret": -3}] * 10)
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(outbox, "b.json")), "文件应保留待人工处理"
    assert not os.path.exists(os.path.join(sent, "b.json"))
    assert len(api.text_calls) == 1, api.text_calls
    assert len(api.omit_calls) == 0, "非 -2 不试 omit-token"


def test_persistent_transient_gives_up():
    """一直 ret=-2（含 omit-token 也失败）：fail fast，文件保留给下次入站。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "c.json")
    api = FakeApi([{"ret": -2}] * 100)
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(outbox, "c.json"))
    assert not os.path.exists(os.path.join(sent, "c.json"))
    # 1 次 send_text + 1 次 omit-token，无任务级重试
    assert len(api.text_calls) == 1, api.text_calls
    assert len(api.omit_calls) == 1


def test_exception_is_transient():
    """请求抛异常视为瞬时故障，可重试并恢复。"""
    wxbot = load_wxbot()
    _, outbox, sent = make_env(wxbot)
    write_job(outbox, "d.json")
    api = FakeApi([None, None, {"ret": 0}])
    run_send(wxbot, api)
    assert os.path.exists(os.path.join(sent, "d.json"))
