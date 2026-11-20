"""keepalive 心跳 + 认领建 .alive + 提示词回归测试（2026-11-20）。

背景：2026-11-20 09:34 连发，watchdog 20 秒宽限一到就误判冷启动
worker 已死、反复重认领，第一份丢回复、第二份晚 5~6 分钟。根因之
一：.alive 只靠 worker 在提示第 0/5 步顺手 touch，长思考中途无刷新，
且认领时根本没有 .alive 文件。修法：认领时立刻建 .alive（_do_claim /
hook §2），worker 起 `wxbot.py keepalive <id>` 每 10 秒续 touch，
watchdog 按新鲜度判活。
keepalive 测试用子进程实跑（不用进程内调）：别的测试文件会把
wxbot.time.sleep 桩成空操作，进程内跑 keepalive 会空转；子进程用真
sleep。只读 live 文件，不动 outbox、不发消息。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# 别的测试文件会在 make_env 里把 time.sleep 桩成空操作（wxbot.time 就是
# 全局 time 模块，桩是进程级生效）；这里在 import 时就抓一份真 sleep，
# 本文件所有等待都用它，否则全量跑时 time.sleep(2.5) 会立刻返回、时序
# 断言全错（单跑时没人桩过，所以单跑能过、全量挂——2026-11-20 实测）。
_real_sleep = time.sleep


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_keepalive_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="keepalive-")
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


def test_parse_keepalive_args():
    wxbot = load_wxbot()
    assert wxbot._parse_keepalive_args([]) == (10, 1200)
    assert wxbot._parse_keepalive_args(["--interval-secs=2", "--max-secs=60"]) == (2, 60)
    assert wxbot._parse_keepalive_args(["--interval-secs", "2", "--max-secs", "60"]) == (2, 60)
    assert wxbot._parse_keepalive_args(["--interval-secs", "abc"]) == (10, 1200)
    assert wxbot._parse_keepalive_args(["--max-secs", "-5"]) == (10, 1200)


def test_claim_creates_alive_marker():
    """认领时立刻建 processing/<id>.alive（watchdog 判活的基准）。"""
    import contextlib
    import io
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    with open(os.path.join(dirs["inbox"], "K1.json"), "w") as f:
        json.dump({"from": "u1", "text": "hi", "msg_id": "K1"}, f)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wxbot.cmd_claim()
    assert rc == 0, buf.getvalue()
    alive = os.path.join(dirs["processing"], "K1.alive")
    assert os.path.exists(alive), "claim must create .alive at claim time"
    assert os.path.exists(os.path.join(dirs["processing"], "K1.json"))


def test_warm_wait_self_claim_creates_alive_marker():
    """warm-wait 自认领同样建 .alive（走 _do_claim 同一路）。"""
    import contextlib
    import io
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    with open(os.path.join(dirs["inbox"], "K2.json"), "w") as f:
        json.dump({"from": "u1", "text": "follow-up", "msg_id": "K2"}, f)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wxbot.cmd_warm_wait(idle_secs=1, cap_secs=5)
    assert rc == 0, buf.getvalue()
    assert os.path.exists(os.path.join(dirs["processing"], "K2.alive"))


def _copy_wxbot_to_tmp():
    tmp = tempfile.mkdtemp(prefix="keepalive-subproc-")
    for name in ("inbox", "processing", "outbox", "done", "sent"):
        os.makedirs(os.path.join(tmp, name))
    import shutil
    shutil.copy(os.path.join(BASE, "..", "wxbot.py"),
                os.path.join(tmp, "wxbot.py"))
    return tmp


def test_keepalive_touches_and_exits_when_job_done():
    """keepalive 每 interval 秒刷新 .alive；processing/<id>.json 消失
    （回完）后自己退出。子进程实跑，真 sleep。"""
    tmp = _copy_wxbot_to_tmp()
    proc_path = os.path.join(tmp, "processing", "K3.json")
    with open(proc_path, "w") as f:
        json.dump({"msg_id": "K3", "claim_ts": int(time.time())}, f)
    alive = os.path.join(tmp, "processing", "K3.alive")
    p = subprocess.Popen(
        [PY, os.path.join(tmp, "wxbot.py"), "keepalive", "K3",
         "--interval-secs", "1", "--max-secs", "30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _real_sleep(2.5)
        assert os.path.exists(alive), "keepalive must create .alive"
        m1 = os.path.getmtime(alive)
        _real_sleep(1.2)
        m2 = os.path.getmtime(alive)
        assert m2 > m1, "keepalive must refresh .alive periodically"
        os.remove(proc_path)
        p.wait(timeout=5)
        assert p.returncode == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_keepalive_exits_on_max_secs():
    """单子一直不回完：keepalive 到 max_secs 自己停，不会无限 touch
    下去（否则 worker 死后它还续命、把死 worker 伪装成活的）。"""
    tmp = _copy_wxbot_to_tmp()
    with open(os.path.join(tmp, "processing", "K4.json"), "w") as f:
        json.dump({"msg_id": "K4", "claim_ts": int(time.time())}, f)
    p = subprocess.run(
        [PY, os.path.join(tmp, "wxbot.py"), "keepalive", "K4",
         "--interval-secs", "1", "--max-secs", "2"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    assert p.returncode == 0


def test_warm_wait_detaches_from_parent_process_group():
    """2026-11-20 常驻死亡根因：前台 exec 跑 warm-wait 时进程留在 worker
    进程组，worker 写总结结束本轮时 runtime 连进程组杀掉，warm-wait 只
    活几十秒。__main__ 入口 setsid 后，warm-wait 应在自己的进程组里
    （PGID == PID；本环境 getsid/ps 读 SID 恒为 0，用 PGID 判定），且
    认领 payload 照常输出。先空 inbox 起进程、确认存活与脱离，再投消息。"""
    tmp = _copy_wxbot_to_tmp()
    p = subprocess.Popen(
        [PY, os.path.join(tmp, "wxbot.py"), "warm-wait", "--resident",
         "--cap-secs", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        _real_sleep(2)
        assert p.poll() is None, "warm-wait must still run on empty inbox"
        assert os.getpgid(p.pid) == p.pid, (
            "warm-wait must be its own process-group leader (setsid), "
            "got pgid=%d pid=%d" % (os.getpgid(p.pid), p.pid))
        with open(os.path.join(tmp, "inbox", "K5.json"), "w") as f:
            json.dump({"from": "u1", "text": "hi", "msg_id": "K5",
                       "context_token": "ctx"}, f)
        out, _ = p.communicate(timeout=30)
        payload = json.loads(out)
        assert payload["msg_ids"] == ["K5"], payload
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_prompt_instructs_request_warm_and_keepalive():
    """提示词回归（2026-11-20 混合版，取代第二轮的常驻版断言）：step 7
    必须写明 request-warm（空闲/总时长到后只写 warm-request.json 就
    退出，hook supervisor 拉起 warm-wait），且必须写明快路是 worker
    自己前台跑 warm-wait（同会话回，绝不用 background=true——worker
    名下的后台进程活不过本轮结束）；step 0 必须写明起 keepalive
    （前台 exec，绝不用 background=true）。第二轮的“绝不要自己跑
    warm-wait”是常驻版的写法，混合版已故意改掉，不再断言它。
    仓库 md 与线上 JSON 两处都要有。"""
    md = None
    for cand in (os.path.join(BASE, "..", "prompts", "hook-worker.md"),
                 "/home/hatch/workspace/muse-wechat-bot/bot/prompts/hook-worker.md"):
        if os.path.exists(cand):
            md = open(cand).read()
            break
    if md is None:
        import pytest
        pytest.skip("hook-worker.md not found in either location")
    assert "request-warm" in md, "hook-worker.md step 7 must say request-warm"
    assert "前台跑一次 warm-wait" in md, \
        "hook-worker.md step 7 must say the worker runs warm-wait in the foreground"
    assert "绝不用 background=true" in md, \
        "hook-worker.md step 7 must forbid background=true for warm-wait"
    assert "keepalive" in md, "hook-worker.md step 0 must say keepalive"
    live = "/home/hatch/hooks/definitions/wechat-inbox.json"
    if os.path.exists(live):
        d = json.load(open(live))
        pr = d.get("prompt", "")
        assert "request-warm" in pr, "live prompt step 7 must say request-warm"
        assert "前台跑一次 warm-wait" in pr, \
            "live prompt step 7 must say the worker runs warm-wait in the foreground"
        assert "绝不用 background=true" in pr, \
            "live prompt step 7 must forbid background=true for warm-wait"
        assert "keepalive" in pr, "live prompt step 0 must say keepalive"
