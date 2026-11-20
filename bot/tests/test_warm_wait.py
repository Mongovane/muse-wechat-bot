"""Warm-wait tests (2026-11-19, resident 2026-11-20).

Covers the frozen cmd_warm_wait contract in wxbot.py: with an empty inbox
it waits out idle_secs and prints IDLE_TIMEOUT; with a pre-seeded inbox
file present at start it self-claims (2026-11-19: no more NEW_MESSAGES
+ separate claim round-trip — that ~1s handoff is what lost the second
17:54 message to the hook's 5s poll) and prints the claim payload JSON.
Resident mode (--resident, 2026-11-20): no IDLE_TIMEOUT inside the window,
24h cap + rotation after 100 claims (payload "rotate": true, marker cleared,
counter resets on the next fresh session); marker persists across claims so
the heartbeat stays fresh for the hook guard's 10s grace window.
Real timing is used here (no sleep stubbing) so the idle/cap paths are
genuinely exercised. Tests only; never touches production files.
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time

import pytest

# 其他测试文件会在 make_env 里把 time.sleep 桩成空操作（全局生效）；
# 这里在 import 时就抓一份真 sleep，供 _stub_calls 的等待循环用。
_real_sleep = time.sleep

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    """Fresh tmp env; EVERY writable module path points at tmp."""
    tmp = tempfile.mkdtemp(prefix="warmtest-")
    dirs = {}
    for name in ("outbox", "sent", "history", "inbox", "done",
                 "processing", "media"):
        p = os.path.join(tmp, name)
        os.makedirs(p)
        dirs[name] = p
    wxbot.OUTBOX = dirs["outbox"]
    wxbot.SENT = dirs["sent"]
    wxbot.HISTORY_DIR = dirs["history"]
    wxbot.INBOX = dirs["inbox"]
    wxbot.DONE = dirs["done"]
    wxbot.PROCESSING = dirs["processing"]
    wxbot.MEDIA = dirs["media"]
    wxbot.BUF = os.path.join(tmp, "buf.txt")
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    wxbot.SEND_LOCK = os.path.join(tmp, "send.lock")
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    wxbot.REPLY_MAP = os.path.join(tmp, "reply_map.json")
    wxbot.TYPING_TICKETS_FILE = os.path.join(tmp, "typing_tickets.json")
    wxbot.LATENCY_LOG = os.path.join(tmp, "latency.jsonl")
    wxbot.OWNER_FILE = os.path.join(tmp, "owner.json")
    wxbot.UNKNOWN_FILE = os.path.join(tmp, "unknown_senders.json")
    wxbot.BIND_FILE = os.path.join(tmp, "bind_state.json")
    wxbot.SEND_LEDGER = os.path.join(tmp, "send_ledger.json")
    wxbot.SEND_LEDGER_LOCK = os.path.join(tmp, "send_ledger.lock")
    wxbot.SEND_AUDIT = os.path.join(tmp, "send_audit.jsonl")
    wxbot.QUOTE_CACHE_FILE = os.path.join(tmp, "quote_cache.json")
    wxbot.TOKEN_STALE_FILE = os.path.join(tmp, "token_stale.json")
    wxbot.ALERT_STATE_FILE = os.path.join(tmp, "alert_state.json")
    wxbot.WATCHDOG_STATE_FILE = os.path.join(tmp, "watchdog_state.json")
    wxbot.WATCHDOG_OFF_FILE = os.path.join(tmp, "watchdog.off")
    wxbot.WARM_FILE = os.path.join(tmp, "warm.json")
    wxbot.WARM_RESIDENT_FILE = os.path.join(tmp, "warm-resident.json")
    wxbot.WARM_REQUEST_FILE = os.path.join(tmp, "warm-request.json")
    wxbot.WARM_CLAIM_PREFIX = os.path.join(tmp, "warm-claim-")
    # claim 专用：EVOLVE 账本 + ack/watchdog 脚本全部指向 tmp 桩脚本，
    # 绝不调真实 ack_timer.sh / watchdog.sh（会发 typing、会回收单子）。
    wxbot.EVOLVE_FILE = os.path.join(tmp, "EVOLVE.md")
    for name in ("ack_timer.sh", "watchdog.sh"):
        stub = os.path.join(tmp, name)
        with open(stub, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    "echo \"$@\" >> %s\n" % os.path.join(tmp, name + ".log"))
        os.chmod(stub, 0o755)
    wxbot.ACK_TIMER_SCRIPT = os.path.join(tmp, "ack_timer.sh")
    wxbot.WATCHDOG_SCRIPT = os.path.join(tmp, "watchdog.sh")
    return tmp, dirs


def capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def test_parse_warm_wait_args_both_forms():
    """CLI 只认 `--cap-secs=900` 时，worker 按自然写法传 `--cap-secs 900`
    会被静默忽略、跑满默认 1800 秒（2026-11-19 实测：live 进程 until
    = start+1800）。两种写法都要收；非法值回落默认。常驻：--resident
    默认 cap 86400、max-claims 100；显式 --cap-secs 优先。"""
    wxbot = load_wxbot()
    assert wxbot._parse_warm_wait_args([]) == (180, 1800, False, 100)
    assert wxbot._parse_warm_wait_args(["--idle-secs=60", "--cap-secs=900"]) == (60, 900, False, 100)
    assert wxbot._parse_warm_wait_args(["--idle-secs", "60", "--cap-secs", "900"]) == (60, 900, False, 100)
    assert wxbot._parse_warm_wait_args(["--cap-secs", "abc"]) == (180, 1800, False, 100)
    assert wxbot._parse_warm_wait_args(["--cap-secs", "-5"]) == (180, 1800, False, 100)
    assert wxbot._parse_warm_wait_args(["--resident"]) == (180, 86400, True, 100)
    assert wxbot._parse_warm_wait_args(["--resident", "--cap-secs", "3600"]) == (180, 3600, True, 100)
    assert wxbot._parse_warm_wait_args(["--resident", "--max-claims=5"]) == (180, 86400, True, 5)
    assert wxbot._parse_warm_wait_args(["--resident", "--max-claims", "0"]) == (180, 86400, True, 100)
    assert wxbot._parse_warm_wait_args(["--resident=false"]) == (180, 1800, False, 100)


def test_warm_wait_idle_timeout_empty_inbox():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    assert os.listdir(dirs["inbox"]) == []
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert isinstance(rc, int)
    assert "IDLE_TIMEOUT" in out


def test_warm_wait_new_messages_preseeded():
    """自认领（2026-11-19）：inbox 预置文件时 warm-wait 直接认领并打印
    claim payload JSON，不再只报 NEW_MESSAGES 等 worker 另起 claim 进程。
    文件 inbox→processing，打 claim_ts，起 ack/watchdog。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    with open(os.path.join(dirs["inbox"], "999.json"), "w") as f:
        json.dump({"from": "u1", "text": "follow-up", "msg_id": "999"}, f)
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert rc == 0, out
    assert "NEW_MESSAGES" not in out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["999"], payload
    assert payload["messages"]["999"]["text"] == "follow-up"
    assert not os.listdir(dirs["inbox"])
    proc = json.load(open(os.path.join(dirs["processing"], "999.json")))
    assert proc["claim_ts"] == payload["messages"]["999"]["claim_ts"]
    assert _stub_calls(tmp, "ack_timer.sh")[0].split()[0] == "999"
    assert _stub_calls(tmp, "watchdog.sh")[0].split()[-1] == "999"


def test_warm_wait_claim_empty_on_corrupt_only():
    """自认领抢不到（只剩读坏文件）：warm-wait 返回 1 并打印 CLAIM_EMPTY，
    坏文件留在 inbox 原地、processing 为空，worker 安静退出。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    with open(os.path.join(dirs["inbox"], "BAD.json"), "w") as f:
        f.write("{not json")
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert rc == 1
    assert "CLAIM_EMPTY" in out
    assert os.path.exists(os.path.join(dirs["inbox"], "BAD.json"))
    assert not os.listdir(dirs["processing"])


def _seed_inbox(dirs, mid, text, sender="u1", image="", mtime=None):
    p = os.path.join(dirs["inbox"], mid + ".json")
    with open(p, "w") as f:
        json.dump({"from": sender, "text": text, "msg_id": mid,
                   "context_token": "ctx-" + mid, "image": image}, f)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _stub_calls(tmp, name):
    """桩脚本是 Popen 异步起的：读日志前最多等 2 秒，防时序抖动误报。
    必须用 import 时抓的真 sleep——time.sleep 可能已被别的测试文件桩掉。"""
    p = os.path.join(tmp, name + ".log")
    for _ in range(20):
        if os.path.exists(p):
            with open(p) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                return lines
        _real_sleep(0.1)
    return []


def test_claim_moves_inbox_to_processing_and_prints_payload():
    """claim：inbox→processing 原子认领，打 claim_ts，起 ack/watchdog，
    输出与 hook wake payload 同形的 JSON（skill_hint + histories + evolve）。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    with open(os.path.join(dirs["history"], "u1.jsonl"), "w") as f:
        f.write(json.dumps({"role": "user", "text": "上文原话"}, ensure_ascii=False) + "\n")
    with open(wxbot.EVOLVE_FILE, "w") as f:
        f.write("# EVOLVE\n- 教训一\n- 教训二\n")
    _seed_inbox(dirs, "W1", "深圳天气")
    rc, out = capture(wxbot.cmd_claim)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["W1"], payload
    assert payload["messages"]["W1"]["skill_hint"]["type"] == "weather"
    assert payload["messages"]["W1"]["skill_hint"]["city"] == "深圳"
    assert payload["messages"]["W1"]["claim_ts"] > 0
    assert payload["histories"]["u1"][0]["text"] == "上文原话"
    assert payload["evolve_tail"] == ["# EVOLVE", "- 教训一", "- 教训二"]
    assert not os.listdir(dirs["inbox"])
    proc = json.load(open(os.path.join(dirs["processing"], "W1.json")))
    assert proc["claim_ts"] == payload["messages"]["W1"]["claim_ts"]
    assert _stub_calls(tmp, "ack_timer.sh")[0].split()[0] == "W1"
    assert _stub_calls(tmp, "watchdog.sh")[0].split()[-1] == "W1"


def test_claim_merges_same_sender_burst():
    """同用户 60 秒内连发：后一条为 lead、文本拼接、图片继承，前一条归档 done/。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    now = time.time()
    _seed_inbox(dirs, "M1", "先看这张图", image="/tmp/pic.jpg", mtime=now - 10)
    _seed_inbox(dirs, "M2", "图里是什么", mtime=now)
    rc, out = capture(wxbot.cmd_claim)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["M2"], payload
    assert payload["messages"]["M2"]["text"] == "先看这张图\n图里是什么"
    assert payload["messages"]["M2"]["image"] == "/tmp/pic.jpg"
    assert os.path.exists(os.path.join(dirs["done"], "M1.json"))
    assert not os.path.exists(os.path.join(dirs["processing"], "M1.json"))


def test_claim_empty_inbox():
    """inbox 为空：打印 CLAIM_EMPTY，返回 1（worker 安静退出，不重复回）。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    rc, out = capture(wxbot.cmd_claim)
    assert rc == 1
    assert "CLAIM_EMPTY" in out


def test_claim_skips_corrupt_json_in_place():
    """读坏的 inbox 文件留在原地不动（hook 下一轮处理），不往 processing 留孤儿。"""
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    with open(os.path.join(dirs["inbox"], "BAD.json"), "w") as f:
        f.write("{not json")
    rc, out = capture(wxbot.cmd_claim)
    assert rc == 1
    assert "CLAIM_EMPTY" in out
    assert os.path.exists(os.path.join(dirs["inbox"], "BAD.json"))
    assert not os.listdir(dirs["processing"])


def test_resident_no_idle_timeout_inside_window():
    """常驻：idle_secs 在窗口内不起作用——空 inbox 等满 cap 才报 TOTAL_CAP，
    绝不报 IDLE_TIMEOUT（idle=1、cap=3，3 秒后应为 TOTAL_CAP 而不是 1 秒后
    的 IDLE_TIMEOUT）。marker 随 TOTAL_CAP 清除。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=3,
                      resident=True, max_claims=100)
    assert rc == 0
    assert "IDLE_TIMEOUT" not in out
    assert "TOTAL_CAP" in out
    assert not os.path.exists(os.path.join(tmp, "warm.json"))


def _resident_claims(wxbot):
    return json.load(open(wxbot.WARM_RESIDENT_FILE))["claims"]


def test_resident_rotation_after_max_claims():
    """常驻轮换（max_claims=2，三轮调用模拟 worker 的三轮 warm-wait）：
    第 1 条认领无 rotate、marker 保留（心跳继续给 hook 守卫留宽限）；
    第 2 条认领带 rotate=true、marker 清除；第 3 条新会话重置计数、无 rotate。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "R1", "第一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, max_claims=2)
    assert rc == 0, out
    p1 = json.loads(out)
    assert p1["msg_ids"] == ["R1"] and "rotate" not in p1, p1
    assert os.path.exists(os.path.join(tmp, "warm.json"))
    assert _resident_claims(wxbot) == 1
    _seed_inbox(dirs, "R2", "第二条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, max_claims=2)
    assert rc == 0, out
    p2 = json.loads(out)
    assert p2["msg_ids"] == ["R2"] and p2.get("rotate") is True, p2
    assert not os.path.exists(os.path.join(tmp, "warm.json"))
    assert not os.path.exists(wxbot.WARM_RESIDENT_FILE)  # 轮换即重置账本
    _seed_inbox(dirs, "R3", "第三条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, max_claims=2)
    assert rc == 0, out
    p3 = json.loads(out)
    assert p3["msg_ids"] == ["R3"] and "rotate" not in p3, p3
    assert _resident_claims(wxbot) == 1


def test_nonresident_marker_cleared_on_claim():
    """对照：非驻留认领成功后 marker 清除（老行为）；常驻才保留。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "N1", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert rc == 0, out
    assert json.loads(out)["msg_ids"] == ["N1"]
    assert not os.path.exists(os.path.join(tmp, "warm.json"))


# --- 2026-11-20 第二轮：hook-owned warm-wait --------------------------------
# worker 不再自己跑 warm-wait（worker 会话结束时 runtime 清掉它名下的后台
# 进程，三轮实测 5 秒～4.5 分钟内死光）。worker 只写 warm-request.json，
# hook §1f supervisor 拉起 warm-wait；warm-wait 认领后把 payload 写一份
# warm-claim-<id>.json，hook 下一轮读它唤醒 worker。

def test_parse_request_warm_args():
    """request-warm 只认 --cap-secs（`=` 与空格两种写法），非法/非正回落
    86400；钳制在 cmd_request_warm 里做，不在解析器里。"""
    wxbot = load_wxbot()
    assert wxbot._parse_request_warm_args([]) == 86400
    assert wxbot._parse_request_warm_args(["--cap-secs=3600"]) == 3600
    assert wxbot._parse_request_warm_args(["--cap-secs", "3600"]) == 3600
    assert wxbot._parse_request_warm_args(["--cap-secs", "abc"]) == 86400
    assert wxbot._parse_request_warm_args(["--cap-secs", "-5"]) == 86400
    assert wxbot._parse_request_warm_args(["--cap-secs", "0"]) == 86400


def test_request_warm_writes_file():
    """cmd_request_warm 写 warm-request.json 并打印 WARM_REQUEST_OK。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    rc, out = capture(wxbot.cmd_request_warm, 3600)
    assert rc == 0
    assert out.strip() == "WARM_REQUEST_OK 3600"
    req = json.load(open(os.path.join(tmp, "warm-request.json")))
    assert req["cap_secs"] == 3600
    assert req["ts"] > 0


def test_request_warm_clamps():
    """cap 钳制到 1..86400；非法输入回落 86400 再钳制。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _, out = capture(wxbot.cmd_request_warm, 999999)
    assert out.strip() == "WARM_REQUEST_OK 86400"
    _, out = capture(wxbot.cmd_request_warm, 0)
    assert out.strip() == "WARM_REQUEST_OK 1"
    _, out = capture(wxbot.cmd_request_warm, -5)
    assert out.strip() == "WARM_REQUEST_OK 1"
    _, out = capture(wxbot.cmd_request_warm, "abc")
    assert out.strip() == "WARM_REQUEST_OK 86400"


def test_resident_claim_writes_claim_file():
    """常驻认领：除了打印 payload，还写 warm-claim-<首条 id>.json，内容与
    打印的 payload 一致；marker 保留（心跳继续）。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "C1", "第二条消息")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, max_claims=100)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["C1"]
    claim_file = os.path.join(tmp, "warm-claim-C1.json")
    assert os.path.exists(claim_file)
    assert json.load(open(claim_file)) == payload
    assert os.path.exists(os.path.join(tmp, "warm.json"))


def test_resident_rotation_claim_file_has_rotate():
    """轮换认领（max_claims=1）：claim 文件里也有 rotate=true，hook 唤醒
    worker 后 worker 按 prompt 不再写请求；marker 已清。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "C2", "最后一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, max_claims=1)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload.get("rotate") is True
    claim_file = os.path.join(tmp, "warm-claim-C2.json")
    assert os.path.exists(claim_file)
    assert json.load(open(claim_file)).get("rotate") is True
    assert not os.path.exists(os.path.join(tmp, "warm.json"))


def test_nonresident_claim_writes_no_claim_file():
    """对照：非驻留认领不写 claim 文件——只有 hook 所有的常驻 warm-wait
    才写（没人读它的 stdout，全靠 hook §1f 读文件唤醒）；非驻留是老流程，
    启动它的 worker 直接读 stdout。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "C3", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["C3"]
    assert not os.path.exists(os.path.join(tmp, "warm-claim-C3.json"))


def test_resident_crash_preserves_marker():
    """异常退出不清除 marker：认领中途崩了，hook supervisor 靠 warm.json
    的 until/claims 判断会话未完、重新拉起；清掉反而丢了会话信息。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "C4", "会崩的一条")
    orig_claim = wxbot._do_claim

    def _boom():
        raise RuntimeError("simulated claim crash")

    wxbot._do_claim = _boom
    try:
        with pytest.raises(RuntimeError, match="simulated claim crash"):
            capture(wxbot.cmd_warm_wait, resident=True, max_claims=100)
    finally:
        wxbot._do_claim = orig_claim
    assert os.path.exists(os.path.join(tmp, "warm.json"))


def test_resident_resume_preserves_until():
    """until 回归（2026-11-20 第二轮实测）：cap 是剩余时长，until 必须是
    启动时刻 + cap，不能是 session_start + cap——旧写法每轮重拉丢掉已流逝
    的时间，09:31 的会话三轮 worker 重拉 + 一轮 hook 重拉后 until 从次日
    09:31 缩到 05:14。账本 session_start 30 分钟前、marker until 1 小时
    后、cap 3600：认领后 marker 的 until 仍约启动时刻 + 3600，而不是
    session_start + 3600（只剩 30 分钟）。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    now = time.time()
    wxbot._resident_save(now - 1800, 0)
    wxbot._write_warm_marker(now - 1800, now + 3600)
    _seed_inbox(dirs, "U1", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=3600,
                      max_claims=100)
    assert rc == 0, out
    assert json.loads(out)["msg_ids"] == ["U1"]
    marker = json.load(open(os.path.join(tmp, "warm.json")))
    assert now + 3590 <= marker["until"] <= now + 3610, marker


def test_resident_expired_by_until_starts_fresh():
    """过期判断回归：marker 的 until 已过，哪怕 elapsed < cap（cap 是剩余
    时长），也必须开新会话而不是续旧会话。账本 1 小时前、claims=5、marker
    until 已过 10 秒、cap 7200：认领后账本 claims 应为 1（新会话 0+1），
    不是 6（续旧会话 5+1）；marker 的 since 是新会话起点。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    now = time.time()
    wxbot._resident_save(now - 3600, 5)
    wxbot._write_warm_marker(now - 3600, now - 10)
    _seed_inbox(dirs, "U2", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=7200,
                      max_claims=100)
    assert rc == 0, out
    assert json.loads(out)["msg_ids"] == ["U2"]
    assert _resident_claims(wxbot) == 1
    marker = json.load(open(os.path.join(tmp, "warm.json")))
    assert marker["since"] >= now - 5, marker


def test_resident_ledger_until_survives_nonresident_clobber():
    """until 钳制回归（2026-11-20 实测）：worker 自跑的非 resident warm-wait
    把 warm.json 的 until 改写成 start+1800 后，supervisor 重拉常驻时 cap
    被钳到 1648 秒、会话 16:25 提前结束。账本 until=2 小时后、marker
    until 只剩 10 分钟、cap 传 3600：认领后 marker 的 until 仍是启动时刻
    +3600（不被 marker 的 10 分钟钳制），且账本 until 不变。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    now = time.time()
    wxbot._resident_save(now - 1800, 5, now + 7200)
    wxbot._write_warm_marker(now - 1800, now + 600)
    _seed_inbox(dirs, "U3", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=3600,
                      max_claims=100)
    assert rc == 0, out
    assert json.loads(out)["msg_ids"] == ["U3"]
    marker = json.load(open(os.path.join(tmp, "warm.json")))
    assert now + 3590 <= marker["until"] <= now + 3610, marker
    assert _resident_claims(wxbot) == 6
    ledger = json.load(open(os.path.join(tmp, "warm-resident.json")))
    assert now + 7190 <= ledger["until"] <= now + 7210, ledger


def test_resident_ledger_until_overrides_stale_marker():
    """过期判断回归（2026-11-20）：非 resident warm-wait 把 warm.json 的
    until 改写成过去的时间后，重拉不能误判会话已结束开新会话。账本 until
    2 小时后、claims=5、marker until 已过 10 秒、cap 7200：认领后账本
    claims 应为 6（续旧会话），不是 1。"""
    import time
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    now = time.time()
    wxbot._resident_save(now - 3600, 5, now + 7200)
    wxbot._write_warm_marker(now - 3600, now - 10)
    _seed_inbox(dirs, "U4", "一条")
    rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=7200,
                      max_claims=100)
    assert rc == 0, out
    assert json.loads(out)["msg_ids"] == ["U4"]
    assert _resident_claims(wxbot) == 6
