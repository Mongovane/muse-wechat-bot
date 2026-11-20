"""Media download Range resume tests (2026-11-19).

Covers download_inbound_image's resume path in wxbot.py: an interrupted
download leaves .enc.part behind; the next attempt sends
`Range: bytes=<downloaded>-` and appends, instead of re-downloading.
Also covers the two fallbacks: server answers 200 (no Range support) ->
restart from zero; server answers 416 -> drop the part and retry.
Tests only; never touches production files or the network.
"""
import base64
import importlib.util
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_media_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot):
    tmp = tempfile.mkdtemp(prefix="mediaresume-")
    media = os.path.join(tmp, "media")
    os.makedirs(media)
    wxbot.MEDIA = media
    wxbot.time.sleep = lambda s: None
    return tmp, media


AES_HEX = "00" * 16  # 16 zero bytes, valid for _parse_aes_key


def make_item():
    return {"image_item": {"media": {"encrypt_query_param": "enc123"},
                           "aeskey": AES_HEX}}


class FakeResp:
    def __init__(self, status_code, headers, chunks):
        self.status_code = status_code
        self.headers = headers
        self._chunks = list(chunks)

    def iter_content(self, chunk_size=65536):
        yield from self._chunks


class FakeSession:
    def __init__(self, resps):
        self.resps = list(resps)
        self.calls = []  # (url, headers) per get()

    def get(self, url, headers=None, timeout=60, stream=True):
        self.calls.append((url, dict(headers or {})))
        return self.resps.pop(0)


def test_resume_sends_range_and_appends():
    """已下载 4 字节 + 服务端 206：请求带 Range: bytes=4-，续写后落盘，
    .enc.part 删掉，文件内容完整。"""
    wxbot = load_wxbot()
    _, media = make_env(wxbot)
    part = os.path.join(media, "in_m1.jpg.enc.part")
    with open(part, "wb") as f:
        f.write(b"ABCD")
    session = FakeSession([FakeResp(
        206, {"Content-Range": "bytes=4-9/10"}, [b"EFGHIJ"])])
    wxbot._cow_cdn_session = lambda: session
    wxbot._cow_decrypt = lambda enc, key: enc  # identity: decrypted == bytes
    api = type("Api", (), {"cdn_base_url": "https://cdn.example"})()

    out = wxbot.download_inbound_image(api, make_item(), "m1")

    assert out == os.path.join(media, "in_m1.jpg")
    assert session.calls[0][1] == {"Range": "bytes=4-"}
    with open(out, "rb") as f:
        assert f.read() == b"ABCDEFGHIJ"
    assert not os.path.exists(part)


def test_server_200_restarts_from_zero():
    """服务端回 200（不支持 Range）时从头重下，不把旧 part 当续传起点。"""
    wxbot = load_wxbot()
    _, media = make_env(wxbot)
    part = os.path.join(media, "in_m2.jpg.enc.part")
    with open(part, "wb") as f:
        f.write(b"XXXX")  # stale partial content, must be discarded
    session = FakeSession([FakeResp(
        200, {"Content-Length": "10"}, [b"ABCDEFGHIJ"])])
    wxbot._cow_cdn_session = lambda: session
    wxbot._cow_decrypt = lambda enc, key: enc
    api = type("Api", (), {"cdn_base_url": "https://cdn.example"})()

    out = wxbot.download_inbound_image(api, make_item(), "m2")

    # 首个请求照常带 Range（客户端事先不知道服务端不支持）；服务端回 200
    # 后代码把已下载量清零、用 wb 从头写全量正文，旧 part 内容被丢弃。
    assert session.calls[0][1] == {"Range": "bytes=4-"}
    with open(out, "rb") as f:
        assert f.read() == b"ABCDEFGHIJ"
    assert not os.path.exists(part)


def test_416_drops_part_and_retries():
    """服务端回 416（本地 part 比服务端多）：删 part、重试，全量拿回。"""
    wxbot = load_wxbot()
    _, media = make_env(wxbot)
    part = os.path.join(media, "in_m3.jpg.enc.part")
    with open(part, "wb") as f:
        f.write(b"TOOLONGPART")
    session = FakeSession([
        FakeResp(416, {}, []),
        FakeResp(200, {"Content-Length": "10"}, [b"ABCDEFGHIJ"]),
    ])
    wxbot._cow_cdn_session = lambda: session
    wxbot._cow_decrypt = lambda enc, key: enc
    api = type("Api", (), {"cdn_base_url": "https://cdn.example"})()

    out = wxbot.download_inbound_image(api, make_item(), "m3")

    assert len(session.calls) == 2
    with open(out, "rb") as f:
        assert f.read() == b"ABCDEFGHIJ"
    assert not os.path.exists(part)
