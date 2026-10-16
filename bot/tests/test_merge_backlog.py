"""merge-backlog 回归测试：outbox 里 flash 积压 >=4 时合并成一条汇总。

2026-10-08 Peter 要求（优化项 2）：通道故障后积压不再一条条轰炸，
自动合并成一条"快讯汇总"。
"""
import importlib.util
import json
import os
import tempfile
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))


def load_wxbot():
    spec = importlib.util.spec_from_file_location(
        "wxbot_under_test", os.path.join(BASE, "..", "wxbot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_env(wxbot, n_flash):
    tmp = tempfile.mkdtemp(prefix="mergetest-")
    outbox = os.path.join(tmp, "outbox")
    sent = os.path.join(tmp, "sent")
    os.makedirs(outbox)
    os.makedirs(sent)
    wxbot.OUTBOX = outbox
    wxbot.SENT = sent
    wxbot.SEND_LOCK = os.path.join(tmp, "send.lock")
    # 合并会记共享去重池：指向 tmp，绝不写真实生产池（此前测试曾把
    # "标题0-4" 写进真实池）。
    wxbot.SHARED_POOL = os.path.join(tmp, "pushed-shared.json")
    # 全部可写路径指向 tmp：测试绝不碰生产文件（2026-10-16 教训见
    # test_send_idempotent.py）。
    wxbot.MSGCACHE = os.path.join(tmp, "msg_cache.json")
    wxbot.QUOTA_FILE = os.path.join(tmp, "quota.json")
    wxbot.HISTORY_DIR = os.path.join(tmp, "history")
    os.makedirs(wxbot.HISTORY_DIR, exist_ok=True)
    for i in range(n_flash):
        with open(os.path.join(outbox, "flash-20261008%04d-1.json" % i), "w") as f:
            json.dump({"to": "u1", "text": "⚡ 快讯：标题%d\n补充%d" % (i, i)}, f)
    return tmp, outbox, sent


def test_merge_skip_when_few():
    """不足 4 个不合并，文件原样保留。"""
    wxbot = load_wxbot()
    _, outbox, _ = make_env(wxbot, 3)
    wxbot.cmd_merge_backlog()
    assert len(os.listdir(outbox)) == 3


def test_merge_five_into_digest():
    """5 个 flash → 1 条汇总，原文件删除，标题去前缀。"""
    wxbot = load_wxbot()
    _, outbox, _ = make_env(wxbot, 5)
    wxbot.cmd_merge_backlog()
    files = os.listdir(outbox)
    assert len(files) == 1, files
    assert files[0].endswith("-digest.json"), files
    with open(os.path.join(outbox, files[0])) as f:
        job = json.load(f)
    assert job["to"] == "u1"
    assert job["text"].startswith("📰 快讯汇总（5条"), job["text"]
    for i in range(5):
        assert "· 标题%d" % i in job["text"], job["text"]
    assert "⚡" not in job["text"].replace("📰", "")


def test_digest_not_remerged():
    """已存在的汇总不参与再次合并；3 新 + 1 旧汇总 → 跳过。"""
    wxbot = load_wxbot()
    _, outbox, _ = make_env(wxbot, 3)
    with open(os.path.join(outbox, "flash-202610080900-digest.json"), "w") as f:
        json.dump({"to": "u1", "text": "📰 快讯汇总（2条）\n· 旧1\n· 旧2"}, f)
    wxbot.cmd_merge_backlog()
    assert len(os.listdir(outbox)) == 4  # 3 新 + 1 旧汇总，原样保留


def test_merge_clears_parts_ledger():
    """合并时顺带清理分片账本，不留垃圾。"""
    wxbot = load_wxbot()
    _, outbox, _ = make_env(wxbot, 4)
    with open(os.path.join(outbox, "flash-202610080000-1.json.parts.json"), "w") as f:
        json.dump(["text:0"], f)
    wxbot.cmd_merge_backlog()
    leftovers = [f for f in os.listdir(outbox) if "parts" in f]
    assert not leftovers, leftovers


def test_digest_filename_uses_cst():
    """汇总文件名时间戳必须是 Asia/Shanghai，不是 VM 本地的 UTC。
    2026-10-08 实测：UTC 命名产出 flash-202610080200-digest.json，
    跟其他 CST 命名的 flash 文件对不上。"""
    import datetime
    wxbot = load_wxbot()
    _, outbox, _ = make_env(wxbot, 4)
    wxbot.cmd_merge_backlog()
    files = os.listdir(outbox)
    assert len(files) == 1, files
    expected = (datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8)))
        .strftime("flash-%Y%m%d%H%M-digest.json"))
    assert files[0] == expected, (files[0], expected)


def test_merge_records_items_to_shared_pool():
    """2026-10-11 15:38 重复推送事故回归：合并后各条标题必须记入共享去重池。"""
    wxbot = load_wxbot()
    tmp, outbox, _ = make_env(wxbot, 4)
    pool_path = os.path.join(tmp, "pushed-shared.json")
    wxbot.SHARED_POOL = pool_path
    wxbot.cmd_merge_backlog()
    with open(pool_path) as f:
        pool = json.load(f)
    titles = [e["title"] for e in pool["pushed"]]
    for i in range(4):
        assert "标题%d" % i in titles, titles
    assert all(e["by"] == "flash-digest" for e in pool["pushed"])


def test_merge_pool_no_dup_and_prunes_stale():
    """已在池中的标题不重复记；48 小时前的旧记录被清理。"""
    wxbot = load_wxbot()
    tmp, outbox, _ = make_env(wxbot, 4)
    pool_path = os.path.join(tmp, "pushed-shared.json")
    wxbot.SHARED_POOL = pool_path
    # 时间炸弹修复：种子时间必须相对 now（此前写死 2026-10-11，
    # 48 小时清理口径会随时间推移把"标题0"当旧记录清掉，测试 10-13 后必挂）。
    cst = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(cst)
    with open(pool_path, "w") as f:
        json.dump({"pushed": [
            {"title": "标题0", "ts": now.isoformat(), "by": "flash"},
            {"title": "三天前的旧闻",
             "ts": (now - datetime.timedelta(days=3)).isoformat(),
             "by": "flash"},
        ]}, f)
    wxbot.cmd_merge_backlog()
    with open(pool_path) as f:
        pool = json.load(f)
    titles = [e["title"] for e in pool["pushed"]]
    assert titles.count("标题0") == 1, titles
    assert "三天前的旧闻" not in titles, titles
    for i in range(1, 4):
        assert "标题%d" % i in titles, titles


def test_merge_skips_already_delivered():
    """2026-10-11 18:59 回归：已送达的文本不再收进汇总（Isaias 重复事故）。
    msg_cache 有该文本的 message_id → 该文件被跳过删除，不进 digest。"""
    wxbot = load_wxbot()
    tmp, outbox, _ = make_env(wxbot, 4)
    # 模拟第 0 条的文本已送达（msg_cache 有记录）
    cache_path = os.path.join(tmp, "msg_cache.json")
    delivered_text = "⚡ 快讯：标题0\n补充0"
    with open(cache_path, "w") as f:
        json.dump({"7514987327573018376": delivered_text}, f)
    wxbot.MSGCACHE = cache_path
    pool_path = os.path.join(tmp, "pushed-shared.json")
    wxbot.SHARED_POOL = pool_path
    wxbot.cmd_merge_backlog()
    files = os.listdir(outbox)
    # 4 个原文件：1 个已送达被删除，3 个合并成 digest
    assert len(files) == 1, files
    assert files[0].endswith("-digest.json"), files
    with open(os.path.join(outbox, files[0])) as f:
        job = json.load(f)
    assert job["text"].startswith("📰 快讯汇总（3条"), job["text"]
    assert "标题0" not in job["text"], job["text"]
    for i in range(1, 4):
        assert "标题%d" % i in job["text"], job["text"]


def test_extract_pool_titles_digest():
    """digest 文本解析出各条标题（· 开头）。"""
    wxbot = load_wxbot()
    text = "📰 快讯汇总（3条，通道恢复补发）\n· 标题A\n· 标题B\n· 标题C"
    titles = wxbot._extract_pool_titles("flash-202610111856-digest.json", text)
    assert titles == ["标题A", "标题B", "标题C"], titles


def test_extract_pool_titles_single_flash():
    """单条 flash 取首行去前缀。"""
    wxbot = load_wxbot()
    titles = wxbot._extract_pool_titles(
        "flash-202610111900-1.json", "⚡ 快讯：标题X\n补充一句")
    assert titles == ["标题X"], titles
