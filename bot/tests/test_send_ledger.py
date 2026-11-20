"""Send guard + send-intent ledger + priority + media-enum tests (2026-11-19).

Covers the frozen send contract in wxbot.py: validate_send_text,
SendLedgerCorrupt, resolve_send_intent / mark_send_intent_sent (client_id
stable across retries, "muse-wechat-" prefix), append_send_audit (JSONL,
no "text" field, trimmed to 1000 lines, never raises), the text guard wired
into _send_one_file, client_id reuse on ConnectionError retry and on the
omit-token retry, outbox priority ordering (reply > priority-1 flash >
priority-3 flash), and media_type enum (image=1, video=2, file=3).
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
    tmp = tempfile.mkdtemp(prefix="sendledgertest-")
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
    return tmp, dirs


def write_job(outbox, name, text="hi", **extra):
    job = {"to": "u1", "context_token": "ctx", "text": text}
    job.update(extra)
    with open(os.path.join(outbox, name), "w") as f:
        json.dump(job, f)
    return os.path.join(outbox, name)


class FakeApi:
    """send_text accepts optional client_id kwarg; script drives replies.

    Script elements: dict = response, None = raise ConnectionError.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.text_calls = []      # (to, txt, client_id)
        self.omit_client_ids = []  # client_id seen on _post sendmessage

    def send_text(self, to, txt, ctx, client_id=None):
        self.text_calls.append((to, txt, client_id))
        nxt = self.script.pop(0) if self.script else {"ret": 0}
        if nxt is None:
            raise ConnectionError("boom")
        r = dict(nxt)
        r.setdefault("message_id", "mid-%d" % len(self.text_calls))
        return r

    def _post(self, endpoint, body, retries=0):
        if endpoint == "ilink/bot/sendmessage":
            assert "context_token" not in body.get("msg", {}), \
                "omit-token request must not contain context_token"
            self.omit_client_ids.append(body["msg"].get("client_id"))
            nxt = self.script.pop(0) if self.script else {"ret": 0}
            r = dict(nxt)
            r.setdefault("message_id", "mid-omit-%d" % len(self.omit_client_ids))
            return r
        # getconfig / sendtyping etc: harmless success, no ticket.
        return {"ret": 0}

    # Media item sends (client_id optional kwarg per contract).
    def send_image_item(self, to, ctx, enc, key, size, client_id=None):
        return {"ret": 0, "message_id": "m-img"}

    def send_video_item(self, to, ctx, enc, key, size, client_id=None):
        return {"ret": 0, "message_id": "m-vid"}

    def send_file_item(self, to, ctx, enc, key, size, name=None, client_id=None):
        return {"ret": 0, "message_id": "m-file"}


class OrderApi:
    """Records send order only; no _post so typing indicator no-ops."""

    def __init__(self):
        self.order = []

    def send_text(self, to, txt, ctx, client_id=None):
        self.order.append(txt)
        return {"ret": 0, "message_id": "m-%d" % len(self.order)}


class MediaApi:
    """Records media-item calls; captures upload media_type via monkeypatch."""

    def __init__(self):
        self.text_calls = []
        self.image_calls = []
        self.video_calls = []
        self.file_calls = []

    def send_text(self, to, txt, ctx, client_id=None):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "m-text"}

    def send_image_item(self, to, ctx, enc, key, size, client_id=None):
        self.image_calls.append((enc, key, size))
        return {"ret": 0, "message_id": "m-img"}

    def send_video_item(self, to, ctx, enc, key, size, client_id=None):
        self.video_calls.append((enc, key, size))
        return {"ret": 0, "message_id": "m-vid"}

    def send_file_item(self, to, ctx, enc, key, size, *args, **kwargs):
        self.file_calls.append((enc, key, size))
        return {"ret": 0, "message_id": "m-file"}


def _nostub_sleep(wxbot):
    """Stub time.sleep (module-global!) and return the original to restore."""
    orig = wxbot.time.sleep
    wxbot.time.sleep = lambda s: None
    return orig


# --- validate_send_text -------------------------------------------------

def test_validate_send_text():
    wxbot = load_wxbot()
    make_env(wxbot)
    assert wxbot.validate_send_text(None) == "empty"
    assert wxbot.validate_send_text("") == "empty"
    assert wxbot.validate_send_text("   ") == "empty"
    assert wxbot.validate_send_text("--key") == "looks-like-flag"
    assert wxbot.validate_send_text("--") == "looks-like-flag"
    assert wxbot.validate_send_text("hello") is None
    assert wxbot.validate_send_text("hi --flag") is None


# --- send-intent ledger -------------------------------------------------

def test_resolve_send_intent_stable_client_id():
    wxbot = load_wxbot()
    make_env(wxbot)
    cid1, sent1 = wxbot.resolve_send_intent("job-1")
    assert cid1.startswith("muse-wechat-")
    assert sent1 is False
    cid2, sent2 = wxbot.resolve_send_intent("job-1")
    assert cid2 == cid1, "same key twice must return the SAME client_id"
    assert sent2 is False
    cid3, _ = wxbot.resolve_send_intent("job-2")
    assert cid3 != cid1, "different keys get different client_ids"


def test_mark_send_intent_sent():
    wxbot = load_wxbot()
    make_env(wxbot)
    cid, _ = wxbot.resolve_send_intent("job-9")
    wxbot.mark_send_intent_sent("job-9")
    cid2, sent = wxbot.resolve_send_intent("job-9")
    assert cid2 == cid
    assert sent is True


def test_resolve_send_intent_corrupt_ledger_raises():
    wxbot = load_wxbot()
    _, _dirs = make_env(wxbot)
    with open(wxbot.SEND_LEDGER, "w") as f:
        f.write("this is not json {{{ garbage")
    try:
        wxbot.resolve_send_intent("job-x")
    except wxbot.SendLedgerCorrupt:
        return
    raise AssertionError("corrupt SEND_LEDGER must raise SendLedgerCorrupt")


# --- send audit ----------------------------------------------------------

def test_append_send_audit_fields_no_text():
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.append_send_audit("out", "u1", "muse-wechat-abc", "ok",
                            1, error=None, elapsed_ms=12)
    with open(wxbot.SEND_AUDIT) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["direction"] == "out"
    assert rec["to"] == "u1"
    assert rec["client_id"] == "muse-wechat-abc"
    assert rec["result"] == "ok"
    assert rec["attempt"] == 1
    assert rec["elapsed_ms"] == 12
    assert "text" not in rec, "audit must never record message text"


def test_append_send_audit_trims_to_1000_lines():
    wxbot = load_wxbot()
    make_env(wxbot)
    with open(wxbot.SEND_AUDIT, "w") as f:
        for i in range(1005):
            f.write(json.dumps({"i": i}) + "\n")
    wxbot.append_send_audit("out", "u1", "muse-wechat-abc", "ok", 1)
    with open(wxbot.SEND_AUDIT) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) <= 1000
    last = json.loads(lines[-1])
    assert last.get("client_id") == "muse-wechat-abc"


def test_append_send_audit_never_raises_on_directory_path():
    wxbot = load_wxbot()
    tmp, _ = make_env(wxbot)
    wxbot.SEND_AUDIT = tmp  # a directory, not a file: must not raise
    wxbot.append_send_audit("out", "u1", "muse-wechat-abc", "ok", 1)


# --- text guard wired into _send_one_file --------------------------------

def test_send_one_file_flag_text_never_sent():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    path = write_job(dirs["outbox"], "flag.json", text="--key")
    api = FakeApi()
    ok, transient = wxbot._send_one_file(api, {}, "flag.json", path)
    assert api.text_calls == [], "flag-looking text must never reach send_text"
    assert (ok, transient) == (False, False), "guard failure is non-transient"


def test_send_one_file_empty_text_never_sent():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    path = write_job(dirs["outbox"], "empty.json", text="")
    api = FakeApi()
    ok, transient = wxbot._send_one_file(api, {}, "empty.json", path)
    assert api.text_calls == []
    assert (ok, transient) == (False, False)


# --- client_id reuse across retries ---------------------------------------

def test_client_id_reused_on_connection_error_retry():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    orig = _nostub_sleep(wxbot)
    try:
        write_job(dirs["outbox"], "111.json", text="hello")
        api = FakeApi([None, {"ret": 0}])  # raise, then succeed
        wxbot._cmd_send_locked(api, {})
    finally:
        wxbot.time.sleep = orig
    assert len(api.text_calls) == 2, api.text_calls
    cid1 = api.text_calls[0][2]
    cid2 = api.text_calls[1][2]
    assert cid1 and cid1.startswith("muse-wechat-")
    assert cid2 == cid1, "retry must reuse the same client_id"
    assert os.path.exists(os.path.join(dirs["sent"], "111.json"))


def test_client_id_reused_on_omit_token_retry():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    orig = _nostub_sleep(wxbot)
    try:
        path = write_job(dirs["outbox"], "flash-omit.json", text="hello")
        api = FakeApi([{"ret": -2}, {"ret": 0}])
        ok, transient = wxbot._send_one_file(api, {}, "flash-omit.json", path)
    finally:
        wxbot.time.sleep = orig
    assert ok is True, "omit-token retry should succeed"
    assert len(api.text_calls) == 1
    assert len(api.omit_client_ids) == 1
    assert api.omit_client_ids[0] == api.text_calls[0][2], \
        "omit-token retry must reuse the send_text client_id"
    assert api.omit_client_ids[0].startswith("muse-wechat-")


# --- priority ordering -----------------------------------------------------

def test_priority_ordering_reply_then_p1_then_p3():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    # Names chosen so plain alphabetical order would send p3 before p1.
    write_job(dirs["outbox"], "111.json", text="reply-first")
    write_job(dirs["outbox"], "flash-zzz-p1.json", text="flash-p1",
              priority=1)
    write_job(dirs["outbox"], "flash-aaa-p3.json", text="flash-p3",
              priority=3)
    api = OrderApi()
    wxbot._cmd_send_locked(api, {})
    assert api.order == ["reply-first", "flash-p1", "flash-p3"], api.order


# --- media enum ------------------------------------------------------------

def _fake_upload_factory(seen):
    def fake_upload(api, path, to, media_type=1):
        seen["media_type"] = media_type
        seen["path"] = path
        return {"encrypt_query_param": "enc",
                "aes_key_b64": "key",
                "ciphertext_size": 123,
                "raw_size": 123}
    return fake_upload


def _media_file(dirs, name, content=b"x"):
    p = os.path.join(dirs["media"], name)
    with open(p, "wb") as f:
        f.write(content)
    return p


def test_media_enum_image_is_1():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    orig = _nostub_sleep(wxbot)
    try:
        img = _media_file(dirs, "pic.jpg")
        write_job(dirs["outbox"], "222.json", text="pic", image=img)
        seen = {}
        wxbot.upload_media_to_cdn = _fake_upload_factory(seen)
        api = MediaApi()
        ok, _ = wxbot._send_one_file(api, {}, "222.json",
                                     os.path.join(dirs["outbox"], "222.json"))
    finally:
        wxbot.time.sleep = orig
    assert ok is True
    assert seen["media_type"] == 1, seen
    assert len(api.image_calls) == 1


def test_media_enum_video_is_2():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    orig = _nostub_sleep(wxbot)
    try:
        vid = _media_file(dirs, "clip.mp4")
        write_job(dirs["outbox"], "333.json", text="vid", video=vid)
        seen = {}
        wxbot.upload_media_to_cdn = _fake_upload_factory(seen)
        api = MediaApi()
        ok, _ = wxbot._send_one_file(api, {}, "333.json",
                                     os.path.join(dirs["outbox"], "333.json"))
    finally:
        wxbot.time.sleep = orig
    assert ok is True
    assert seen["media_type"] == 2, seen
    assert len(api.video_calls) == 1


def test_media_enum_file_is_3():
    wxbot = load_wxbot()
    _, dirs = make_env(wxbot)
    orig = _nostub_sleep(wxbot)
    try:
        fp = _media_file(dirs, "doc.pdf")
        write_job(dirs["outbox"], "444.json", text="doc", file=fp)
        seen = {}
        wxbot.upload_media_to_cdn = _fake_upload_factory(seen)
        api = MediaApi()
        ok, _ = wxbot._send_one_file(api, {}, "444.json",
                                     os.path.join(dirs["outbox"], "444.json"))
    finally:
        wxbot.time.sleep = orig
    assert ok is True
    assert seen["media_type"] == 3, seen
    assert len(api.file_calls) == 1
