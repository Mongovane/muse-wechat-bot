"""Token -14 stale-vs-expired grading tests (2026-11-19).

Covers the grading in wxbot.py: -14 within the first hour counts as
stale (silent, no expired marker, hook does not nag for a rescan);
-14 still appearing after 1h counts as expired (marker file dropped).
The same window (TOKEN_STALE_WINDOW = 3600) is used by the receive path
and by check_send_response on the send side; the send side is tested
here end to end, plus the _note_token_stale / _clear_token_stale
helpers both sides rely on. Tests only; never touches production files.
"""
import importlib.util
import json
import os
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_token_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="tokenstale-")
    wxbot.TOKEN_STALE_FILE = os.path.join(tmp, "token_stale.json")
    wxbot.SEND_EXPIRED_MARKER = os.path.join(tmp, ".send_session_expired")
    return tmp


def test_first_14_within_hour_is_stale_no_marker():
    """首次 -14（1 小时内）：记 stale，不落过期标记。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.check_send_response({"ret": -14}, "f1.json")
    assert not os.path.exists(wxbot.SEND_EXPIRED_MARKER)
    assert os.path.exists(wxbot.TOKEN_STALE_FILE)
    with open(wxbot.TOKEN_STALE_FILE) as f:
        first = json.load(f)["first_seen"]
    assert abs(first - time.time()) < 5


def test_14_after_one_hour_is_expired_marker_dropped():
    """-14 持续超过 1 小时：落过期标记，hook 可以据此提示重扫。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    with open(wxbot.TOKEN_STALE_FILE, "w") as f:
        json.dump({"first_seen": time.time() - 3601}, f)
    wxbot.check_send_response({"ret": -14}, "f2.json")
    assert os.path.exists(wxbot.SEND_EXPIRED_MARKER)


def test_14_via_errcode_field_and_ok_response_is_quiet():
    """errcode=-14 同样分级；ret=0 什么都不做（不建 stale 文件、不落标记）。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    wxbot.check_send_response({"errcode": -14}, "f3.json")
    assert os.path.exists(wxbot.TOKEN_STALE_FILE)
    assert not os.path.exists(wxbot.SEND_EXPIRED_MARKER)
    wxbot._clear_token_stale()
    wxbot.check_send_response({"ret": 0}, "f4.json")
    assert not os.path.exists(wxbot.TOKEN_STALE_FILE)
    assert not os.path.exists(wxbot.SEND_EXPIRED_MARKER)


def test_note_token_stale_first_seen_stable_then_cleared():
    """_note_token_stale：首次建 first_seen，重复调用返回同一值；
    _clear_token_stale：成功轮询后清掉标记（receive 路径依赖此行为）。"""
    wxbot = load_wxbot()
    make_env(wxbot)
    first = wxbot._note_token_stale()
    assert wxbot._note_token_stale() == first
    wxbot._clear_token_stale()
    assert not os.path.exists(wxbot.TOKEN_STALE_FILE)
    assert wxbot._note_token_stale() != first
