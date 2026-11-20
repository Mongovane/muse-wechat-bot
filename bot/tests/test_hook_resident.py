"""Hook-owned resident warm-wait sandbox test (2026-11-20 round 2).

Drives the REAL ~/hooks/scripts/wechat-inbox.sh in a /tmp sandbox:
- fake HOME (hooks/state, hooks/scripts), fake BOT_DIR holding a COPY of the
  real wxbot.py (BASE = sandbox, so every writable path lands in tmp),
  stub HATCH_HOOK_RUNTIME (wake/silent/log only recorded, never wakes a
  real worker), stub ack_timer.sh / watchdog.sh, stub receiver-supervisor.
- The LIVE receiver stays running and untouched: hook §1 sees it alive via
  the global pgrep, and the fake buf.txt is kept fresh so §1a never targets
  it. Nothing here touches live inbox/processing/outbox, and no message is
  ever sent (the stub runtime only records wake calls).
- Proves the round-2 design: a hook-spawned warm-wait SURVIVES the hook
  process exiting (the exact failure mode of worker-owned warm-wait: three
  rounds died 5s–4.5min after the worker's turn ended), claims a message,
  files warm-claim-<id>.json, and the next hook run wakes a worker with
  that payload and spawns a replacement warm-wait.
Real timing is used (no sleep stubbing); the hook flow takes ~30s.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

# 其他测试文件会把 time.sleep 桩成空操作（全局生效）；这里在 import 时
# 就抓一份真 sleep，本文件的等待循环只用它。
_real_sleep = time.sleep

HOME = os.path.expanduser("~")
HOOK_SCRIPT = os.path.join(HOME, "hooks", "scripts", "wechat-inbox.sh")
LIVE_BOT = os.path.join(HOME, "workspace", "wechat-bot")
VENV_PYTHON = os.path.join(HOME, "workspace", "CowAgent", "venv", "bin", "python")

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
    sb = tempfile.mkdtemp(prefix="hook-r2-")
    home = os.path.join(sb, "home")
    bot = os.path.join(sb, "bot")
    os.makedirs(os.path.join(home, "hooks", "state"))
    os.makedirs(os.path.join(home, "hooks", "scripts"))
    for name in ("inbox", "processing", "done", "outbox", "sent",
                 "history", "media"):
        os.makedirs(os.path.join(bot, name))
    # 真实 wxbot.py 的副本：BASE = sandbox，所有可写路径落 tmp。
    shutil.copy(os.path.join(LIVE_BOT, "wxbot.py"),
                os.path.join(bot, "wxbot.py"))
    # claim 专用桩脚本：只记调用，不发 typing、不回收单子。
    for name in ("ack_timer.sh", "watchdog.sh"):
        stub = os.path.join(bot, name)
        with open(stub, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    "echo \"$@\" >> %s\n" % os.path.join(sb, name + ".log"))
        os.chmod(stub, 0o755)
    # buf.txt 保持新鲜：§1a 不会判定接收器卡住；receiver.log 为空。
    open(os.path.join(bot, "buf.txt"), "w").close()
    open(os.path.join(bot, "receiver.log"), "w").close()
    # 桩 runtime：只记录 wake/silent/log。
    wake_log = os.path.join(sb, "wake.log")
    runtime = os.path.join(sb, "runtime.sh")
    with open(runtime, "w") as f:
        f.write(RUNTIME_STUB.replace("$WAKE_LOG", wake_log))
    # 桩 receiver-supervisor：只记一次生成，不留常驻循环。
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


def test_hook_owned_warm_wait_survives_and_claims():
    """决定性测试：hook 拉起的 warm-wait 在 hook 进程退出后仍活着（worker
    所有的三轮全死在这里），认领后写 claim 文件，下一轮 hook 唤醒 worker
    并拉起替代 warm-wait。"""
    sb = _build_sandbox()
    bot = sb["bot"]
    try:
        # 1. 空 inbox：hook 静默，不拉 warm-wait（无请求、无 warm.json）。
        assert _run_hook(sb) == 0
        assert any(ln.startswith("SILENT|") for ln in _wake_lines(sb))
        assert not os.path.exists(os.path.join(bot, "warm.json"))
        assert _wait_for(lambda: os.path.exists(sb["sup_log"]), timeout=5), \
            "§1g did not spawn the supervisor stub"

        # 2. worker 视角：只写请求就退出（真实 request-warm CLI）。
        p = subprocess.run(
            [VENV_PYTHON, os.path.join(bot, "wxbot.py"), "request-warm",
             "--cap-secs", "120"],
            cwd=bot, env=sb["env"], capture_output=True, text=True,
            timeout=30)
        assert p.returncode == 0 and "WARM_REQUEST_OK 120" in p.stdout
        assert os.path.exists(os.path.join(bot, "warm-request.json"))

        # 3. hook 拉起 warm-wait；hook 退出后它必须还活着，心跳还在走。
        assert _run_hook(sb) == 0
        pid1 = _wait_for(lambda: _warm_pid(bot), timeout=10)
        assert pid1 and _pid_alive(pid1), "hook did not spawn warm-wait"
        hb1 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        _real_sleep(6)  # 心跳每 5 秒写一次；hook 进程早已退出，worker 所有的
        # 三轮死在这里（5 秒～4.5 分钟）
        assert _pid_alive(pid1), "hook-spawned warm-wait died with hook"
        hb2 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        assert hb2 > hb1, "heartbeat not advancing"

        # 4. 投一条消息：warm-wait 认领（inbox→processing + claim 文件），
        # 认领完自己退出（marker 保留，等 hook 拉替代）。
        with open(os.path.join(bot, "inbox", "777.json"), "w") as f:
            json.dump({"from": "u1", "text": "第二条",
                       "msg_id": "777", "context_token": "ctx-777"}, f)
        claimed = _wait_for(
            lambda: os.path.exists(os.path.join(bot, "warm-claim-777.json"))
            and os.path.exists(os.path.join(bot, "processing", "777.json")),
            timeout=15)
        assert claimed, "warm-wait did not claim within 15s"
        assert not os.path.exists(os.path.join(bot, "inbox", "777.json"))
        # 认领完自己退出（写文件→打印→返回有几毫秒延迟，等它彻底退出）。
        assert _wait_for(lambda: not _pid_alive(pid1), timeout=5), \
            "claimer should exit after claim"
        assert os.path.exists(os.path.join(bot, "warm.json"))
        claim = json.load(open(os.path.join(bot, "warm-claim-777.json")))
        assert claim["msg_ids"] == ["777"]
        assert claim["messages"]["777"]["text"] == "第二条"
        ack_log = os.path.join(sb["sb"], "ack_timer.sh.log")
        assert _wait_for(
            lambda: os.path.exists(ack_log)
            and open(ack_log).read().split()[0] == "777",
            timeout=5), "ack_timer stub was not called for 777"

        # 5. 下一轮 hook：消费 claim 文件、唤醒 worker、拉起替代 warm-wait。
        assert _run_hook(sb) == 0
        wakes = [ln for ln in _wake_lines(sb) if ln.startswith("WAKE|")]
        assert len(wakes) == 1, wakes
        payload = json.loads(wakes[0].split("|", 2)[2])
        assert payload["msg_ids"] == ["777"]
        assert payload["messages"]["777"]["text"] == "第二条"
        assert os.path.exists(os.path.join(bot, "warm-claim-777.json.consumed"))
        assert not os.path.exists(os.path.join(bot, "warm-claim-777.json"))
        # 替代 warm-wait 的 marker 会覆盖旧 pid1：等一个不同且活着的 pid。
        pid2 = _wait_for(
            lambda: (_warm_pid(bot)
                     if _warm_pid(bot) not in (None, pid1) else None),
            timeout=10)
        assert pid2 and _pid_alive(pid2), \
            "hook did not spawn a replacement warm-wait"
        until = json.load(open(os.path.join(bot, "warm.json")))["until"]
        assert 0 < until - time.time() <= 120, "replacement cap not remaining"
        _kill(pid2)
        _real_sleep(0.5)
        assert not _pid_alive(pid2)
    finally:
        pid = _warm_pid(bot)
        _kill(pid)
        shutil.rmtree(sb["sb"], ignore_errors=True)


def test_hook_exits_promptly_with_piped_stdout_when_spawning():
    """2026-11-20 11:17/11:25 回归：live hook 的 stdout 是 runtime 的 pipe，
    subshell 形 spawn（( ... & )，无论 flock/trailing log/exit 0 还是
    || true）会让 hook 挂在 do_wait（两轮各挂数分钟，且挂住的调用会堵住
    后续轮询——kill 14496 后 60 秒无重拉，kill 挂住的 14505 后 6 秒即重拉）。
    f2 形（主脚本直接后台 + disown）必须让 hook 在 pipe 下也立即退出，
    同时 daemon 活着、心跳在走。/tmp 沙箱不能复现 live 的挂起（旧形在
    沙箱 pipe 下也会退出），这只是回归守卫；live 证明是 kill 14496/14505
    后的重拉：spawner 退出、无挂住的调用。"""
    sb = _build_sandbox()
    bot = sb["bot"]
    try:
        with open(os.path.join(bot, "warm-request.json"), "w") as f:
            json.dump({"cap_secs": 120, "ts": int(time.time())}, f)
        # stdout 接 pipe（模拟 live runtime）；挂住会在这里超时失败。
        p = subprocess.run(["bash", HOOK_SCRIPT], env=sb["env"],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=20)
        assert p.returncode == 0
        pid1 = _wait_for(lambda: _warm_pid(bot), timeout=10)
        assert pid1 and _pid_alive(pid1), "hook did not spawn warm-wait"
        hb1 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        _real_sleep(6)
        assert _pid_alive(pid1), "warm-wait died after hook exit"
        hb2 = json.load(open(os.path.join(bot, "warm.json")))["heartbeat"]
        assert hb2 > hb1, "heartbeat not advancing"
        # §1g 同样走 f2：supervisor stub 被拉起。
        assert _wait_for(lambda: os.path.exists(sb["sup_log"]), timeout=5), \
            "§1g did not spawn the supervisor stub"
    finally:
        _kill(_warm_pid(bot))
        shutil.rmtree(sb["sb"], ignore_errors=True)


def _supervisor_sandbox(session_expired=False):
    """常驻 receiver supervisor 沙箱：桩 BOT_PYTHON（记一次启动后以
    'wxbot.py receive SANDBOXTEST' 的 cmdline 睡 3 秒即死），方便 pgrep
    只匹配桩进程、不撞 live 接收器。"""
    sb = tempfile.mkdtemp(prefix="recv-sup-")
    home = os.path.join(sb, "home")
    bot = os.path.join(sb, "bot")
    os.makedirs(os.path.join(home, "hooks", "state"))
    os.makedirs(bot)
    stub_log = os.path.join(sb, "receive-starts.log")
    stub = os.path.join(sb, "stub-python")
    with open(stub, "w") as f:
        f.write("#!/usr/bin/env bash\n"
                "echo \"RECEIVE_START $(date +%%s%%N)\" >> %s\n"
                "exec -a \"wxbot.py receive SANDBOXTEST\" sleep 3\n" % stub_log)
    os.chmod(stub, 0o755)
    with open(os.path.join(bot, "receiver.log"), "w") as f:
        if session_expired:
            f.write("SESSION_EXPIRED\n")
    env = dict(os.environ)
    env.update({
        "HOME": home,
        "WECHAT_BOT_DIR": bot,
        "WECHAT_BOT_PYTHON": stub,
        "WECHAT_RECEIVER_SUPERVISOR_INTERVAL": "2",
        "WECHAT_RECEIVER_PATTERN": "[w]xbot.py receive SANDBOXTEST",
    })
    return {"sb": sb, "home": home, "bot": bot, "env": env,
            "stub_log": stub_log}


def _count_starts(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for ln in f if ln.startswith("RECEIVE_START"))


def test_receiver_supervisor_restarts_dead_receiver():
    """接收器死了（桩进程 3 秒即退）→ supervisor 每轮重拉；重启记入日志。"""
    sb = _supervisor_sandbox()
    sup = os.path.join(HOME, "hooks", "scripts",
                       "wechat-receiver-supervisor.sh")
    proc = subprocess.Popen(["bash", sup], env=sb["env"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        assert _wait_for(lambda: _count_starts(sb["stub_log"]) >= 2,
                         timeout=25), "supervisor did not restart receiver"
        sup_log = os.path.join(sb["home"], "hooks", "state",
                               "receiver-supervisor.log")
        assert os.path.exists(sup_log)
        assert "supervisor restarted receiver" in open(sup_log).read()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        subprocess.run(["pkill", "-f", "wxbot.py receive SANDBOXTEST"],
                       capture_output=True)
        shutil.rmtree(sb["sb"], ignore_errors=True)


def test_receiver_supervisor_respects_session_expired():
    """receiver.log 有 SESSION_EXPIRED → 不拉（重拉也没用，等人工扫码）。"""
    sb = _supervisor_sandbox(session_expired=True)
    sup = os.path.join(HOME, "hooks", "scripts",
                       "wechat-receiver-supervisor.sh")
    proc = subprocess.Popen(["bash", sup], env=sb["env"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        _real_sleep(8)  # 约 4 轮检查（间隔 2 秒）
        assert _count_starts(sb["stub_log"]) == 0
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        shutil.rmtree(sb["sb"], ignore_errors=True)
