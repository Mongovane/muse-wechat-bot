"""Hybrid warm-wait tests (2026-11-20).

Two warm paths coexist:
- fast path: the WORKER runs non-resident `wxbot.py warm-wait` as a
  foreground exec and waits; on a new message it self-claims via
  cmd_claim() and prints the payload, and the SAME worker answers in
  the same session (Nov 19 design, ~8-12s replies).
- backstop: the hook §1f supervisor keeps a RESIDENT warm-wait alive;
  if the worker's warm-wait is not running, the resident claims and
  the hook wakes a fresh worker.

Safety contract (the whole point of the hybrid): the non-resident
path must NEVER write a warm-claim-*.json file (only the resident
branch does), so a worker-run warm-wait can never cause the hook to
wake a second worker. Claims are atomic (os.replace in _do_claim),
so when both warm-waits race on one message exactly one wins and
exactly one worker answers — never zero, never two.
Tests only; never touches production files.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import pytest

_real_sleep = time.sleep


@contextlib.contextmanager
def _real_time_sleep():
    """让 time.sleep 在本块内是真的（不是别的测试文件留下的 no-op）。

    根因（2026-11-20 查实）：多个测试文件的 make_env 里写
    `wxbot.time.sleep = lambda s: None` 加速，且从不恢复——wxbot.time
    就是全局 time 模块，所以整个 pytest 进程的 time.sleep 会被改成
    no-op。别的测试不量真实耗时所以一直没爆；让步测试要量 2 秒，
    必须先恢复真 sleep。本块退出时还原进来之前的状态。"""
    import time as _time
    prev = _time.sleep
    _time.sleep = _real_sleep
    try:
        yield
    finally:
        _time.sleep = prev

BASE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
HOOK_SCRIPT = os.path.join(HOME, "hooks", "scripts", "wechat-inbox.sh")
LIVE_BOT = os.path.join(HOME, "workspace", "wechat-bot")
VENV_PYTHON = os.path.join(HOME, "workspace", "CowAgent", "venv", "bin", "python")


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    """Fresh tmp env; EVERY writable module path points at tmp."""
    tmp = tempfile.mkdtemp(prefix="hybridtest-")
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


def _seed_inbox(dirs, mid, text, sender="u1"):
    p = os.path.join(dirs["inbox"], mid + ".json")
    with open(p, "w") as f:
        json.dump({"from": sender, "text": text, "msg_id": mid,
                   "context_token": "ctx-" + mid}, f)
    return p


def _claim_files(tmp):
    return [f for f in os.listdir(tmp)
            if f.startswith("warm-claim-") and f.endswith(".json")]


# --- (a) non-resident claim: no claim file, moves to processing, prints payload

def test_hybrid_nonresident_claim_writes_no_claim_file():
    """混合版快路的契约：worker 自己跑的 warm-wait 认领后只打印 payload
    给等着的 worker，不写 warm-claim-*.json（只有 resident 才写），所以
    hook 不会再另唤一个 worker。消息 inbox→processing，payload 含该条。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "H1", "快路跟进")
    rc, out = capture(wxbot.cmd_warm_wait, idle_secs=1, cap_secs=5)
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["msg_ids"] == ["H1"], payload
    assert payload["messages"]["H1"]["text"] == "快路跟进"
    assert not os.listdir(dirs["inbox"])
    proc = json.load(open(os.path.join(dirs["processing"], "H1.json")))
    assert proc["claim_ts"] == payload["messages"]["H1"]["claim_ts"]
    assert _claim_files(tmp) == [], "non-resident must not write a claim file"


# --- (b) resident defers 2s: worker-run warm-wait wins when both run;
# resident still claims (after the defer) when alone

def _run_defer_race(rep):
    """Both warm-waits start together (barrier) on one seeded message.
    The resident must defer 2s, so the non-resident (1s poll) claims it
    first; the resident finds nothing, keeps polling, and exits on its
    short cap. Asserts the outcome."""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "H1", "让步抢认领 %d" % rep)
    barrier = threading.Barrier(2)
    results = {}

    def _worker(name, **kw):
        barrier.wait(timeout=10)
        try:
            results[name] = wxbot.cmd_warm_wait(**kw)
        except Exception as e:  # noqa: BLE001 - record, assert below
            results[name] = e

    t_res = threading.Thread(target=_worker, args=("resident",),
                           kwargs={"resident": True, "cap_secs": 5,
                                   "max_claims": 100})
    t_fast = threading.Thread(target=_worker, args=("fast",),
                            kwargs={"idle_secs": 5, "cap_secs": 30})
    with _real_time_sleep():
        t_res.start()
        t_fast.start()
        t_res.join(timeout=20)
        t_fast.join(timeout=20)
    assert not t_res.is_alive() and not t_fast.is_alive(), \
        "a warm-wait thread hung (rep %d)" % rep
    for name, v in results.items():
        assert not isinstance(v, Exception), "%s raised %r" % (name, v)
    assert os.listdir(dirs["inbox"]) == [], "rep %d: inbox not drained" % rep
    proc_json = sorted(f for f in os.listdir(dirs["processing"])
                       if f.endswith(".json"))
    assert proc_json == ["H1.json"], \
        "rep %d: message claimed %s times" % (rep, proc_json)
    # 非驻留赢：resident 让步 2 秒，复查时消息已被认领走，所以它一个
    # claim 文件都没写；resident 赢了才会有 warm-claim-H1.json。
    assert _claim_files(tmp) == [], \
        "rep %d: resident claimed despite the defer: %s" % (
            rep, _claim_files(tmp))


def test_hybrid_nonresident_wins_when_both_running():
    """混合版让步：常驻与 worker 自跑的 warm-wait 同时在跑，worker 那路
    必须赢（它 1 秒轮询，常驻让步 2 秒）。跑 3 轮，每轮都要求零 claim
    文件、消息恰好认领一次。"""
    for i in range(3):
        _run_defer_race(i)


def test_hybrid_resident_defers_two_seconds_when_alone():
    """常驻单独在跑：发现消息后必须等 2 秒再认领（让步），不是立刻抢。
    直接量时间：从启动到认领返回不少于 1.9 秒，且 claim 文件恰好一个。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    _seed_inbox(dirs, "H1", "单独认领")
    t0 = time.time()
    with _real_time_sleep():
        rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=30,
                          max_claims=100)
    elapsed = time.time() - t0
    assert rc == 0, "resident did not claim (rc=%r, out=%r)" % (rc, out)
    assert elapsed >= 1.9, \
        "resident claimed in %.2fs, defer did not happen" % elapsed
    assert elapsed < 8, "resident took too long: %.2fs" % elapsed
    assert os.listdir(dirs["inbox"]) == [], "inbox not drained"
    proc_json = sorted(f for f in os.listdir(dirs["processing"])
                       if f.endswith(".json"))
    assert proc_json == ["H1.json"], "message claimed %s times" % proc_json
    claim_files = _claim_files(tmp)
    assert claim_files == ["warm-claim-H1.json"], \
        "resident won but claim file is %s" % (claim_files,)
    assert json.load(open(os.path.join(tmp, claim_files[0])))["msg_ids"] == ["H1"]


# --- (b3) adaptive defer: recent done file -> long defer, slow worker wins;
# no recent done -> short defer

def _seed_done(dirs, name, age_secs=0):
    """Seed a done file with mtime `age_secs` seconds ago."""
    p = os.path.join(dirs["done"], name + ".json")
    with open(p, "w") as f:
        json.dump({"msg_id": name, "text": "done"}, f)
    t = time.time() - age_secs
    os.utime(p, (t, t))
    return p


def test_recent_done():
    """_recent_done 只认 window 秒内的 done 文件。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    assert wxbot._recent_done() is False
    _seed_done(dirs, "OLD", age_secs=120)
    assert wxbot._recent_done() is False
    _seed_done(dirs, "NEW", age_secs=5)
    assert wxbot._recent_done() is True


def _run_adaptive_race():
    """自适应让步端到端：done 文件 5 秒前落盘（worker 刚回完），非驻留
    warm-wait 晚 1.5 秒才启动（模拟 worker 发完到跑起 warm-wait 的慢
    启动）；常驻必须等长让步（本测试 patch 成 3 秒），让慢启动的 worker
    那路先抢到。断言零 claim 文件、消息恰好认领一次。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    wxbot.WARM_RESIDENT_DEFER_SECS = 0.5
    wxbot.WARM_RESIDENT_DEFER_LONG_SECS = 3
    _seed_done(dirs, "PREV", age_secs=5)
    _seed_inbox(dirs, "H1", "自适应让步")
    barrier = threading.Barrier(2)
    results = {}

    def _worker(name, delay, **kw):
        barrier.wait(timeout=10)
        if delay:
            _real_sleep(delay)
        try:
            results[name] = wxbot.cmd_warm_wait(**kw)
        except Exception as e:  # noqa: BLE001
            results[name] = e

    t_res = threading.Thread(target=_worker, args=("resident", 0),
                           kwargs={"resident": True, "cap_secs": 6,
                                   "max_claims": 100})
    t_fast = threading.Thread(target=_worker, args=("fast", 1.5),
                            kwargs={"idle_secs": 5, "cap_secs": 30})
    with _real_time_sleep():
        t_res.start()
        t_fast.start()
        t_res.join(timeout=20)
        t_fast.join(timeout=20)
    assert not t_res.is_alive() and not t_fast.is_alive(), \
        "a warm-wait thread hung"
    for name, v in results.items():
        assert not isinstance(v, Exception), "%s raised %r" % (name, v)
    assert os.listdir(dirs["inbox"]) == [], "inbox not drained"
    proc_json = sorted(f for f in os.listdir(dirs["processing"])
                       if f.endswith(".json"))
    assert proc_json == ["H1.json"], \
        "message claimed %s times" % (proc_json,)
    assert _claim_files(tmp) == [], \
        "resident claimed despite the long defer: %s" % (_claim_files(tmp),)


def test_hybrid_adaptive_long_defer_lets_slow_worker_win():
    """刚回完 + worker 慢启动时，常驻必须等长让步，让 worker 那路赢。"""
    _run_adaptive_race()


def test_hybrid_recent_done_uses_long_defer():
    """常驻单独在跑且刚有 worker 回完：必须等长让步（本测试 patch 成 3 秒），
    不是短让步。直接量时间。"""
    wxbot = load_wxbot()
    tmp, dirs = make_env(wxbot)
    wxbot.WARM_RESIDENT_DEFER_SECS = 0.5
    wxbot.WARM_RESIDENT_DEFER_LONG_SECS = 3
    _seed_done(dirs, "PREV", age_secs=5)
    _seed_inbox(dirs, "H1", "单独认领长让步")
    t0 = time.time()
    with _real_time_sleep():
        rc, out = capture(wxbot.cmd_warm_wait, resident=True, cap_secs=30,
                          max_claims=100)
    elapsed = time.time() - t0
    assert rc == 0, "resident did not claim (rc=%r, out=%r)" % (rc, out)
    assert elapsed >= 2.9, \
        "resident claimed in %.2fs, long defer did not happen" % elapsed
    assert elapsed < 8, "resident took too long: %.2fs" % elapsed
    claim_files = _claim_files(tmp)
    assert claim_files == ["warm-claim-H1.json"], \
        "resident won but claim file is %s" % (claim_files,)


# --- (c) worker warm-wait exit + request-warm -> supervisor keeps resident

RUNTIME_STUB = """\
wake() { echo "WAKE|$1|$2" >> "$WAKE_LOG"; exit 0; }
silent() { echo "SILENT|$1|$2" >> "$WAKE_LOG"; exit 0; }
log() { echo "LOG|$1|$2" >> "$WAKE_LOG"; }
"""


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _wait_for(fn, timeout=15, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        _real_sleep(interval)
    return fn()


def _build_sandbox():
    sb = tempfile.mkdtemp(prefix="hook-hybrid-")
    home = os.path.join(sb, "home")
    bot = os.path.join(sb, "bot")
    os.makedirs(os.path.join(home, "hooks", "state"))
    os.makedirs(os.path.join(home, "hooks", "scripts"))
    for name in ("inbox", "processing", "done", "outbox", "sent",
                 "history", "media"):
        os.makedirs(os.path.join(bot, name))
    shutil.copy(os.path.join(LIVE_BOT, "wxbot.py"),
                os.path.join(bot, "wxbot.py"))
    for name in ("ack_timer.sh", "watchdog.sh"):
        stub = os.path.join(bot, name)
        with open(stub, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    "echo \"$@\" >> %s\n" % os.path.join(sb, name + ".log"))
        os.chmod(stub, 0o755)
    open(os.path.join(bot, "buf.txt"), "w").close()
    open(os.path.join(bot, "receiver.log"), "w").close()
    wake_log = os.path.join(sb, "wake.log")
    runtime = os.path.join(sb, "runtime.sh")
    with open(runtime, "w") as f:
        f.write(RUNTIME_STUB.replace("$WAKE_LOG", wake_log))
    sup_log = os.path.join(sb, "supervisor-spawns.log")
    with open(os.path.join(home, "hooks", "scripts",
                           "wechat-receiver-supervisor.sh"), "w") as f:
        f.write("#!/usr/bin/env bash\n"
                "echo spawned >> %s\n" % sup_log)
    env = dict(os.environ)
    env.update({
        "HOME": home,
        "WECHAT_BOT_DIR": bot,
        "WECHAT_BOT_PYTHON": VENV_PYTHON,
        "HATCH_HOOK_RUNTIME": runtime,
    })
    return {"sb": sb, "home": home, "bot": bot, "env": env,
            "wake_log": wake_log, "sup_log": sup_log}


def _run_hook(sb):
    p = subprocess.run(["bash", HOOK_SCRIPT], env=sb["env"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
    return p.returncode


def _wake_lines(sb):
    if not os.path.exists(sb["wake_log"]):
        return []
    with open(sb["wake_log"]) as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def _warm_pid(bot):
    try:
        return json.load(open(os.path.join(bot, "warm.json")))["pid"]
    except (OSError, ValueError, KeyError):
        return None


def _kill(pid):
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_hybrid_worker_exit_then_request_warm_keeps_resident():
    """混合版交接：worker 自己跑的 warm-wait 空闲退出（IDLE_TIMEOUT，
    marker 清除）→ worker 写 request-warm → hook §1f 拉起常驻 warm-wait
    并保持心跳；随后一条消息由常驻认领（claim 文件 + processing），下
    一轮 hook 消费 claim 文件唤醒 worker 并拉起替代常驻。"""
    sb = _build_sandbox()
    bot = sb["bot"]
    try:
        # 1. worker 的 warm-wait：空 inbox，1 秒空闲后 IDLE_TIMEOUT 退出，
        #    marker 清除（快路结束，交接给常驻）。
        p = subprocess.run(
            [VENV_PYTHON, os.path.join(bot, "wxbot.py"), "warm-wait",
             "--idle-secs", "1", "--cap-secs", "5"],
            cwd=bot, env=sb["env"], capture_output=True, text=True,
            timeout=30)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "IDLE_TIMEOUT" in p.stdout, p.stdout
        assert not os.path.exists(os.path.join(bot, "warm.json"))
        assert not os.path.exists(os.path.join(bot, "warm-request.json"))

        # 2. worker 写 request-warm 就退出（混合版第 7 步 IDLE_TIMEOUT 分支）。
        p = subprocess.run(
            [VENV_PYTHON, os.path.join(bot, "wxbot.py"), "request-warm",
             "--cap-secs", "120"],
            cwd=bot, env=sb["env"], capture_output=True, text=True,
            timeout=30)
        assert p.returncode == 0 and "WARM_REQUEST_OK 120" in p.stdout
        assert os.path.exists(os.path.join(bot, "warm-request.json"))

        # 3. hook 拉起常驻 warm-wait；hook 退出后它必须还活着，心跳在走。
        assert _run_hook(sb) == 0
        pid1 = _wait_for(lambda: _warm_pid(bot), timeout=10)
        assert pid1 and _pid_alive(pid1), "hook did not spawn resident"
        hb1 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        _real_sleep(6)
        assert _pid_alive(pid1), "resident died after hook exit"
        hb2 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        assert hb2 > hb1, "heartbeat not advancing"

        # 4. 投一条消息：常驻认领（inbox→processing + claim 文件），认领完退出。
        with open(os.path.join(bot, "inbox", "888.json"), "w") as f:
            json.dump({"from": "u1", "text": "兜底跟进",
                       "msg_id": "888", "context_token": "ctx-888"}, f)
        claimed = _wait_for(
            lambda: os.path.exists(os.path.join(bot, "warm-claim-888.json"))
            and os.path.exists(os.path.join(bot, "processing", "888.json")),
            timeout=15)
        assert claimed, "resident did not claim within 15s"
        assert not os.path.exists(os.path.join(bot, "inbox", "888.json"))
        assert _wait_for(lambda: not _pid_alive(pid1), timeout=5), \
            "claimer should exit after claim"
        claim = json.load(open(os.path.join(bot, "warm-claim-888.json")))
        assert claim["msg_ids"] == ["888"]
        assert claim["messages"]["888"]["text"] == "兜底跟进"

        # 5. 下一轮 hook：消费 claim 文件、唤醒 worker、拉起替代常驻。
        assert _run_hook(sb) == 0
        wakes = [ln for ln in _wake_lines(sb) if ln.startswith("WAKE|")]
        assert len(wakes) == 1, wakes
        payload = json.loads(wakes[0].split("|", 2)[2])
        assert payload["msg_ids"] == ["888"]
        assert payload["messages"]["888"]["text"] == "兜底跟进"
        assert os.path.exists(
            os.path.join(bot, "warm-claim-888.json.consumed"))
        assert not os.path.exists(os.path.join(bot, "warm-claim-888.json"))
        pid2 = _wait_for(
            lambda: (_warm_pid(bot)
                     if _warm_pid(bot) not in (None, pid1) else None),
            timeout=10)
        assert pid2 and _pid_alive(pid2), \
            "hook did not spawn a replacement resident"
        until = json.load(open(os.path.join(bot, "warm.json")))["until"]
        assert 0 < until - time.time() <= 120, "replacement cap not remaining"
        _kill(pid2)
        _real_sleep(0.5)
        assert not _pid_alive(pid2)
    finally:
        _kill(_warm_pid(bot))
        shutil.rmtree(sb["sb"], ignore_errors=True)
