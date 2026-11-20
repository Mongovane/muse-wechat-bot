"""Outbox watcher tests (2026-11-20, 发送提速).

watch-outbox 常驻进程盯 outbox/，普通回复文件一出现就代发，worker
不用再调 finish（省一次模型动作 ~3.5s）。reply_to_sent 从 ~5 秒
降到 ~1.5-2 秒（1.2 秒接口 + 0.2 秒检测 + 0.35 秒收尾）。

安全契约：
- 只发纯数字文件名的普通回复，且 processing/<id>.json 必须在；
  proactive（flash-/briefing-/market-/digest-）绝不代发。
- 失败（配额 -2）fail-fast：文件留 outbox、processing 留原地，
  退避期内不重复尝试（防配额墙下空转），绝不丢单、不重发。
- 发送复用 _finish_one（与 cmd_finish 同一套逻辑：账本 client_id、
  parts 账本、reply_map 去重、latency 打点），只是不做 FINISH_RECHECK。
Tests only; never touches production files.
"""
import contextlib
import importlib.util
import json
import os
import tempfile
import time

_real_sleep = time.sleep


@contextlib.contextmanager
def _real_time_sleep():
    """让 time.sleep 在本块内是真的（同 test_hybrid_warm_wait.py 的教训：
    别的测试文件把 wxbot.time.sleep 改成 no-op 且不恢复，量真实耗时
    前必须先恢复）。"""
    import time as _time
    prev = _time.sleep
    _time.sleep = _real_sleep
    try:
        yield
    finally:
        _time.sleep = prev

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot(tmp):
    spec = importlib.util.spec_from_file_location(
        "wxbot_watch_outbox_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("INBOX", "OUTBOX", "SENT", "DONE", "PROCESSING", "MEDIA"):
        p = os.path.join(tmp, name.lower())
        os.makedirs(p, exist_ok=True)
        setattr(mod, name, p)
    mod.SEND_LOCK = os.path.join(tmp, "send.lock")
    mod.LATENCY_LOG = os.path.join(tmp, "wechat-latency.jsonl")
    mod.HISTORY_DIR = os.path.join(tmp, "history")
    mod.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    mod.QUOTA_FILE = os.path.join(tmp, "quota.json")
    mod.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    mod.REPLY_MAP = os.path.join(tmp, "reply_map.json")
    mod.TYPING_TICKETS_FILE = os.path.join(tmp, "typing_tickets.json")
    mod.SEND_LEDGER = os.path.join(tmp, "send_ledger.json")
    mod.SEND_LEDGER_LOCK = os.path.join(tmp, "send_ledger.lock")
    mod.SEND_AUDIT = os.path.join(tmp, "send_audit.jsonl")
    mod.QUOTE_CACHE_FILE = os.path.join(tmp, "quote_cache.json")
    mod.TOKEN_STALE_FILE = os.path.join(tmp, "token_stale.json")
    mod.WATCH_OUTBOX_FILE = os.path.join(tmp, "watch-outbox.json")
    os.makedirs(mod.HISTORY_DIR, exist_ok=True)
    return mod


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False)


def write_reply_atomically(wxbot, mid, text="hello"):
    """按 worker 协议原子发布回复：先写 .tmp 再 mv（文件一出现就是完整的）。"""
    tmp_path = os.path.join(wxbot.OUTBOX, mid + ".json.tmp")
    write_json(tmp_path, {"to": "u@im.wechat", "context_token": "c",
                          "text": text})
    os.replace(tmp_path, os.path.join(wxbot.OUTBOX, mid + ".json"))


class FakeApiOk:
    """成功路径：send_text 返回 ret 0；_post 处理 getconfig/sendtyping。"""

    def __init__(self):
        self.text_calls = []
        self.post_calls = []

    def send_text(self, to, txt, ctx, client_id=None):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "mid-text-%d" % len(self.text_calls)}

    def _post(self, endpoint, body, retries=0):
        self.post_calls.append(endpoint)
        if "getconfig" in endpoint:
            return {"ret": 0, "typing_ticket": "fake-ticket"}
        return {"ret": 0}


class FakeApiQuota:
    """配额墙：send_text 与 omit-token _post 都返回 ret=-2。"""

    def __init__(self):
        self.text_calls = []
        self.post_calls = []

    def send_text(self, to, txt, ctx, client_id=None):
        self.text_calls.append(txt)
        return {"ret": -2, "errmsg": "prepare failed"}

    def _post(self, endpoint, body, retries=0):
        self.post_calls.append(endpoint)
        if "getconfig" in endpoint:
            return {"ret": 0, "typing_ticket": "fake-ticket"}
        return {"ret": -2, "errmsg": "prepare failed"}


def _fake_cmd_send(wxbot, api):
    """用真实 _cmd_send_locked + FakeApi 替换 cmd_send（跳过真实网络
    与 load_creds，但保留账本/parts/去重等全部发送逻辑）。"""
    def fake_send(only=None):
        wxbot._cmd_send_locked(api, {}, only=only)
    return fake_send


def _setup_reply(wxbot, mid, claim_ts=1000):
    write_json(os.path.join(wxbot.PROCESSING, mid + ".json"),
               {"from": "u@im.wechat", "text": "hi", "claim_ts": claim_ts})
    write_reply_atomically(wxbot, mid)


def test_watcher_sends_reply_fast_path():
    """快路：worker 原子写好回复 → watcher 一次扫描就发出。
    文件 outbox→sent、processing→done、latency 有记录且 reply_to_sent 很小。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    mid = "1234567890123456789"
    _setup_reply(wxbot, mid, claim_ts=1000)
    api = FakeApiOk()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        attempts, stats = {}, {"sent": 0, "failed": 0}
        sent, failed = wxbot._watch_outbox_scan(attempts, 60, stats)
    finally:
        wxbot.cmd_send = real_cmd_send
    assert (sent, failed) == (1, 0)
    assert api.text_calls == ["hello"], "只发一次，不重发"
    assert not os.path.exists(os.path.join(wxbot.OUTBOX, mid + ".json"))
    assert os.path.exists(os.path.join(wxbot.SENT, mid + ".json"))
    assert os.path.exists(os.path.join(wxbot.DONE, mid + ".json"))
    assert not os.path.exists(os.path.join(wxbot.PROCESSING, mid + ".json"))
    lines = open(wxbot.LATENCY_LOG).read().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["msg_id"] == mid
    # 模拟 API 瞬时返回，reply_to_sent 应远小于旧 finish 的 ~5 秒
    assert 0 <= entry["reply_to_sent_s"] < 2, entry
    # 心跳文件已写，供 hook §1h 判活
    hb = json.load(open(wxbot.WATCH_OUTBOX_FILE))
    assert hb["sent"] == 1 and hb["failed"] == 0
    assert hb["pid"] == os.getpid()


def test_watcher_quota_fail_fast_and_backoff():
    """配额 -2：fail-fast 留文件、processing 留原地；退避期内不再尝试；
    退避过后（或文件被重写）才再试，且每次尝试都只发一次、不重发。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    mid = "1234567890123456790"
    _setup_reply(wxbot, mid, claim_ts=1000)
    api = FakeApiQuota()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        attempts, stats = {}, {"sent": 0, "failed": 0}
        sent, failed = wxbot._watch_outbox_scan(attempts, 60, stats)
        assert (sent, failed) == (0, 1)
        # -2 试了一次正文 + 一次 omit-token，之后 fail-fast，不再重试
        assert len(api.text_calls) == 1, api.text_calls
        assert len(api.post_calls) == 1, api.post_calls
        # 文件原样保留给兜底（§1c / sweeper），绝不丢单
        assert os.path.exists(os.path.join(wxbot.OUTBOX, mid + ".json"))
        assert os.path.exists(os.path.join(wxbot.PROCESSING, mid + ".json"))
        assert not os.path.exists(os.path.join(wxbot.SENT, mid + ".json"))
        # 立刻再扫：退避期内不重复尝试（API 调用数不变）
        sent2, failed2 = wxbot._watch_outbox_scan(attempts, 60, stats)
        assert (sent2, failed2) == (0, 0)
        assert len(api.text_calls) == 1
        assert len(api.post_calls) == 1
        # 退避过后（retry_secs=0 强制再试）：再试一次，仍失败、仍不丢单
        sent3, failed3 = wxbot._watch_outbox_scan(attempts, 0, stats)
        assert (sent3, failed3) == (0, 1)
        assert len(api.text_calls) == 2, "退避后恰好再试一次"
        assert os.path.exists(os.path.join(wxbot.OUTBOX, mid + ".json"))
    finally:
        wxbot.cmd_send = real_cmd_send


def test_watcher_ignores_proactive_and_orphans():
    """watcher 只发 processing 在的纯数字普通回复；proactive 与
    无 processing 的孤儿一律不动（留给显式 send / 兜底）。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    api = FakeApiOk()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        # proactive 文件（即使 processing 在也不代发）
        write_json(os.path.join(wxbot.PROCESSING, "flash-20261120.json"),
                   {"from": "u", "text": "x", "claim_ts": 1000})
        write_json(os.path.join(wxbot.OUTBOX, "flash-20261120.json"),
                   {"to": "u@im.wechat", "text": "快讯"})
        # 数字回复但无 processing（孤儿）
        write_json(os.path.join(wxbot.OUTBOX, "1234567890123456791.json"),
                   {"to": "u@im.wechat", "text": "孤儿"})
        # .parts.json 账本文件也不是任务
        write_json(os.path.join(wxbot.OUTBOX, "1234567890123456792.json.parts.json"),
                   ["text:0"])
        attempts, stats = {}, {"sent": 0, "failed": 0}
        sent, failed = wxbot._watch_outbox_scan(attempts, 60, stats)
    finally:
        wxbot.cmd_send = real_cmd_send
    assert (sent, failed) == (0, 0)
    assert api.text_calls == [], "一个都不该发"
    assert os.path.exists(os.path.join(wxbot.OUTBOX, "flash-20261120.json"))
    assert os.path.exists(os.path.join(wxbot.OUTBOX,
                                       "1234567890123456791.json"))


def test_watcher_rewrites_retry_immediately():
    """文件被 worker 重写（mtime 变）→ 不等退避，立即再试。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    mid = "1234567890123456793"
    _setup_reply(wxbot, mid, claim_ts=1000)
    api = FakeApiQuota()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        attempts, stats = {}, {"sent": 0, "failed": 0}
        wxbot._watch_outbox_scan(attempts, 3600, stats)
        assert len(api.text_calls) == 1
        # worker 重写回复（新 mtime）：立即再试，不用等 3600 秒退避
        time.sleep(0.05)
        write_reply_atomically(wxbot, mid, text="hello v2")
        sent, failed = wxbot._watch_outbox_scan(attempts, 3600, stats)
        assert (sent, failed) == (0, 1)
        assert len(api.text_calls) == 2
        assert api.text_calls[-1] == "hello v2"
    finally:
        wxbot.cmd_send = real_cmd_send


def test_watch_outbox_loop_bounded_run():
    """loop 本身：max_secs 到点退出并打印计数；跑动期间写好的回复会被发出。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    mid = "1234567890123456794"
    _setup_reply(wxbot, mid, claim_ts=1000)
    api = FakeApiOk()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        with _real_time_sleep():
            rc = wxbot.cmd_watch_outbox(poll_secs=0.05, retry_secs=60,
                                        max_secs=0.5)
    finally:
        wxbot.cmd_send = real_cmd_send
    assert rc == 0
    assert api.text_calls == ["hello"]
    assert os.path.exists(os.path.join(wxbot.SENT, mid + ".json"))
    hb = json.load(open(wxbot.WATCH_OUTBOX_FILE))
    assert hb["sent"] == 1


def test_parse_watch_outbox_args():
    """参数解析：`=` 与空格两种写法都接受，非法值回落默认值。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    assert wxbot._parse_watch_outbox_args([]) == (0.2, 60, None)
    assert wxbot._parse_watch_outbox_args(["--poll-secs=0.5"]) == (0.5, 60, None)
    assert wxbot._parse_watch_outbox_args(["--poll-secs", "0.5",
                                           "--retry-secs", "30",
                                           "--max-secs", "10"]) == (0.5, 30, 10)
    assert wxbot._parse_watch_outbox_args(["--poll-secs", "abc"]) == (0.2, 60, None)
    assert wxbot._parse_watch_outbox_args(["--poll-secs", "-1"]) == (0.2, 60, None)


def test_prompt_instructs_watch_outbox_not_finish():
    """提示词回归（2026-11-20 watch-outbox）：step 5 必须写明回复写好后
    由常驻 watch-outbox 代发、worker 不用再调 finish；step 7 的"回完"
    锚点必须是"回复写好后"而不是"finish 成功后"（worker 不再调 finish）。
    只断言仓库 md——线上 JSON 由部署时同步更新（本测试不碰线上文件）。"""
    md = None
    for cand in (os.path.join(BASE, "..", "prompts", "hook-worker.md"),
                 "/home/hatch/workspace/muse-wechat-bot/bot/prompts/hook-worker.md"):
        if os.path.exists(cand):
            md = open(cand).read()
            break
    if md is None:
        import pytest
        pytest.skip("hook-worker.md not found in either location")
    assert "watch-outbox" in md, "hook-worker.md step 5 must say watch-outbox sends it"
    assert "不用再调 finish" in md, \
        "hook-worker.md step 5 must tell the worker not to call finish"
    assert "本轮回复写好后" in md, \
        "hook-worker.md step 7 must anchor on reply-written, not finish"
    assert "本轮 finish 成功后" not in md, \
        "hook-worker.md step 7 must not say 'finish 成功后' any more"


def test_finish_one_without_recheck_sends_stale_reply():
    """_finish_one(do_recheck=False) 不合并追问、直接发（watcher 语义）；
    而 cmd_finish（do_recheck=True）会合并并返回 2。两者共用同一套发送逻辑。"""
    tmp = tempfile.mkdtemp(prefix="watchtest-")
    wxbot = load_wxbot(tmp)
    mid = "1234567890123456795"
    _setup_reply(wxbot, mid, claim_ts=1000)
    # 同发信人追问还在 inbox 里
    write_json(os.path.join(wxbot.INBOX, "9999999999999999999.json"),
               {"from": "u@im.wechat", "text": "还有呢"})
    api = FakeApiOk()
    real_cmd_send = wxbot.cmd_send
    wxbot.cmd_send = _fake_cmd_send(wxbot, api)
    try:
        rc = wxbot._finish_one(mid, do_recheck=False)
    finally:
        wxbot.cmd_send = real_cmd_send
    assert rc == 0, "watcher 不复查，直接发"
    assert api.text_calls == ["hello"]
    assert os.path.exists(os.path.join(wxbot.DONE, mid + ".json"))
