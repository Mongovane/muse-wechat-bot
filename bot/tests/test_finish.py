"""finish 子命令回归测试：失败路径不丢单、成功路径收尾干净。

覆盖 2026-09-30 提速改造的新链路：
  worker 写 outbox/<id>.json → `wxbot.py finish <id>` 一条命令完成
  发送 + processing→done + latency 打点。
"""
import importlib.util
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot(tmp):
    spec = importlib.util.spec_from_file_location(
        "wxbot_finish_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    # 把目录常量指向隔离的 tmp，避免污染生产目录
    spec.loader.exec_module(mod)
    for name in ("INBOX", "OUTBOX", "SENT", "DONE", "PROCESSING", "MEDIA"):
        p = os.path.join(tmp, name.lower())
        os.makedirs(p, exist_ok=True)
        setattr(mod, name, p)
    mod.SEND_LOCK = os.path.join(tmp, "send.lock")
    mod.LATENCY_LOG = os.path.join(tmp, "wechat-latency.jsonl")
    return mod


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False)


def test_finish_no_processing_fails():
    """processing 文件不存在 → FINISH_FAIL，不抛异常。"""
    tmp = tempfile.mkdtemp(prefix="finishtest-")
    wxbot = load_wxbot(tmp)
    rc = wxbot.cmd_finish("ghost-id")
    assert rc == 1


def test_finish_no_outbox_keeps_processing():
    """outbox 缺失 → FINISH_FAIL，且 processing 保留给兜底。"""
    tmp = tempfile.mkdtemp(prefix="finishtest-")
    wxbot = load_wxbot(tmp)
    write_json(os.path.join(wxbot.PROCESSING, "m1.json"),
               {"from": "u", "text": "hi", "claim_ts": 1000})
    rc = wxbot.cmd_finish("m1")
    assert rc == 1
    assert os.path.exists(os.path.join(wxbot.PROCESSING, "m1.json"))


class FakeApiOk:
    def send_text(self, to, txt, ctx):
        return {"ret": 0, "message_id": "mid-1"}

    def check_send_response(self, resp, fn=""):
        return None


def test_finish_success_moves_done_and_logs(tmp_path=None):
    """发送成功 → processing→done，并写 latency 打点。"""
    tmp = tempfile.mkdtemp(prefix="finishtest-")
    wxbot = load_wxbot(tmp)
    mid = "m2"
    write_json(os.path.join(wxbot.PROCESSING, mid + ".json"),
               {"from": "u@im.wechat", "text": "hi", "claim_ts": 1000})
    write_json(os.path.join(wxbot.OUTBOX, mid + ".json"),
               {"to": "u@im.wechat", "context_token": "c",
                "text": "hello"})

    real_cmd_send = wxbot.cmd_send

    def fake_send():
        # 模拟发送成功：outbox→sent（跳过真实网络）
        os.replace(os.path.join(wxbot.OUTBOX, mid + ".json"),
                   os.path.join(wxbot.SENT, mid + ".json"))
    wxbot.cmd_send = fake_send
    try:
        rc = wxbot.cmd_finish(mid)
    finally:
        wxbot.cmd_send = real_cmd_send
    assert rc == 0
    assert os.path.exists(os.path.join(wxbot.DONE, mid + ".json"))
    assert not os.path.exists(os.path.join(wxbot.PROCESSING, mid + ".json"))
    lines = open(wxbot.LATENCY_LOG).read().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["msg_id"] == mid
    assert entry["note"] == "纯文本"
    assert entry["total_s"] >= 0
