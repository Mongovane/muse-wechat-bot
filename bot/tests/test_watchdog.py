"""watchdog.sh 新鲜度判活回归测试（2026-11-20）。

旧版（认领后睡 20 秒、只看 .alive 文件存在）：冷启动 worker 从认领到
第一次 touch 要几十秒（09:34 实测最长约 59 秒），20 秒一到就误判死、
把单子扔回 inbox，hook 又重唤——同一任务反复认领/判死/重排。2026-11-20
09:34 连发：5 次唤醒、第一份丢回复、第二份晚 5~6 分钟。
新版：宽限 180 秒（覆盖冷启动）+ .alive 新鲜度（60 秒没刷新 = 死），
循环监督。环境变量覆盖宽限/新鲜度/轮询/总时长，测试用小值实跑；
生产默认值（180/60/30/1800）用 test_watchdog_defaults_are_safe 断言。
只碰 tmp，不动 live bot、不发消息。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))


def _setup_job(tmp, mid, claim_age=300, alive_age=None, retries=0):
    proc_dir = os.path.join(tmp, "processing")
    inbox_dir = os.path.join(tmp, "inbox")
    os.makedirs(proc_dir, exist_ok=True)
    os.makedirs(inbox_dir, exist_ok=True)
    now = int(time.time())
    proc = os.path.join(proc_dir, mid + ".json")
    with open(proc, "w") as f:
        json.dump({"msg_id": mid, "claim_ts": now - claim_age,
                   "watchdog_retries": retries,
                   "watchdog_noack": retries > 0}, f)
    if alive_age is not None:
        alive = os.path.join(proc_dir, mid + ".alive")
        open(alive, "a").close()
        t = now - alive_age
        os.utime(alive, (t, t))
    return proc


def _run_watchdog(tmp, mid, env_extra):
    script = os.path.join(tmp, "watchdog.sh")
    shutil.copy(os.path.join(BASE, "..", "watchdog.sh"), script)
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.run(["bash", script, tmp, mid], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
    return p.returncode


def _state(tmp, mid):
    proc = os.path.join(tmp, "processing", mid + ".json")
    inbox = os.path.join(tmp, "inbox", mid + ".json")
    alive = os.path.join(tmp, "processing", mid + ".alive")
    d = None
    if os.path.exists(proc):
        d = json.load(open(proc))
    elif os.path.exists(inbox):
        d = json.load(open(inbox))
    return {"in_processing": os.path.exists(proc),
            "in_inbox": os.path.exists(inbox),
            "alive_exists": os.path.exists(alive),
            "retries": (d or {}).get("watchdog_retries", 0),
            "noack": (d or {}).get("watchdog_noack", False)}


def test_watchdog_leaves_fresh_alive_worker_alone():
    """慢但活着的 worker：.alive 新鲜（刚刷新过），认领再久也不动，
    哪怕思考再慢。宽限已过、认领 300 秒前，.alive 5 秒前刷新。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W1", claim_age=300, alive_age=5)
    rc = _run_watchdog(tmp, "W1", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "3"})
    assert rc == 0
    st = _state(tmp, "W1")
    assert st["in_processing"] and not st["in_inbox"], st
    assert st["retries"] == 0


def test_watchdog_requeues_stale_alive_worker():
    """.alive 120 秒没刷新 = worker 已死：扔回 inbox，retries+1，noack，
    .alive 清掉。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W2", claim_age=300, alive_age=120)
    rc = _run_watchdog(tmp, "W2", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "30"})
    assert rc == 0
    st = _state(tmp, "W2")
    assert st["in_inbox"] and not st["in_processing"], st
    assert st["retries"] == 1 and st["noack"] is True, st
    assert not st["alive_exists"]


def test_watchdog_does_not_judge_inside_grace():
    """宽限期内：.alive 缺失也不判死（冷启动还没起来）。认领 10 秒前，
    宽限 60 秒，总监督 4 秒——宽限没过就退出，单子不动。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W3", claim_age=10, alive_age=None)
    rc = _run_watchdog(tmp, "W3", {"WATCHDOG_GRACE_SECS": "60",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "4"})
    assert rc == 0
    st = _state(tmp, "W3")
    assert st["in_processing"] and not st["in_inbox"], st
    assert st["retries"] == 0


def test_watchdog_requeues_never_started_worker_after_grace():
    """宽限已过、.alive 从来没有过 = worker 从未起来：扔回 inbox。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W4", claim_age=300, alive_age=None)
    rc = _run_watchdog(tmp, "W4", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "30"})
    assert rc == 0
    st = _state(tmp, "W4")
    assert st["in_inbox"] and not st["in_processing"], st
    assert st["retries"] == 1 and st["noack"] is True, st


def test_watchdog_retry_cap_two_then_leaves_for_cron():
    """重试已到 2 次：不再重排，留给 cron/兜底（旧行为保留）。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W5", claim_age=300, alive_age=120, retries=2)
    rc = _run_watchdog(tmp, "W5", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "30"})
    assert rc == 0
    st = _state(tmp, "W5")
    assert st["in_processing"] and not st["in_inbox"], st
    assert st["retries"] == 2


def test_watchdog_exits_when_job_finished():
    """任务文件已不在（回完）：立刻退出，不动任何东西。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    os.makedirs(os.path.join(tmp, "processing"), exist_ok=True)
    rc = _run_watchdog(tmp, "W6", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "30"})
    assert rc == 0


def test_watchdog_defaults_are_safe():
    """生产默认值断言：宽限 180 秒（冷启动最长实测约 59 秒的 3 倍），
    新鲜度 60 秒（keepalive 每 10 秒 touch，60 秒 = 连续 6 次漏 touch），
    轮询 30 秒，总监督 1800 秒。改默认值必须同步改这里。"""
    text = open(os.path.join(BASE, "..", "watchdog.sh")).read()
    assert 'GRACE="${WATCHDOG_GRACE_SECS:-180}"' in text
    assert 'STALE="${WATCHDOG_STALE_SECS:-60}"' in text
    assert 'INTERVAL="${WATCHDOG_INTERVAL_SECS:-30}"' in text
    assert 'MAX_TOTAL="${WATCHDOG_MAX_TOTAL_SECS:-1800}"' in text


def test_watchdog_requeue_deletes_warm_claim_file():
    """2026-11-20 第二轮：重排时删掉这单的 warm-claim-<id>.json——这单若是
    warm 认领的批次 lead，留着它 hook §1f 会按旧 payload 再唤醒一个 worker
    造成重复回；非 lead 的单子文件名对不上，§1f 全量校验同样当 STALE 删掉。"""
    tmp = tempfile.mkdtemp(prefix="watchdog-")
    _setup_job(tmp, "W7", claim_age=300, alive_age=None)
    claim = os.path.join(tmp, "warm-claim-W7.json")
    with open(claim, "w") as f:
        json.dump({"msg_ids": ["W7"], "messages": {}}, f)
    rc = _run_watchdog(tmp, "W7", {"WATCHDOG_GRACE_SECS": "0",
                                   "WATCHDOG_STALE_SECS": "60",
                                   "WATCHDOG_INTERVAL_SECS": "1",
                                   "WATCHDOG_MAX_TOTAL_SECS": "30"})
    assert rc == 0
    st = _state(tmp, "W7")
    assert st["in_inbox"] and not st["in_processing"], st
    assert st["retries"] == 1
    assert not os.path.exists(claim)
