"""Owner / unknown-sender / bind-window tests (2026-11-19).

Covers the frozen owner contract in wxbot.py: load_owner/save_owner/
is_owner, unknown-sender ledger (capped at 100, no message content),
bind state + cmd_bind window (600s), cmd_approve_owner, cmd_list_unknown,
cmd_remove_owner. Tests only; never touches production files.
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
    tmp = tempfile.mkdtemp(prefix="ownertest-")
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


def _age_json_timestamps(path, seconds):
    """Rewrite every epoch/ISO timestamp in a JSON file to `seconds` ago."""
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
                from datetime import datetime
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                from datetime import timedelta, timezone
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


def test_load_owner_missing_file_is_empty():
    wxbot = load_wxbot()
    make_env(wxbot)
    got = wxbot.load_owner()
    assert got["owner_id"] == ""
    assert got["owner_ids"] == []


def test_save_and_load_owner_roundtrip():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.save_owner("owner1", ["owner1", "owner2"])
    got = wxbot.load_owner()
    assert got["owner_id"] == "owner1"
    assert got["owner_ids"] == ["owner1", "owner2"]


def test_is_owner():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.is_owner("nobody") is False
    wxbot.save_owner("owner1", ["owner1", "owner2"])
    assert wxbot.is_owner("owner1") is True
    assert wxbot.is_owner("owner2") is True
    assert wxbot.is_owner("stranger") is False


def test_note_unknown_sender_records_counts_no_content():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.note_unknown_sender("u1")
    wxbot.note_unknown_sender("u1")
    wxbot.note_unknown_sender("u2")
    got = wxbot.load_unknown_senders()
    assert got["u1"]["count"] == 2
    assert got["u2"]["count"] == 1
    for uid, rec in got.items():
        assert set(rec) >= {"first_seen", "last_seen", "count"}
        assert "text" not in rec and "message" not in rec
        assert "content" not in rec
        # ISO timestamps parse
        assert isinstance(rec["first_seen"], str) and "T" in rec["first_seen"]
        assert isinstance(rec["last_seen"], str) and "T" in rec["last_seen"]


def test_unknown_senders_capped_at_100_most_recent():
    wxbot = load_wxbot()
    make_env(wxbot)
    for i in range(105):
        wxbot.note_unknown_sender("u%03d" % i)
    got = wxbot.load_unknown_senders()
    assert len(got) == 100, "cap is 100 most-recent senders"
    assert "u000" not in got, "oldest sender must be evicted"
    assert "u104" in got


def test_bind_state_save_load_clear():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.load_bind_state() == {}
    wxbot.save_bind_state({"opened_at": time.time()})
    got = wxbot.load_bind_state()
    assert got["opened_at"] > 0
    wxbot.clear_bind_state()
    assert wxbot.load_bind_state() == {}


def test_cmd_bind_opens_window_then_already_open():
    wxbot = load_wxbot()
    make_env(wxbot)
    rc, out = capture(wxbot.cmd_bind)
    assert rc == 0
    assert "BIND_OK" in out
    assert wxbot.load_bind_state() != {}
    rc2, out2 = capture(wxbot.cmd_bind)
    assert isinstance(rc2, int)
    assert "BIND_ALREADY_OPEN" in out2


def test_cmd_bind_already_bound_when_owner_exists():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.save_owner("owner1", ["owner1"])
    rc, out = capture(wxbot.cmd_bind)
    assert isinstance(rc, int)
    assert "ALREADY_BOUND" in out


def test_cmd_bind_window_expires_after_600s():
    wxbot = load_wxbot()
    tmp, _ = make_env(wxbot)
    rc, out = capture(wxbot.cmd_bind)
    assert "BIND_OK" in out
    # Fake the window as opened >600s ago (schema-agnostic timestamp ageing).
    _age_json_timestamps(wxbot.BIND_FILE, 3600)
    assert wxbot.load_bind_state() == {}, "expired window reads as empty"
    rc2, out2 = capture(wxbot.cmd_bind)
    assert "BIND_OK" in out2, "expired window must allow a fresh bind"


def test_cmd_approve_owner_adds_and_clears_unknown():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.note_unknown_sender("u9")
    assert "u9" in wxbot.load_unknown_senders()
    rc, out = capture(wxbot.cmd_approve_owner, "u9")
    assert isinstance(rc, int)
    assert "OWNER_APPROVED" in out
    assert wxbot.is_owner("u9") is True
    assert "u9" not in wxbot.load_unknown_senders()
    rc2, out2 = capture(wxbot.cmd_approve_owner, "u9")
    assert "OWNER_ALREADY" in out2


def test_cmd_list_unknown_prints_json():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.note_unknown_sender("uA")
    wxbot.note_unknown_sender("uB")
    rc, out = capture(wxbot.cmd_list_unknown)
    assert isinstance(rc, int)
    try:
        data = json.loads(out.strip())
    except ValueError:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert "uA" in data and "uB" in data
    assert data["uA"]["count"] == 1


def test_cmd_remove_owner():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.save_owner("owner1", ["owner1", "owner2"])
    rc, out = capture(wxbot.cmd_remove_owner, "owner1")
    assert isinstance(rc, int)
    assert "OWNER_REMOVED" in out
    assert wxbot.is_owner("owner1") is False
    assert wxbot.is_owner("owner2") is True
