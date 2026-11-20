"""Health / watchdog / alert closed-loop tests (2026-11-19).

Covers the frozen contract in wxbot.py: cmd_health (HEALTH_OK / HEALTH_WARN
/ HEALTH_CRIT, exit 0/1/2), cmd_watchdog (WATCHDOG_OK / WATCHDOG_ALERT /
WATCHDOG_STALE / WATCHDOG_RECOVERED / WATCHDOG_OFF), alert_check closed-loop
(token: send -> suppress -> send again after 3600s; recovered -> recovered
then none; poll: suppress until first_seen >600s), and cmd_alert.
Tests only; never touches production files.
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    """Fresh tmp env; EVERY writable module path points at tmp."""
    tmp = tempfile.mkdtemp(prefix="healthtest-")
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
    with open(wxbot.BUF, "w") as f:  # fresh receiver heartbeat
        f.write("alive\n")
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
    # Dummy creds file so the clean-env health check can pass; the CRIT
    # test re-points CREDS at a nonexistent path.
    wxbot.CREDS = os.path.join(tmp, "credentials.json")
    with open(wxbot.CREDS, "w") as f:
        json.dump({"token": "dummy", "base_url": "http://127.0.0.1:9"}, f)
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def _age_state_file(path, seconds):
    """Fake every epoch/ISO timestamp in a JSON state file to `seconds` ago."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        data = json.load(f)
    now = time.time()

    def age(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v > 1000000000:
            if v > 1000000000000:  # epoch ms
                return int((now - seconds) * 1000)
            return now - seconds
        if isinstance(v, str):
            try:
                from datetime import datetime, timedelta, timezone
                datetime.fromisoformat(v.replace("Z", "+00:00"))
                aged = datetime.now(timezone.utc) - timedelta(seconds=seconds)
                return aged.isoformat()
            except ValueError:
                return v
        if isinstance(v, dict):
            return {k: age(x) for k, x in v.items()}
        if isinstance(v, list):
            return [age(x) for x in v]
        return v

    with open(path, "w") as f:
        json.dump(age(data), f)
    return True


def _touch_old(path, seconds_ago):
    old = time.time() - seconds_ago
    os.utime(path, (old, old))


# --- cmd_health ------------------------------------------------------------

def test_cmd_health_ok_clean_env():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_health, offline=False)
    assert rc == 0
    assert "HEALTH_OK" in out


def test_cmd_health_warn_stale_processing():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    stale = os.path.join(dirs["processing"], "stale.json")
    with open(stale, "w") as f:
        json.dump({"from": "u1"}, f)
    _touch_old(stale, 3600)  # ctime/mtime >10min old
    rc, out = capture(wxbot.cmd_health, offline=False)
    assert rc in (1, 2) and rc != 0
    assert "HEALTH_WARN" in out


def test_cmd_health_crit_creds_missing():
    wxbot = load_wxbot()
    tmp, _ = make_env(wxbot)
    wxbot.CREDS = os.path.join(tmp, "credentials-missing.json")
    rc, out = capture(wxbot.cmd_health, offline=False)
    assert rc in (1, 2) and rc != 0
    assert "HEALTH_CRIT" in out


# --- cmd_watchdog ----------------------------------------------------------

def test_cmd_watchdog_ok_fresh_buf():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert rc == 0
    assert "WATCHDOG_OK" in out


def test_cmd_watchdog_alert_stale_recovered_sequence():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert "WATCHDOG_OK" in out
    _touch_old(wxbot.BUF, 3600)  # buf stale (>30min)
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert rc in (1, 2)
    assert "WATCHDOG_ALERT" in out
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert "WATCHDOG_STALE" in out, "repeat stale sighting must escalate"
    with open(wxbot.BUF, "w") as f:  # fresh again
        f.write("alive\n")
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert rc == 0
    assert "WATCHDOG_RECOVERED" in out


def test_cmd_watchdog_off_flag():
    wxbot = load_wxbot()
    make_env(wxbot)
    with open(wxbot.WATCHDOG_OFF_FILE, "w") as f:
        f.write("off\n")
    rc, out = capture(wxbot.cmd_watchdog, max_age_mins=30)
    assert rc == 0
    assert "WATCHDOG_OFF" in out


# --- alert_check -----------------------------------------------------------

def test_alert_token_send_suppress_send_again():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.alert_check("token", "failing") == "ALERT_SEND"
    assert wxbot.alert_check("token", "failing") == "ALERT_SUPPRESS"
    assert _age_state_file(wxbot.ALERT_STATE_FILE, 7200), \
        "alert state file must exist after first alert"
    assert wxbot.alert_check("token", "failing") == "ALERT_SEND", \
        "last_alert >3600s ago must allow a fresh alert"


def test_alert_token_recovered_then_none():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.alert_check("token", "failing") == "ALERT_SEND"
    assert wxbot.alert_check("token", "recovered") == "ALERT_RECOVERED"
    assert wxbot.alert_check("token", "recovered") == "ALERT_NONE"


def test_alert_poll_suppress_until_first_seen_aged():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.alert_check("poll", "failing") == "ALERT_SUPPRESS"
    assert wxbot.alert_check("poll", "failing") == "ALERT_SUPPRESS"
    assert _age_state_file(wxbot.ALERT_STATE_FILE, 1200), \
        "alert state file must exist after first poll failure"
    assert wxbot.alert_check("poll", "failing") == "ALERT_SEND", \
        "first_seen >600s ago must escalate the poll alert"


def test_cmd_alert_prints_result():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_alert, "token", "failing")
    assert isinstance(rc, int)
    assert "ALERT_SEND" in out
    assert wxbot.alert_check("token", "failing") == "ALERT_SUPPRESS"
