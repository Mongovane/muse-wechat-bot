---
name: muse-wechat-bot
version: 1.0
summary: 让 Muse 本人在个人微信里亲自回消息的机器人方案
description: 复用 CowAgent 的微信 iLink 协议做收发，由 Muse 亲自回答每条微信消息。事件驱动（hook 10 秒唤醒），带 skill 系统（天气/热搜/新闻/小工具/基金/电影感生图）、grok-search 联网搜索、图片发送、防重复认领。无中间模型层，不需要模型 API Key。
---

# muse-wechat-bot

**Use this skill when the user wants any of these:**

- 在个人微信上跑一个机器人，由 Muse（你）亲自回答消息，而不是接第三方 AI
- 把已有的微信机器人方案搬到新的 Muse 对话 / 新机器上复刻
- 给微信机器人加 skill（天气、热搜、新闻、生图…）或排查收发问题

**Do not use it** for 企业微信 / 公众号开发、微信群控外挂、多开等灰产场景。

## 你要交付的东西

一套跑在用户机器（Muse 沙盒 / VPS）上的个人微信机器人：

1. `wxbot.py`：登录（扫码）/ 接收（长轮询）/ 发送（含图片）
2. hook `wechat-inbox`：每 10 秒检查收件箱，有新消息立即唤醒 worker；顺带做看门狗
3. 兜底 cron（5 分钟）：只捡漏，不抢单
4. `skills/`：按场景调用的 skill 文档

协议层复用 CowAgent 的 `channel/weixin/weixin_api.py`（iLink），**不要**用 CowAgent 当 Agent——回答层就是你（Muse）自己。

## 操作步骤

按 `docs/setup.md` 从头到尾执行，顺序不能乱：

1. 装 CowAgent + venv（只需要它的协议文件和 Python 环境）
2. 部署 `bot/wxbot.py` → `qr` 取码 → 用户扫码 → `wait_login` 落盘 `credentials.json`（600 权限）
3. 起 `receive` 接收器（`setsid nohup … &`，断开不死）
4. 装 hook 脚本到 `~/hooks/scripts/`，用 hooks.add 注册（poll 10s），worker 指令全文复制 `bot/prompts/hook-worker.md`；dry_run 测两条路径（空收件箱静默 / 有文件唤醒）再 enable
5. 建 5 分钟兜底 cron，body 全文复制 `bot/prompts/sweeper-cron.md`
6. 复制 `skills/` 到机器人目录；需要 grok-search 时按 `docs/skills.md` 装（fetch/map 免 key，search 要 GROK_API_KEY）
7. 让用户从微信发一条"测试"验证端到端

细节、架构图、排障分别在 `docs/architecture.md`、`docs/troubleshooting.md`。

## 关键约定（不要违背）

- 登录凭证只存本地 `credentials.json`（600），**绝不**进仓库、不贴到对话里；向用户展示登录响应时只报字段名，不报值
- 认领消息必须用 `mv inbox/ → processing/` 原子操作，mv 失败就跳过——这是防重复回复的唯一机制
- receiver 只处理 `message_type == 1` 的用户消息；发送用 `message_type == 2`，不会自问自答
- 发送微信消息是敏感操作：如用户抱怨每次都要点确认，告诉他可以给定时任务授长期许可（设置页可撤销），**不要**想办法绕过审批
- `SESSION_EXPIRED` 是唯一需要重新扫码的情况；其他掉线直接重启接收器复用旧凭证

## Skill 路由（worker 指令里保持这份映射）

天气 → `skills/weather-query.md`；热搜 → `skills/hot-topics.md`；每日新闻 → `skills/daily-news-60s.md`；翻译/二维码/哈希/IP/密码 → `skills/utility-tools.md`；基金 → `skills/fund-market-analysis.md`；影视 → `skills/entertainment.md`；媒体 → `skills/media-info.md`；百科 → `skills/knowledge-wiki.md`；数据 → `skills/data-query.md`；精读网页 → `skills/grok-search/scripts/fetch.js --provider direct`；生图 → 先读 `skills/GPT电影感生图skill_巨物大片优化版.md` 扩写提示词，再用自己的图片能力出图，outbox 加 `"image"` 字段发出。

加新 skill：把 .md 丢进 `skills/`，在两份 prompt 里加一行映射。详见 `docs/skills.md`。
