"""发送幂等回归测试：图文/多分片任务在中途失败重试时，已落地的分片不再重发。

复现 2026-09-29 21:42 真实故障：文字先发成功、图片上传抛异常，整单留在
outbox，下一次 send 从头重发 → 同一句话在微信发了 3 遍。
"""
import importlib.util
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def send_text(self, to, txt, ctx):
        self.text_calls.append(txt)
        return {"ret": 0, "message_id": "mid-text-%d" % len(self.text_calls)}

    def send_image_item(self, to, ctx, enc, key, size):
        self.image_calls.append((enc, key, size))
        return {"ret": 0, "message_id": "mid-image-%d" % len(self.image_calls)}


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="sendtest-")
    outbox = os.path.join(tmp, "outbox")
    sent = os.path.join(tmp, "sent")
    hist = os.path.join(tmp, "history")
    os.makedirs(outbox)
    os.makedirs(sent)
    wxbot.OUTBOX = outbox
    wxbot.SENT = sent
    wxbot.HISTORY_DIR = hist
    wxbot.time.sleep = lambda s: None  # 测试加速
    return tmp, outbox, sent, hist


def write_job(outbox, name, to="u1", text="hello", image=None):
    job = {"to": to, "context_token": "ctx", "text": text}
    if image:
        job["image"] = image
    with open(os.path.join(outbox, name), "w") as f:
        json.dump(job, f)


def test_retry_skips_delivered_text():
    """文字已发、图片上传抛异常 → 重试时文字不再重发，只补图片。"""
    wxbot = load_wxbot()
    tmp, outbox, sent, hist = make_env(wxbot)
    img = os.path.join(tmp, "pic.jpg")
    open(img, "wb").write(b"fakejpg")
    write_job(outbox, "t1.json", text="hello", image=img)

    calls = {"n": 0}

    def fake_upload(api, path, to, media_type=1):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("proxy boom")  # 第一次上传失败
        return {"encrypt_query_param": "enc", "aes_key_b64": "key",
                "ciphertext_size": 7}

    wxbot.upload_media_to_cdn = fake_upload
    api = FakeApi()

    # 第 1 次：文字成功，上传抛异常 → 任务留在 outbox
    wxbot._cmd_send_locked(api, {})
    assert len(api.text_calls) == 1, api.text_calls
    assert not os.path.exists(os.path.join(sent, "t1.json"))
    assert os.path.exists(os.path.join(outbox, "t1.json.parts.json"))

    # 第 2 次（重试）：文字必须跳过，只发图片
    wxbot._cmd_send_locked(api, {})
    assert len(api.text_calls) == 1, "文字被重复发送: %r" % api.text_calls
    assert len(api.image_calls) == 1, api.image_calls
    assert os.path.exists(os.path.join(sent, "t1.json"))
    assert not os.path.exists(os.path.join(outbox, "t1.json.parts.json")), \
        "账本未清理"

    # 历史记了一条完整回复（含图片标记）
    lines = open(os.path.join(hist, "u1.jsonl")).read().strip().split("\n")
    assert len(lines) == 1, lines
    entry = json.loads(lines[0])
    assert entry["text"] == "hello\n[图片]", entry["text"]
    print("PASS test_retry_skips_delivered_text")


def test_ledger_files_ignored():
    """outbox 里的 *.parts.json 不能被当成待发任务。"""
    wxbot = load_wxbot()
    tmp, outbox, sent, hist = make_env(wxbot)
    with open(os.path.join(outbox, "x.json.parts.json"), "w") as f:
        json.dump(["text:0"], f)
    wxbot._cmd_send_locked(FakeApi(), {})  # 不抛异常即通过
    print("PASS test_ledger_files_ignored")


def test_chunk_resume():
    """多文字分片：chunk0 已发、chunk1 失败 → 重试只补 chunk1。"""
    wxbot = load_wxbot()
    tmp, outbox, sent, hist = make_env(wxbot)
    wxbot.split_text = lambda t, limit=0: ["c0", "c1"]
    write_job(outbox, "t2.json", text="c0c1")

    api = FakeApi()
    orig_send = api.send_text
    fail = {"n": 0}

    def flaky_send(to, txt, ctx):
        if txt == "c1" and fail["n"] < 2:
            fail["n"] += 1
            return {"ret": 500}  # 内部重试也失败，整单中断
        return orig_send(to, txt, ctx)

    api.send_text = flaky_send
    wxbot._cmd_send_locked(api, {})  # c0 发出，c1 失败
    assert api.text_calls == ["c0"], api.text_calls
    wxbot._cmd_send_locked(api, {})  # 重试：只补 c1
    assert api.text_calls == ["c0", "c1"], api.text_calls
    assert os.path.exists(os.path.join(sent, "t2.json"))
    print("PASS test_chunk_resume")


def test_happy_path_no_ledger():
    """无失败时行为不变：一次成功，不留账本。"""
    wxbot = load_wxbot()
    tmp, outbox, sent, hist = make_env(wxbot)
    write_job(outbox, "t3.json", text="hi")
    api = FakeApi()
    wxbot._cmd_send_locked(api, {})
    assert api.text_calls == ["hi"]
    assert os.path.exists(os.path.join(sent, "t3.json"))
    assert not os.path.exists(os.path.join(outbox, "t3.json.parts.json"))
    print("PASS test_happy_path_no_ledger")


if __name__ == "__main__":
    test_retry_skips_delivered_text()
    test_ledger_files_ignored()
    test_chunk_resume()
    test_happy_path_no_ledger()
    print("ALL PASS")
