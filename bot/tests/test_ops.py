"""Ops commands tests (2026-11-19).

Covers the frozen ops contract in wxbot.py: cmd_stats, cmd_preflight (must
not require login), cmd_prune_media (dry-run deletes nothing; real run
deletes old unreferenced media but keeps quote-referenced files), and
cmd_smoke_test(no_send=True) (SKIP, never touches the network, missing
creds must not crash). Tests only; never touches production files.
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
    tmp = tempfile.mkdtemp(prefix="opstest-")
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


def _touch_old(path, seconds_ago):
    old = time.time() - seconds_ago
    os.utime(path, (old, old))


def _seed_media(dirs, wxbot):
    """Old unreferenced file + equally-old quote-referenced file."""
    old = os.path.join(dirs["media"], "old.jpg")
    keep = os.path.join(dirs["media"], "keep.jpg")
    for p in (old, keep):
        with open(p, "wb") as f:
            f.write(b"x")
        _touch_old(p, 40 * 86400)  # >30d old: both age-eligible
    wxbot.record_quote("q1", "a quoted image", media_path=keep)
    return old, keep


def test_cmd_stats_prints_stats():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_stats)
    assert isinstance(rc, int)
    assert rc == 0
    assert "STATS" in out


def test_cmd_preflight_ok_without_login():
    wxbot = load_wxbot()
    tmp, _ = make_env(wxbot)
    # Preflight must not require login: point CREDS at a nonexistent path.
    wxbot.CREDS = os.path.join(tmp, "credentials-missing.json")
    rc, out = capture(wxbot.cmd_preflight)
    assert rc == 0
    assert "PREFLIGHT_OK" in out


def test_cmd_prune_media_dry_run_deletes_nothing():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    old, keep = _seed_media(dirs, wxbot)
    rc, out = capture(wxbot.cmd_prune_media, max_age_days=30,
                      max_total_mb=500, dry_run=True)
    assert isinstance(rc, int)
    assert "PRUNE" in out
    assert os.path.exists(old), "dry-run must not delete anything"
    assert os.path.exists(keep)


def test_cmd_prune_media_real_run_keeps_quoted():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    old, keep = _seed_media(dirs, wxbot)
    rc, out = capture(wxbot.cmd_prune_media, max_age_days=30,
                      max_total_mb=500, dry_run=False)
    assert isinstance(rc, int)
    assert not os.path.exists(old), "old unreferenced file must be deleted"
    assert os.path.exists(keep), "quote-referenced file must be kept"


def test_cmd_smoke_test_no_send_skips_without_creds():
    wxbot = load_wxbot()
    tmp, _ = make_env(wxbot)
    wxbot.CREDS = os.path.join(tmp, "credentials-missing.json")
    rc, out = capture(wxbot.cmd_smoke_test, no_send=True)
    assert rc == 0, "missing creds must SKIP, not crash"
    assert "SMOKE_SKIP" in out
