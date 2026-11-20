"""Quote-cache tests (2026-11-19).

Covers the frozen quote-cache contract in wxbot.py: load_quote_cache,
record_quote, resolve_quote (by msg_id and by svr_id), oldest-first
pruning past QUOTE_CACHE_CAP, and record_quote never raising on bad input.
Tests only; never touches production files.
"""
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


def make_env(wxbot):
    """Fresh tmp env; EVERY writable module path points at tmp."""
    tmp = tempfile.mkdtemp(prefix="quotecachetest-")
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


def test_load_quote_cache_empty():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.load_quote_cache() == {"by_id": {}, "by_svr": {}}


def test_record_and_resolve_by_id():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.record_quote("m1", "hello world", kind="text", from_uid="u1")
    assert wxbot.resolve_quote("m1") == "hello world"
    cache = wxbot.load_quote_cache()
    assert cache["by_id"]["m1"]["text"] == "hello world"
    assert cache["by_id"]["m1"]["kind"] == "text"
    assert cache["by_id"]["m1"]["from"] == "u1"


def test_resolve_by_svr_id():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.record_quote("m1", "svr hello", svr_id="svr-9")
    # svr lookup: empty qid + svr_id resolves through the by_svr map.
    assert wxbot.resolve_quote("", svr_id="svr-9") == "svr hello"
    assert wxbot.resolve_quote(None, svr_id="svr-9") == "svr hello"
    cache = wxbot.load_quote_cache()
    assert cache["by_svr"]["svr-9"] == "m1"


def test_resolve_unknown_returns_empty():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.record_quote("m1", "hello")
    assert wxbot.resolve_quote("nope") == ""
    assert wxbot.resolve_quote("") == ""
    assert wxbot.resolve_quote("m1", svr_id="svr-missing") == "hello"


def test_cap_prunes_oldest_first():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.QUOTE_CACHE_CAP = 5  # small monkeypatched cap for the test
    for i in range(7):
        wxbot.record_quote("m%d" % i, "text-%d" % i)
    cache = wxbot.load_quote_cache()
    assert len(cache["by_id"]) == 5, cache["by_id"].keys()
    assert "m0" not in cache["by_id"], "oldest entry must be pruned first"
    assert "m1" not in cache["by_id"]
    assert "m6" in cache["by_id"]
    assert wxbot.resolve_quote("m6") == "text-6"
    assert wxbot.resolve_quote("m0") == ""


def test_record_quote_never_raises_on_bad_input():
    wxbot = load_wxbot()
    make_env(wxbot)
    # Each of these must not raise.
    wxbot.record_quote(None, None)
    wxbot.record_quote("", None)
    wxbot.record_quote("x", None)
    wxbot.record_quote(None, "text")
    wxbot.record_quote(123, 456)
    wxbot.record_quote("y", "ok", kind=None, from_uid=None,
                       media_path=None, svr_id=None)
    cache = wxbot.load_quote_cache()
    assert isinstance(cache, dict)
    assert set(cache) == {"by_id", "by_svr"}
