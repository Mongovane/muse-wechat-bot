"""Finish recheck tests (2026-11-19).

Covers the frozen cmd_finish recheck contract in wxbot.py: before sending,
finish re-checks the inbox for newer messages from the same sender; if one
arrived, it merges it into this job (moves the newer inbox file to DONE,
nothing lost), prints FINISH_RECHECK, returns 2, and does NOT call
cmd_send. The control test proves the normal path still finishes with
FINISH_OK / return 0 when no newer inbox file exists.
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
    tmp = tempfile.mkdtemp(prefix="finishtest-")
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
    wxbot.time.sleep = lambda s: None
    return tmp, dirs


def capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


MSG_ID = "1111111111111111111"
NEWER_ID = "2222222222222222222"


def _seed_job(wxbot, dirs, claim_ts):
    """processing/<id>.json + valid outbox reply job for the same id."""
    proc_path = os.path.join(dirs["processing"], MSG_ID + ".json")
    with open(proc_path, "w") as f:
        json.dump({"from": "u1", "claim_ts": claim_ts,
                   "text": "original question"}, f)
    out_path = os.path.join(dirs["outbox"], MSG_ID + ".json")
    with open(out_path, "w") as f:
        json.dump({"to": "u1", "context_token": "ctx",
                   "text": "reply to original"}, f)
    old = time.time() - 120
    os.utime(out_path, (old, old))
    return proc_path, out_path


class FakeApi:
    """Minimal WeixinApi stand-in for cmd_send; no _post, no network."""

    def __init__(self, *args, **kwargs):
        self.text_calls = []

    def send_text(self, to, txt, ctx, client_id=None):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "m1"}


def test_finish_recheck_newer_same_sender_message():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    claim_ts = int(time.time()) - 120
    _seed_job(wxbot, dirs, claim_ts)
    # A NEWER inbox file from the same sender arrived after the claim.
    with open(os.path.join(dirs["inbox"], NEWER_ID + ".json"), "w") as f:
        json.dump({"from": "u1", "text": "follow-up", "msg_id": NEWER_ID,
                   "ts": time.time()}, f)
    send_calls = []
    orig_send = wxbot.cmd_send
    wxbot.cmd_send = lambda only=None: send_calls.append(only)
    try:
        rc, out = capture(wxbot.cmd_finish, MSG_ID, claim_ts)
    finally:
        wxbot.cmd_send = orig_send
    assert rc == 2, out
    assert "FINISH_RECHECK" in out
    assert send_calls == [], "recheck must NOT send the stale reply"
    assert not os.path.exists(os.path.join(dirs["inbox"], NEWER_ID + ".json"))
    assert os.path.exists(os.path.join(dirs["done"], NEWER_ID + ".json")), \
        "newer inbox file must be merged to DONE, not lost"
    assert os.path.exists(os.path.join(dirs["outbox"], MSG_ID + ".json")), \
        "stale reply job stays for regeneration"


def test_finish_control_no_newer_message_finishes_ok():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    claim_ts = int(time.time())
    _seed_job(wxbot, dirs, claim_ts)
    assert os.listdir(dirs["inbox"]) == []
    wxbot.load_creds = lambda: {}
    wxbot.WeixinApi = FakeApi
    rc, out = capture(wxbot.cmd_finish, MSG_ID, claim_ts)
    assert rc == 0, out
    assert "FINISH_OK" in out
    assert os.path.exists(os.path.join(dirs["sent"], MSG_ID + ".json"))
    assert os.path.exists(os.path.join(dirs["done"], MSG_ID + ".json"))
    assert not os.path.exists(os.path.join(dirs["outbox"], MSG_ID + ".json"))
